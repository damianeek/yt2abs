"""Single-job worker with a durable SQLite queue and exponential-ish backoff."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .db import Database, utc_now

logger = logging.getLogger(__name__)

ProcessCallable = Callable[[Mapping[str, Any], Callable[[str], None]], Any]


class JobNotifier(Protocol):
    """Notification hooks the worker calls after a job state change."""

    def job_imported(
        self, job: Mapping[str, Any], fields: Mapping[str, Any]
    ) -> Any: ...

    def job_retry(
        self, job: Mapping[str, Any], error: str, delay: float, attempts: int
    ) -> Any: ...

    def job_failed(
        self, job: Mapping[str, Any], error: str, attempts: int
    ) -> Any: ...


class Worker:
    def __init__(
        self,
        db: Database,
        process: ProcessCallable,
        *,
        max_attempts: int = 3,
        retry_backoff: tuple[int, ...] = (60, 300, 900),
        poll_interval: float = 2.0,
        clock: Callable[[], str] = utc_now,
        wait: Callable[[float], Any] | None = None,
        notifier: JobNotifier | None = None,
        discard: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.db = db
        self.process = process
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff = tuple(retry_backoff) or (60,)
        self.poll_interval = poll_interval
        self.clock = clock
        self._wait = wait
        self.notifier = notifier
        self.discard = discard

    # ------------------------------------------------------------------ public
    def run_forever(self, stop_event: threading.Event) -> None:
        reset = self.db.reset_inflight()
        if reset:
            logger.warning("requeued %d job(s) left in downloading state", reset)
        logger.info("worker started (max_attempts=%d)", self.max_attempts)
        while not stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:  # noqa: BLE001 - the loop must survive storage hiccups
                logger.exception("worker iteration failed")
                processed = False
            if not processed:
                if self._wait is not None:
                    self._wait(self.poll_interval)
                else:
                    stop_event.wait(self.poll_interval)
        logger.info("worker stopped")

    def run_once(self) -> bool:
        job = self.db.claim_next(now=self.clock())
        if job is None:
            return False
        video_id = str(job["video_id"])
        attempt = int(job.get("attempts") or 0)
        logger.info("processing %s (attempt %d/%d)", video_id, attempt, self.max_attempts)

        def report(stage: str) -> None:
            self.db.set_stage(video_id, stage)
            logger.info("%s: stage=%s", video_id, stage)

        try:
            outcome = self.process(job, report)
        except Exception as exc:  # noqa: BLE001 - every failure becomes job state
            self._handle_failure(job, exc)
            return True

        fields = outcome.as_fields() if hasattr(outcome, "as_fields") else dict(outcome or {})
        if self._is_deleted(video_id):
            self._discard_deleted(video_id, fields)
            return True
        self.db.mark_imported(video_id, **fields)
        current = self.db.get_video(video_id)
        if current is not None and current.get("status") == "deleted":
            # Deleted between the check above and the atomic update inside
            # mark_imported(); discard the just-imported artefact.
            self._discard_deleted(video_id, fields)
            return True
        logger.info("imported %s -> %s", video_id, fields.get("file_path"))
        self._notify("job_imported", self._current_job(video_id, job), fields)
        return True

    # ---------------------------------------------------------------- internals
    def _is_deleted(self, video_id: str) -> bool:
        """True when the WebUI deleted the job while it was being processed."""
        try:
            row = self.db.get_video(video_id)
        except Exception:  # noqa: BLE001 - never lose a finished job over this check
            logger.exception("could not check deletion state of %s", video_id)
            return False
        return bool(row and row.get("status") == "deleted")

    def _discard_deleted(self, video_id: str, fields: Mapping[str, Any]) -> None:
        """Undo a download/import that finished after the user deleted the job."""
        logger.warning("job %s was deleted while processing; discarding result", video_id)
        discard = self.discard or getattr(self.process, "discard", None)
        if callable(discard):
            try:
                discard(fields)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                logger.exception("discarding result of deleted job %s failed", video_id)
            return
        path = str(fields.get("file_path") or "")
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not remove %s of deleted job %s: %s", path, video_id, exc)

    def _handle_failure(self, job: Mapping[str, Any], exc: Exception) -> None:
        video_id = str(job["video_id"])
        if self._is_deleted(video_id):
            current = self._current_job(video_id, job)
            fields = {
                "file_path": current.get("file_path"),
                "abs_item_id": current.get("abs_item_id"),
                "abs_episode_id": current.get("abs_episode_id"),
            }
            if any(fields.values()):
                self._discard_deleted(video_id, fields)
            return
        attempts = int(job.get("attempts") or 0)
        error = f"{type(exc).__name__}: {exc}"
        current = self._current_job(video_id, job)
        if attempts >= self.max_attempts:
            self.db.mark_failed(video_id, error)
            logger.error("job %s failed after %d attempt(s): %s", video_id, attempts, error)
            self._notify("job_failed", current, error, attempts)
            return
        delay = self.retry_backoff[min(attempts - 1, len(self.retry_backoff) - 1)]
        self.db.schedule_retry(video_id, error, delay)
        logger.warning(
            "job %s attempt %d/%d failed (%s); retrying in %ss",
            video_id,
            attempts,
            self.max_attempts,
            error,
            delay,
        )
        self._notify("job_retry", current, error, float(delay), attempts)

    def _current_job(self, video_id: str, fallback: Mapping[str, Any]) -> Mapping[str, Any]:
        """Prefer the fresh row (the pipeline may have filled in the title)."""
        try:
            row = self.db.get_video(video_id)
        except Exception:  # noqa: BLE001 - notification context is best effort
            logger.exception("could not refresh job %s for notifications", video_id)
            row = None
        return row or fallback

    def _notify(self, method: str, *args: Any) -> None:
        """Call an optional notification hook; failures never affect the queue."""
        notifier = self.notifier
        if notifier is None:
            return
        callback = getattr(notifier, method, None)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - notifications are best effort
            logger.exception("notification hook %s failed", method)
