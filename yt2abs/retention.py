"""Hourly retention job: delete old episodes through the Audiobookshelf API.

Policy hierarchy (highest wins):

1. ``videos.retention_override`` (per-video): ``0`` = keep forever,
2. ``channels.<id>.retention_days`` from ``config.yaml``: ``null`` = keep,
   ``0`` = delete on the next run,
3. ``defaults.retention_days``: ``null`` = keep.

The deadline is always computed from ``videos.added_at`` (the moment the video
was marked), never from its publication date. It is recomputed on every run, so
changing a channel policy to ``null`` (keep) before the deadline protects every
episode that is still imported.

``protect_unfinished`` (per channel, inherited from defaults) defers deletion of
an episode that was started but not finished in Audiobookshelf, up to twice the
retention window. After ``2 * N`` days the episode is deleted regardless.

Deletion is always ``DELETE /api/podcasts/{item}/episode/{episode}?hard=1``:
Audiobookshelf removes both the episode entry and its audio file. The podcast
item (folder, cover, settings) is never removed, so an empty channel remains a
valid podcast.

The same run also reconciles episodes that were deleted manually in
Audiobookshelf: if the podcast item or the episode is gone, the local row is
marked ``deleted`` instead of being retried forever.

Reconcile is guarded by a safety fuse, because a wrong ``ABS_URL``, a proxy
answer or a rebuilt ABS database must never mass-mark the library as deleted:

* when the library listing fails, is empty while rows are imported, or the
  configured library root is missing/empty, the whole pass is skipped (report +
  alert): nothing is written and nothing is deleted;
* when more than ``retention.reconcile_max_deletions`` (or
  ``reconcile_max_fraction`` of the checked rows) would disappear in one pass,
  the pass is reported and **no write happens at all**;
* a row is only marked deleted when its audio file is really gone from disk and
  the episode cannot be found by a unique file name (a rebuilt ABS gets its ids
  refreshed instead of being treated as deleted).

``retention.dry_run`` (and ``GET /api/retention/preview``) computes the same
plan without any write, delete or notification.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .abs_client import AbsClient, AbsError, AbsNotFound, EpisodeProgress
from .config import Config
from .db import Database, utc_now

logger = logging.getLogger(__name__)

_MISSING = object()


class _UnknownProgress:
    """Marker: playback state could not be read, so nothing may be deleted."""

    __slots__ = ()


_UNKNOWN_PROGRESS = _UnknownProgress()


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from SQLite (``Z`` suffix included)."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def resolve_retention_days(config: Config, row: Mapping[str, Any]) -> int | None:
    """Effective retention in days for one video row (``None`` = keep forever)."""
    override = row.get("retention_override")
    if override is not None:
        value = max(0, int(override))
        # Existing contract from phase 1a: override 0 means "keep forever".
        return None if value == 0 else value
    channel = config.channels.get(str(row.get("channel_id") or ""))
    if channel is not None:
        return channel.retention_days
    return config.defaults.retention_days


def resolve_protect_unfinished(config: Config, row: Mapping[str, Any]) -> bool:
    return config.channel(str(row.get("channel_id") or "")).protect_unfinished


def _days_word(days: int) -> str:
    """English plural: 1 day, 2 days."""
    return "day" if days == 1 else "days"


class RetentionActionError(RuntimeError):
    """An explicit WebUI deletion could not be completed in Audiobookshelf."""


@dataclass
class RetentionReport:
    """Result of one retention pass (JSON serialisable)."""

    started_at: str
    finished_at: str | None = None
    checked: int = 0
    kept: int = 0
    deleted: list[dict[str, Any]] = field(default_factory=list)
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    deferred: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    resynced: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    #: What a dry run would do (filled only when ``dry_run`` is true).
    planned: list[dict[str, Any]] = field(default_factory=list)
    #: Set when the safety fuse stopped a pass (report only, zero writes).
    reconcile_blocked: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "checked": self.checked,
            "kept": self.kept,
            "deleted": list(self.deleted),
            "reconciled": list(self.reconciled),
            "deferred": list(self.deferred),
            "errors": list(self.errors),
            "resynced": list(self.resynced),
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
            "planned": list(self.planned),
            "reconcile_blocked": self.reconcile_blocked,
        }


@dataclass
class _PlannedAction:
    """One write the planner wants to perform after the safety checks."""

    kind: str  # "delete" | "reconcile" | "resync"
    row: Mapping[str, Any]
    item_id: str = ""
    episode_id: str = ""
    reason: str = ""
    days: int | None = None


class RetentionJob:
    """Runs the retention policy; safe to call manually while the thread runs."""

    def __init__(
        self,
        config: Config,
        db: Database,
        abs_client: AbsClient,
        *,
        now: Callable[[], datetime] | None = None,
        notifier: Any | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.abs = abs_client
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.notifier = notifier
        self._lock = threading.Lock()
        self.last_report: RetentionReport | None = None
        self.last_run_at: str | None = None
        self._item_cache: dict[str, Any] = {}
        self._progress: dict[tuple[str, str], EpisodeProgress] | None = None
        self._progress_loaded = False
        self._progress_unavailable = False
        self._last_alert: tuple[Any, ...] | None = None

    # ------------------------------------------------------------------ public
    @property
    def running(self) -> bool:
        """True while a pass (scheduled, manual or preview) is in progress."""
        return self._lock.locked()

    def try_start(self) -> bool:
        """Start a pass in a background thread, atomically claiming the lock.

        Returns ``False`` when a pass (scheduled, manual or preview) already
        holds the lock, so two quick POSTs cannot start two passes.
        """
        if not self._lock.acquire(blocking=False):
            return False

        def _runner() -> None:
            dry_run = bool(self.config.retention.dry_run)
            pending: list[dict[str, Any]] = []
            try:
                report, pending = self._run(dry_run=dry_run)
                self.last_report = report
                self.last_run_at = report.finished_at
            except Exception:  # noqa: BLE001 - background pass must not kill the app
                logger.exception("manual retention run failed")
            finally:
                self._lock.release()
            # Same contract as run_once(): one summary push per pass, outside
            # the lock, and never for a dry run.
            if not dry_run:
                self._notify_deleted_batch(pending)

        threading.Thread(target=_runner, name="yt2abs-retention-manual", daemon=True).start()
        return True

    def run_once(self, *, dry_run: bool | None = None) -> RetentionReport:
        """Run one pass; ``dry_run=None`` follows ``retention.dry_run``."""
        effective_dry_run = self.config.retention.dry_run if dry_run is None else bool(dry_run)
        with self._lock:
            report, pending = self._run(dry_run=effective_dry_run)
            if dry_run is None:
                self.last_report = report
                self.last_run_at = report.finished_at
        # One summary push per pass, outside the lock: a bulk cleanup must not
        # hold up the WebUI and must not burn the ntfy rate limit with N alerts.
        # A dry run has an empty ``pending`` (the executor is never called).
        if not effective_dry_run:
            self._notify_deleted_batch(pending)
        return report

    def preview(self) -> RetentionReport:
        """Compute what the next pass would do, without writing anything."""
        with self._lock:
            report, _pending = self._run(dry_run=True)
        return report

    def run_forever(
        self,
        stop_event: threading.Event,
        interval_seconds: float,
        *,
        run_immediately: bool = True,
    ) -> None:
        logger.info(
            "retention job started (interval=%ss, run_on_startup=%s, dry_run=%s)",
            int(interval_seconds),
            run_immediately,
            self.config.retention.dry_run,
        )
        if run_immediately:
            self._safe_run()
        while not stop_event.wait(max(1.0, interval_seconds)):
            self._safe_run()
        logger.info("retention job stopped")

    def delete_video(self, video_id: str) -> dict[str, Any]:
        """Delete one video now (WebUI "delete"), including its ABS episode.

        Raises :class:`RetentionActionError` when Audiobookshelf cannot be
        reached, so the UI does not pretend the episode is gone.
        """
        with self._lock:
            row = self.db.get_video(video_id)
            if row is None:
                raise KeyError(video_id)
            if row["status"] == "deleted":
                return row
            if row["status"] == "imported" or (
                row.get("abs_item_id") and row.get("abs_episode_id")
            ):
                report = RetentionReport(started_at=utc_now())
                self._item_cache = {}
                library_items = None
                if not (row.get("abs_item_id") and row.get("abs_episode_id")):
                    library_items = self._library_items()
                item_id, episode_id, changed = self._ensure_ids(row, library_items, report)
                if changed and item_id and episode_id:
                    self.db.update_video(
                        video_id, abs_item_id=item_id, abs_episode_id=episode_id
                    )
                if item_id and episode_id:
                    # A rebuilt ABS may have new ids: refresh by unique file name
                    # before falling back to the local delete.
                    if hasattr(self.abs, "get_item"):
                        try:
                            self._get_item(item_id)
                        except AbsNotFound:
                            recovered = self._recover_ids(
                                row, library_items or self._library_items()
                            )
                            if recovered is not None:
                                item_id, episode_id, _item = recovered
                                self.db.update_video(
                                    video_id, abs_item_id=item_id, abs_episode_id=episode_id
                                )
                    action = _PlannedAction(
                        "delete", row, item_id, episode_id, "manual", days=None
                    )
                    self._delete(action, report, manual=True)
                    if report.errors:
                        raise RetentionActionError(
                            str(report.errors[0].get("error") or "ABS delete failed")
                        )
                    self._remove_local_file(row)
                    return self.db.get_video(video_id) or row
                hard_errors = [
                    error
                    for error in report.errors
                    if str(error.get("error") or "").startswith("locate:")
                ]
                if hard_errors:
                    raise RetentionActionError(str(hard_errors[0].get("error")))
            # The episode is already missing in ABS (or was never indexed):
            # finish the local side and remove a stale file if it is ours.
            self._remove_local_file(row)
            return self.db.mark_deleted(video_id, reason="deleted from panel") or row

    # ---------------------------------------------------------------- internals
    def _run(self, *, dry_run: bool) -> tuple[RetentionReport, list[dict[str, Any]]]:
        report = RetentionReport(started_at=utc_now(), dry_run=dry_run)
        pending: list[dict[str, Any]] = []
        self._item_cache = {}
        self._progress = None
        self._progress_loaded = False

        rows = self.db.list_by_status("imported")
        report.checked = len(rows)
        library_items = self._library_items()
        reconcile_ok = library_items is not None
        if not reconcile_ok:
            self._block(
                report,
                "library_listing_failed",
                "could not fetch the ABS library listing",
                "Zero writes: the whole pass was skipped.",
                notify=not dry_run,
            )
        elif rows and not library_items:
            # An empty listing while rows are imported means the URL/library id
            # points at the wrong ABS (or an empty 200 response), not that the
            # whole library vanished.
            reconcile_ok = False
            self._block(
                report,
                "library_mismatch",
                "ABS library listing is empty while the database holds imported episodes",
                "Zero writes: probably a wrong ABS_URL/library_id.",
                notify=not dry_run,
            )
        elif not self._library_root_ok(rows):
            reconcile_ok = False
            self._block(
                report,
                "library_unavailable",
                f"library directory {self.config.library_path!r} is unavailable or empty",
                "Zero writes: a missing mount cannot mark the library as deleted.",
                notify=not dry_run,
            )

        progress = None
        self._progress_unavailable = False
        if not reconcile_ok:
            # A failed listing (wrong ABS_URL, stopped ABS, rebuilt database) or
            # a missing library mount is not evidence that anything was deleted.
            # Skip the whole pass instead of risking a mass "deleted" stamp.
            report.finished_at = utc_now()
            logger.warning("retention: pass skipped (reconcile unavailable)")
            return report, pending

        if any(resolve_protect_unfinished(self.config, row) for row in rows):
            progress = self._load_progress(report, dry_run=dry_run)

        actions: list[_PlannedAction] = []
        for row in rows:
            try:
                actions.extend(
                    self._plan_row(
                        row,
                        library_items,
                        report,
                        progress=progress,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pass
                logger.exception("retention: unexpected failure for %s", row.get("video_id"))
                report.errors.append(
                    {"video_id": row.get("video_id"), "error": f"{type(exc).__name__}: {exc}"}
                )

        limit = self.config.retention.reconcile_limit(report.checked)
        missing = [action for action in actions if action.kind == "reconcile"]
        if missing and len(missing) > limit:
            self._block(
                report,
                "too_many_missing",
                (
                    f"{len(missing)} of {report.checked} "
                    f"episodes disappeared in one pass (limit {limit})"
                ),
                "Zero writes: reconcile and deletion paused until the next pass. "
                "Check ABS, and for a deliberate cleanup raise "
                "retention.reconcile_max_deletions.",
                notify=not dry_run,
                would_reconcile=[
                    {"video_id": action.row.get("video_id"), "reason": action.reason}
                    for action in missing
                ],
                checked=report.checked,
                limit=limit,
            )
            actions = []

        if dry_run:
            report.planned = [self._describe(action) for action in actions]
        else:
            self._execute(actions, report, pending)

        # A later outage of the same kind must alert again: the dedupe window is
        # for repeated identical hourly passes, not for the process lifetime.
        if report.reconcile_blocked is None:
            self._last_alert = None

        report.finished_at = utc_now()
        logger.info(
            "retention: checked=%d deleted=%d reconciled=%d resynced=%d deferred=%d "
            "kept=%d errors=%d dry_run=%s%s",
            report.checked,
            len(report.deleted),
            len(report.reconciled),
            len(report.resynced),
            len(report.deferred),
            report.kept,
            len(report.errors),
            report.dry_run,
            " blocked" if report.reconcile_blocked else "",
        )
        return report, pending

    def _safe_run(self) -> None:
        try:
            self.run_once()
        except Exception:  # noqa: BLE001 - the schedule must survive ABS/storage hiccups
            logger.exception("retention run failed")

    def _plan_row(
        self,
        row: Mapping[str, Any],
        library_items: Mapping[str, dict[str, Any]] | None,
        report: RetentionReport,
        *,
        progress: Mapping[tuple[str, str], EpisodeProgress] | None,
    ) -> list[_PlannedAction]:
        """Decide what to do with one row; no writes happen here."""
        actions: list[_PlannedAction] = []
        video_id = str(row.get("video_id") or "")
        item_id, episode_id, changed = self._ensure_ids(row, library_items, report)
        if not item_id or not episode_id:
            return actions
        if changed:
            actions.append(_PlannedAction("resync", row, item_id, episode_id, "ids_changed"))

        item: dict[str, Any] | None
        try:
            item = self._get_item(item_id)
        except AbsNotFound:
            item = None
        except AbsError as exc:
            logger.warning(
                "retention: cannot read ABS item %s for %s: %s", item_id, video_id, exc
            )
            report.errors.append({"video_id": video_id, "error": f"reconcile: {exc}"})
            return actions

        if item is None:
            recovered = self._recover_ids(row, library_items)
            if recovered is not None:
                new_item, new_episode, item = recovered
                if (new_item, new_episode) != (item_id, episode_id):
                    item_id, episode_id = new_item, new_episode
                    actions.append(
                        _PlannedAction("resync", row, item_id, episode_id, "ids_changed")
                    )
            else:
                if self._file_exists(row):
                    report.kept += 1
                    report.errors.append(
                        {
                            "video_id": video_id,
                            "error": (
                                "reconcile: the podcast is missing in ABS, but the file is still "
                                f"on disk: {row.get('file_path')}"
                            ),
                        }
                    )
                    return actions
                actions.append(
                    _PlannedAction("reconcile", row, item_id, episode_id, "item_missing")
                )
                return actions
        elif self.abs.find_episode_by_id(item, episode_id) is None:
            filename = Path(str(row.get("file_path") or "")).name
            matches = self._episodes_by_filename(item_id, filename, item)
            unique = matches[0] if len(matches) == 1 else None
            if unique is not None and str(unique.get("id") or "") not in ("", episode_id):
                episode_id = str(unique.get("id") or "")
                actions.append(
                    _PlannedAction("resync", row, item_id, episode_id, "ids_changed")
                )
            elif self._file_exists(row):
                # The audio file is still there: never mark the row deleted.
                report.kept += 1
                if matches:
                    reason = (
                        f"reconcile: episode {episode_id} is missing in ABS, but "
                        f"{len(matches)} episodes share the same file name and the file "
                        "is still on disk - not guessing which episode it is"
                    )
                else:
                    reason = (
                        "reconcile: the episode is missing in ABS, but the file is still "
                        f"on disk: {row.get('file_path')}"
                    )
                report.errors.append({"video_id": video_id, "error": reason})
                return actions
            else:
                actions.append(
                    _PlannedAction(
                        "reconcile", row, item_id, episode_id, "episode_missing"
                    )
                )
                return actions

        days = resolve_retention_days(self.config, row)
        if days is None:
            report.kept += 1
            return actions

        added_at = parse_timestamp(row.get("added_at"))
        if added_at is None:
            report.errors.append(
                {"video_id": video_id, "error": f"invalid added_at: {row.get('added_at')!r}"}
            )
            return actions
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        try:
            deadline = added_at + timedelta(days=days)
        except OverflowError:
            # Legacy/corrupt rows may hold a huge override; one row must not
            # kill the hourly pass (the API validates 0..36500).
            report.errors.append(
                {"video_id": video_id, "error": f"retention too large: {days} days"}
            )
            return actions
        if now < deadline:
            report.kept += 1
            return actions

        if days > 0 and resolve_protect_unfinished(self.config, row):
            entry = self._progress_for(item_id, episode_id, video_id, progress, report)
            if isinstance(entry, _UnknownProgress):
                return actions
            if entry is not None and entry.unfinished:
                extended = added_at + timedelta(days=2 * days)
                if now < extended:
                    logger.info(
                        "retention: %s started but unfinished; deferring until %s",
                        video_id,
                        extended.isoformat(),
                    )
                    report.deferred.append(
                        {
                            "video_id": video_id,
                            "reason": "unfinished",
                            "deadline": extended.isoformat().replace("+00:00", "Z"),
                        }
                    )
                    return actions

        actions.append(_PlannedAction("delete", row, item_id, episode_id, "retention", days=days))
        return actions

    def _execute(
        self,
        actions: list[_PlannedAction],
        report: RetentionReport,
        pending: list[dict[str, Any]],
    ) -> None:
        for action in actions:
            row = action.row
            video_id = str(row.get("video_id") or "")
            try:
                self._execute_one(action, report, pending)
            except Exception as exc:  # noqa: BLE001 - one action must not stop the rest
                logger.exception("retention: action %s failed for %s", action.kind, video_id)
                report.errors.append(
                    {"video_id": video_id, "error": f"{action.kind}: {type(exc).__name__}: {exc}"}
                )

    def _execute_one(
        self,
        action: _PlannedAction,
        report: RetentionReport,
        pending: list[dict[str, Any]],
    ) -> None:
        row = action.row
        video_id = str(row.get("video_id") or "")
        if action.kind == "resync":
            self.db.update_video(
                video_id,
                abs_item_id=action.item_id,
                abs_episode_id=action.episode_id,
            )
            report.resynced.append(
                {
                    "video_id": video_id,
                    "item_id": action.item_id,
                    "episode_id": action.episode_id,
                }
            )
            logger.info(
                "retention: refreshed ABS ids for %s (item %s, episode %s)",
                video_id,
                action.item_id,
                action.episode_id,
            )
        elif action.kind == "reconcile":
            self.db.mark_deleted(
                video_id,
                reason=f"reconcile: {action.reason}",
                only_status="imported",
            )
            report.reconciled.append({"video_id": video_id, "reason": action.reason})
            logger.info(
                "retention: %s gone from ABS (%s); marked deleted",
                video_id,
                action.reason,
            )
        elif action.kind == "delete":
            self._delete(action, report, pending=pending)

    def _delete(
        self,
        action: _PlannedAction,
        report: RetentionReport,
        *,
        manual: bool = False,
        pending: list[dict[str, Any]] | None = None,
    ) -> None:
        row = action.row
        video_id = str(row.get("video_id") or "")
        try:
            self.abs.delete_episode(action.item_id, action.episode_id, hard=True)
        except AbsNotFound:
            if manual:
                # The user explicitly asked to delete this row: remove our own
                # copy and finish the local side instead of returning 502.
                self._remove_local_file(row)
                self.db.mark_deleted(video_id, reason="deleted from panel (missing in ABS)")
                logger.info(
                    "retention: %s missing in ABS; manual delete finished locally", video_id
                )
                return
            # Already gone (deleted manually between reconcile and delete).
            if not self._file_exists(row):
                self.db.mark_deleted(
                    video_id,
                    reason="reconcile: episode already missing",
                    only_status="imported",
                )
                report.reconciled.append({"video_id": video_id, "reason": "episode_missing"})
                logger.info(
                    "retention: %s already missing in ABS; marked deleted", video_id
                )
            else:
                report.errors.append(
                    {
                        "video_id": video_id,
                        "error": (
                            "delete: ABS returned 404, but the file is still on disk: "
                            f"{row.get('file_path')}"
                        ),
                    }
                )
            return
        except AbsError as exc:
            logger.error("retention: ABS delete failed for %s: %s", video_id, exc)
            report.errors.append({"video_id": video_id, "error": f"delete: {exc}"})
            return

        self.db.mark_deleted(video_id, only_status="imported")
        report.deleted.append(
            {
                "video_id": video_id,
                "item_id": action.item_id,
                "episode_id": action.episode_id,
                "file_path": row.get("file_path"),
            }
        )
        logger.info(
            "retention: deleted %s (episode %s, file %s)",
            video_id,
            action.episode_id,
            row.get("file_path"),
        )
        if manual or action.days is None:
            # Explicit WebUI deletion: the user already knows, so stay silent
            # (the ntfy channel is for errors only).
            self._warn_if_file_still_exists(row)
            return
        if action.days == 0:
            reason = "retention: 0-day policy"
        else:
            reason = f"retention: expired after {action.days} {_days_word(action.days)}"
        if pending is not None:
            pending.append(
                {
                    "video_id": video_id,
                    "title": row.get("title"),
                    "channel_title": row.get("channel_title"),
                    "reason": reason,
                }
            )
        self._warn_if_file_still_exists(row)

    def _notify_deleted_batch(self, pending: list[dict[str, Any]]) -> None:
        """Tell the error channel about retention deletions in one summary push.

        Called once per pass, after the lock is released. Manual WebUI
        deletions and reconcile paths (episode/podcast already missing in ABS)
        deliberately stay silent: those rows were not removed by retention, and
        a false 404 must not fan out notifications.
        """
        if not pending:
            return
        notifier = self.notifier
        if notifier is None:
            return
        reasons = {str(item.get("reason") or "") for item in pending}
        reason = reasons.pop() if len(reasons) == 1 else ""
        callback = getattr(notifier, "job_deleted_batch", None)
        if callable(callback):
            try:
                callback(pending, reason)
            except Exception:  # noqa: BLE001 - notifications never break retention
                logger.exception("retention: deleted notification failed")
            return
        single = getattr(notifier, "job_deleted", None)
        if not callable(single):
            return
        for item in pending:
            try:
                single(item, str(item.get("reason") or ""))
            except Exception:  # noqa: BLE001 - same for a fallback notifier
                logger.exception(
                    "retention: deleted notification failed for %s",
                    item.get("video_id"),
                )

    def _block(
        self,
        report: RetentionReport,
        reason: str,
        message: str,
        consequence: str,
        *,
        notify: bool = True,
        **extra: Any,
    ) -> None:
        """Record a blocked reconcile pass and alert once per distinct event."""
        payload: dict[str, Any] = {
            "reason": reason,
            "message": message,
            "consequence": consequence,
            **extra,
        }
        report.reconcile_blocked = payload
        report.errors.append({"error": f"reconcile blocked ({reason}): {message}"})
        logger.error("retention: reconcile blocked (%s): %s", reason, message)
        self._send_alert(report, payload, notify=notify)

    def _send_alert(
        self, report: RetentionReport, payload: dict[str, Any], *, notify: bool = True
    ) -> None:
        """Send one operational alert, deduped per identical pass.

        ``notify=False`` (dry run) still records the fact in the report but
        never touches the notifier and never consumes the dedupe slot.
        """
        if not notify:
            return
        key = (payload.get("reason"), payload.get("message"))
        if key == self._last_alert:
            return
        self._last_alert = key
        notifier = self.notifier
        callback = getattr(notifier, "retention_alert", None) if notifier else None
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 - notifications never break retention
            logger.exception("retention: alert failed")

    def _progress_for(
        self,
        item_id: str,
        episode_id: str,
        video_id: str,
        progress: Mapping[tuple[str, str], EpisodeProgress] | None,
        report: RetentionReport,
    ) -> EpisodeProgress | None | _UnknownProgress:
        """Progress of one episode; ``_UNKNOWN_PROGRESS`` means "do not delete"."""
        if self._progress_unavailable:
            # The scope could not be read (bad progress_user, 5xx, malformed
            # answer). Falling back to /api/me would check the wrong listener.
            return _UNKNOWN_PROGRESS
        if progress is not None:
            return progress.get((item_id, episode_id))
        try:
            return self.abs.get_media_progress(item_id, episode_id)
        except AbsError as exc:
            # Do not delete when playback state is unknown: a missed run is
            # cheaper than deleting an episode somebody is still listening to.
            logger.warning("retention: progress check failed for %s: %s", video_id, exc)
            report.errors.append({"video_id": video_id, "error": f"progress: {exc}"})
            return _UNKNOWN_PROGRESS

    def _load_progress(
        self, report: RetentionReport, *, dry_run: bool = False
    ) -> dict[tuple[str, str], EpisodeProgress] | None:
        """Playback progress map for the configured/all ABS accounts.

        A failure makes the whole pass treat playback state as unknown: rows
        protected by ``protect_unfinished`` are deferred instead of deleted.
        Only a 401/403 from ``/api/users`` (non-admin key, default scope) falls
        back to the key owner, because that is the documented single-account
        mode. Nothing is alerted in a dry run.
        """
        if self._progress_loaded:
            return self._progress
        self._progress_loaded = True
        loader = getattr(self.abs, "media_progress_map", None)
        if loader is None:
            return None
        wanted = str(getattr(self.config.abs, "progress_user", "") or "").strip()
        try:
            if wanted:
                self._progress = loader(user=wanted)
            else:
                all_loader = getattr(self.abs, "all_media_progress", None)
                if all_loader is None:
                    self._progress = loader()
                else:
                    try:
                        self._progress = all_loader()
                    except AbsError as exc:
                        if exc.status_code not in (401, 403):
                            raise
                        warning = (
                            "progress of all ABS accounts is unavailable "
                            f"({exc}); using only the API key owner account"
                        )
                        report.warnings.append(warning)
                        logger.warning("retention: %s", warning)
                        self._progress = loader()
        except AbsError as exc:
            message = f"could not read ABS progress: {exc}"
            report.warnings.append(message)
            report.errors.append({"error": message})
            logger.error(
                "retention: %s; protect_unfinished defers all due episodes in this pass",
                message,
            )
            self._progress = None
            self._progress_unavailable = True
            self._send_alert(
                report,
                {
                    "reason": "progress_unavailable",
                    "message": message,
                    "consequence": (
                        "Episodes with protect_unfinished will not be deleted in this "
                        "pass (listening progress is unknown)."
                    ),
                },
                notify=not dry_run,
            )
        return self._progress

    def _ensure_ids(
        self,
        row: Mapping[str, Any],
        library_items: Mapping[str, dict[str, Any]] | None,
        report: RetentionReport,
    ) -> tuple[str, str, bool]:
        """Return stored ABS ids or recover them from the recorded file path."""
        item_id = str(row.get("abs_item_id") or "")
        episode_id = str(row.get("abs_episode_id") or "")
        if item_id and episode_id:
            return item_id, episode_id, False

        video_id = str(row.get("video_id") or "")
        file_path = str(row.get("file_path") or "")
        if not file_path:
            report.errors.append({"video_id": video_id, "error": "no ABS ids and no file_path"})
            return "", "", False
        if library_items is None:
            report.errors.append(
                {"video_id": video_id, "error": "no ABS ids and library listing unavailable"}
            )
            return "", "", False
        folder = str(Path(self._abs_file_path(file_path)).parent)
        try:
            item = self.abs.find_item_by_path(
                self.config.abs.library_id, folder, items=list(library_items.values())
            )
            if item is None:
                report.errors.append(
                    {"video_id": video_id, "error": "ABS ids missing and podcast not found"}
                )
                return "", "", False
            item_id = str(item.get("id") or "")
            full = self._get_item(item_id) if item_id else None
            matches = (
                self._episodes_by_filename(item_id, Path(file_path).name, full)
                if full is not None
                else []
            )
            episode = matches[0] if len(matches) == 1 else None
            if episode is None:
                report.errors.append(
                    {"video_id": video_id, "error": "ABS ids missing and episode not found"}
                )
                return "", "", False
            episode_id = str(episode.get("id") or "")
        except AbsError as exc:
            report.errors.append({"video_id": video_id, "error": f"locate: {exc}"})
            return "", "", False
        if item_id and episode_id:
            logger.info("retention: recovered ABS ids for %s", video_id)
            return item_id, episode_id, True
        return "", "", False

    def _recover_ids(
        self, row: Mapping[str, Any], library_items: Mapping[str, dict[str, Any]] | None
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Find the podcast by folder and the episode by file name (rebuilt ABS)."""
        file_path = str(row.get("file_path") or "")
        if not file_path or library_items is None:
            return None
        folder = str(Path(self._abs_file_path(file_path)).parent)
        item = self.abs.find_item_by_path(
            self.config.abs.library_id, folder, items=list(library_items.values())
        )
        if item is None:
            return None
        item_id = str(item.get("id") or "")
        if not item_id:
            return None
        try:
            full = self._get_item(item_id)
        except AbsError:
            return None
        matches = self._episodes_by_filename(item_id, Path(file_path).name, full)
        episode = matches[0] if len(matches) == 1 else None
        if episode is None:
            return None
        episode_id = str(episode.get("id") or "")
        if not episode_id:
            return None
        return item_id, episode_id, full

    def _episodes_by_filename(
        self, item_id: str, filename: str, item: Mapping[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Episodes with this audio file name; empty when the name is unusable."""
        if not filename:
            return []
        finder = getattr(self.abs, "find_episodes_by_filename", None)
        if finder is not None:
            return [dict(episode) for episode in finder(item_id, filename, item=item)]
        single = self.abs.find_episode_by_filename(item_id, filename, item=item)
        return [dict(single)] if single is not None else []

    def _library_items(self) -> Mapping[str, dict[str, Any]] | None:
        try:
            items = self.abs.list_library_items(self.config.abs.library_id)
        except AbsError as exc:
            logger.warning("retention: cannot list ABS library items: %s", exc)
            return None
        return {str(item.get("id") or ""): item for item in items if item.get("id")}

    def _get_item(self, item_id: str) -> dict[str, Any]:
        cached = self._item_cache.get(item_id)
        if cached is _MISSING:
            raise AbsNotFound(f"ABS item {item_id} not found")
        if isinstance(cached, dict):
            return cached
        try:
            item = self.abs.get_item(item_id)
        except AbsNotFound:
            self._item_cache[item_id] = _MISSING
            raise
        self._item_cache[item_id] = item
        return item

    def _abs_file_path(self, path: str) -> str:
        """Map a yt2abs library path to the path Audiobookshelf sees."""
        local = str(self.config.library_path or "").rstrip("/")
        remote = str(self.config.abs.library_path or "").rstrip("/")
        if local and remote and (path == local or path.startswith(local + "/")):
            return remote + path[len(local) :]
        return path

    def _file_exists(self, row: Mapping[str, Any]) -> bool:
        file_path = str(row.get("file_path") or "")
        return bool(file_path) and Path(file_path).exists()

    def _library_root_ok(self, rows: Sequence[Mapping[str, Any]]) -> bool:
        """A missing *or empty* library root means the bind mount is not there.

        Docker's short bind syntax silently creates an empty host directory, so
        ``is_dir()`` alone would accept an unmounted share and every file check
        would report "file gone".
        """
        root = str(self.config.library_path or "")
        if not root:
            return True
        path = Path(root)
        if not path.is_dir():
            return False
        try:
            has_entries = next(path.iterdir(), None) is not None
        except OSError:
            return False
        return has_entries or not rows

    def _describe(self, action: _PlannedAction) -> dict[str, Any]:
        return {
            "video_id": action.row.get("video_id"),
            "action": action.kind,
            "reason": action.reason,
            "item_id": action.item_id or None,
            "episode_id": action.episode_id or None,
            "days": action.days,
        }

    def _remove_local_file(self, row: Mapping[str, Any]) -> None:
        """Remove a stale local file, but only inside the configured library."""
        file_path = str(row.get("file_path") or "")
        library_path = str(self.config.library_path or "")
        if not file_path or not library_path:
            return
        path = Path(file_path)
        try:
            path.resolve().relative_to(Path(library_path).resolve())
        except (OSError, ValueError):
            logger.warning(
                "retention: refusing to delete %s outside library %s",
                file_path,
                library_path,
            )
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("retention: could not remove local file %s: %s", file_path, exc)
        else:
            logger.info("retention: removed local file %s", file_path)

    def _warn_if_file_still_exists(self, row: Mapping[str, Any]) -> None:
        file_path = str(row.get("file_path") or "")
        if file_path and Path(file_path).exists():
            logger.warning(
                "retention: ABS deleted episode %s but file %s still exists",
                row.get("video_id"),
                file_path,
            )
