"""FastAPI application: job API, bookmarklet fallback page and worker lifecycle."""
from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .abs_client import AbsClient, from_settings
from .channels import MAX_RETENTION_DAYS, ChannelPolicyStore, resolve_channel_reference
from .config import Config, load_config
from .db import Database, as_public
from .notify import JOB_EVENTS, DiscordNotifier, FanoutNotifier, normalise_events, parse_events
from .ntfy import NtfyNotifier
from .pipeline import ImportPipeline
from .playlist import PlaylistPoller
from .retention import RetentionJob
from .security import SecurityMiddleware
from .updater import YtDlpUpdater
from .urls import VIDEO_ID_RE, InvalidYouTubeURL, canonical_url, extract_video_id
from .webui import create_router, job_public
from .worker import Worker

logger = logging.getLogger(__name__)

NOTIFY_EVENTS_SETTING = "notify.events"
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: Seconds spent at most joining worker/retention/updater threads on shutdown
#: (docker-compose ``stop_grace_period`` is 30 s).
SHUTDOWN_JOIN_BUDGET = 20.0
#: Job ids in URLs are YouTube video ids; the read-only job page validates them
#: so a stray path segment cannot be reflected back into HTML.
JOB_ID_RE = VIDEO_ID_RE


def _configure_logging() -> None:
    """Make application logs visible under ``uvicorn --factory``.

    Uvicorn only configures its own loggers, so without this the root logger has
    no handler and INFO messages (retention, updater, worker) are dropped.
    """
    level_name = os.environ.get("YT2ABS_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        root.setLevel(level)
    logging.getLogger("yt2abs").setLevel(level)

#: What each job event means, in the order shown in the WebUI.
EVENT_CATALOGUE: tuple[tuple[str, str, str], ...] = (
    (
        "imported",
        "Import finished",
        "The video was downloaded and added to Audiobookshelf.",
    ),
    (
        "retry",
        "Retry",
        "A single attempt failed, the job returned to the queue.",
    ),
    (
        "failed",
        "Permanent failure",
        "The job failed after all attempts - needs attention.",
    ),
)

STATUS_LABELS = {
    "queued": "queued",
    "downloading": "downloading",
    "imported": "imported",
    "failed": "failed",
    "deleted": "deleted",
}


class JobRequest(BaseModel):
    url: str = Field(..., description="YouTube video link")
    retention_override: int | None = Field(
        default=None,
        ge=0,
        le=MAX_RETENTION_DAYS,
        description=(
            "Retention days for this video; 0 = keep forever "
            "(null = channel policy)"
        ),
    )


class JobResponse(BaseModel):
    video_id: str
    url: str | None
    status: str
    title: str
    channel_title: str | None
    file_path: str | None
    error: str | None
    attempts: int
    created: bool = False
    requeued: bool = False


class NotificationSettingsRequest(BaseModel):
    """Job events the WebUI wants delivered to Discord."""

    events: list[str] = Field(
        default_factory=list,
        description="Subset: imported, retry, failed (empty list = no notifications)",
    )


def _serialise(
    row: Mapping[str, Any],
    *,
    created: bool = False,
    requeued: bool = False,
    config: Config | None = None,
) -> dict[str, Any]:
    if config is not None:
        return job_public(config, row, created=created, requeued=requeued)
    payload = as_public(row)
    payload["created"] = created
    payload["requeued"] = requeued
    return payload


def _worker_enabled_default() -> bool:
    return os.environ.get("YT2ABS_WORKER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _playlist_switch(config: Config) -> tuple[bool, str]:
    """Effective playlist polling flag and where it came from.

    ``YT2ABS_PLAYLIST_ENABLED`` is a *kill-switch* like the worker one: a falsy
    spelling disables polling, but it never enables a playlist that
    ``playlist.enabled`` leaves off.  Returns ``(enabled, source)`` so the log
    line can say which switch decided without re-parsing the environment.
    """
    raw = os.environ.get("YT2ABS_PLAYLIST_ENABLED")
    if raw is not None and raw.strip():
        allowed = raw.strip().lower() not in ("0", "false", "no", "off")
        return bool(config.playlist.enabled) and allowed, "env"
    return bool(config.playlist.enabled), "config"


def _default_pipeline(config: Config, db: Database) -> ImportPipeline:
    return ImportPipeline(config, db, from_settings(config.abs))


def create_app(
    config: Config | None = None,
    *,
    db: Database | None = None,
    pipeline: ImportPipeline | None = None,
    worker_enabled: bool | None = None,
    pipeline_factory: Callable[[Config, Database], ImportPipeline] = _default_pipeline,
    notifier: DiscordNotifier | None = None,
    ntfy_notifier: NtfyNotifier | None = None,
    retention_job: RetentionJob | None = None,
    updater: YtDlpUpdater | None = None,
    abs_client: AbsClient | None = None,
    channel_resolver: Callable[[str], str] | None = None,
    playlist_poller: PlaylistPoller | None = None,
    playlist_enabled: bool | None = None,
) -> FastAPI:
    _configure_logging()
    settings = config or load_config()
    database = db or Database(settings.paths.database_path)
    store = ChannelPolicyStore(database)
    enabled = _worker_enabled_default() if worker_enabled is None else worker_enabled
    active_notifier = notifier if notifier is not None else DiscordNotifier.from_env()
    active_ntfy = ntfy_notifier if ntfy_notifier is not None else NtfyNotifier.from_env()
    # Discord keeps its imported/retry/failed selection; ntfy only implements
    # failed/deleted, so the fan-out skips missing hooks per backend.
    worker_notifier = FanoutNotifier([active_notifier, active_ntfy])

    # Where the active event selection comes from: webui | env | default.
    notify_source = {
        "value": "env" if os.environ.get("DISCORD_NOTIFY_EVENTS") is not None else "default"
    }

    def notifications_state() -> dict[str, Any]:
        """Serialise the notification configuration (never the webhook URL)."""
        active = frozenset(active_notifier.events)
        return {
            "configured": bool(active_notifier.configured),
            "enabled": bool(active_notifier.enabled),
            "disabled_by_env": bool(active_notifier.disabled),
            "source": notify_source["value"],
            "available": list(JOB_EVENTS),
            "events": [
                {
                    "name": name,
                    "label": label,
                    "description": description,
                    "enabled": name in active,
                }
                for name, label, description in EVENT_CATALOGUE
            ],
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        effective_config = store.apply(settings)
        app.state.config = effective_config
        stored_events = database.get_setting(NOTIFY_EVENTS_SETTING)
        if stored_events is not None:
            active_notifier.set_events(parse_events(stored_events))
            notify_source["value"] = "webui"
        app.state.db = database
        app.state.notifier = active_notifier
        logger.info(
            "discord notifications %s (events=%s, source=%s)",
            "enabled" if active_notifier.enabled else "disabled",
            ", ".join(sorted(active_notifier.events)) or "none",
            notify_source["value"],
        )
        logger.info(
            "ntfy error notifications %s (events=%s)%s",
            "enabled" if active_ntfy.enabled else "disabled",
            ", ".join(sorted(active_ntfy.events)) or "none",
            f": {active_ntfy.config_error}" if active_ntfy.config_error else "",
        )
        logger.info(
            "LAN mode: no authentication (rate limit %d/min, public %d/min, "
            "trusted proxies: %s)",
            settings.api.rate_limit_per_minute,
            settings.api.public_rate_limit_per_minute,
            ", ".join(settings.api.trusted_proxies) or "none",
        )
        app.state.stop_event = threading.Event()
        app.state.worker_thread = None
        app.state.retention_thread = None
        app.state.updater_thread = None
        app.state.playlist_thread = None
        app.state.worker = None
        app.state.retention = None
        app.state.updater = None
        if enabled:
            active_pipeline = pipeline or pipeline_factory(effective_config, database)
            if not settings.abs.configured:
                raise RuntimeError(
                    "Audiobookshelf is not configured: set abs.url, ABS_API_KEY, "
                    "abs.library_id and abs.library_path (or disable the worker)."
                )
            app.state.pipeline = active_pipeline
            app.state.worker = Worker(
                database,
                active_pipeline.process,
                max_attempts=settings.worker.max_attempts,
                retry_backoff=settings.worker.retry_backoff,
                poll_interval=settings.worker.poll_interval,
                notifier=worker_notifier,
                discard=getattr(active_pipeline, "discard", None),
            )
            worker_thread = threading.Thread(
                target=app.state.worker.run_forever,
                args=(app.state.stop_event,),
                name="yt2abs-worker",
                daemon=True,
            )
            worker_thread.start()
            app.state.worker_thread = worker_thread

            active_retention = retention_job or RetentionJob(
                effective_config,
                database,
                getattr(active_pipeline, "abs", None) or from_settings(settings.abs),
                notifier=worker_notifier,
            )
            app.state.retention = active_retention
            retention_thread = threading.Thread(
                target=active_retention.run_forever,
                args=(app.state.stop_event, settings.retention.interval_seconds),
                kwargs={"run_immediately": settings.retention.run_on_startup},
                name="yt2abs-retention",
                daemon=True,
            )
            retention_thread.start()
            app.state.retention_thread = retention_thread

            active_updater = updater or YtDlpUpdater(settings)
            app.state.updater = active_updater
            updater_thread = threading.Thread(
                target=active_updater.run_forever,
                args=(app.state.stop_event, settings.ytdlp.interval_seconds),
                name="yt2abs-ytdlp-updater",
                daemon=True,
            )
            updater_thread.start()
            app.state.updater_thread = updater_thread

        # Playlist polling is independent of the worker: it only enqueues jobs,
        # so it can stay on while the worker is paused for maintenance.
        if playlist_enabled is None:
            playlist_on, playlist_switch = _playlist_switch(settings)
        else:
            playlist_on, playlist_switch = bool(playlist_enabled), "override"
        playlist_config = effective_config.playlist
        if playlist_on and playlist_config.url:
            active_playlist = playlist_poller or PlaylistPoller(
                effective_config, database
            )
            app.state.playlist_poller = active_playlist
            playlist_thread = threading.Thread(
                target=active_playlist.run_forever,
                args=(app.state.stop_event, playlist_config.interval_seconds),
                name="yt2abs-playlist",
                daemon=True,
            )
            playlist_thread.start()
            app.state.playlist_thread = playlist_thread
            logger.info(
                "playlist polling enabled (interval=%d min, switch=%s)",
                playlist_config.interval_minutes,
                playlist_switch,
            )
        elif playlist_on:
            logger.warning(
                "playlist polling enabled but playlist.url is empty - poller not started"
            )
        else:
            logger.info("playlist polling disabled")
        try:
            yield
        finally:
            app.state.stop_event.set()
            # Total shutdown budget must stay below docker-compose
            # ``stop_grace_period`` (30 s): three sequential 15 s joins would
            # exceed it and the container would get SIGKILL mid-download.
            deadline = time.monotonic() + SHUTDOWN_JOIN_BUDGET
            for thread in (
                app.state.worker_thread,
                app.state.retention_thread,
                app.state.updater_thread,
                app.state.playlist_thread,
            ):
                if thread is None:
                    continue
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    app = FastAPI(title="yt2abs", version=__version__, lifespan=lifespan)
    app.add_middleware(SecurityMiddleware, config=settings.api)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.config = settings

    def current_config() -> Config:
        return getattr(app.state, "config", settings)

    def apply_config(new_config: Config) -> None:
        """Make channel edits effective immediately and for future jobs."""
        app.state.config = new_config
        active_pipeline = getattr(app.state, "pipeline", None)
        if active_pipeline is not None:
            try:
                active_pipeline.config = new_config
            except Exception:  # noqa: BLE001 - injected test doubles may be immutable
                logger.debug("pipeline does not accept a new config", exc_info=True)
        active_retention = getattr(app.state, "retention", None)
        if active_retention is not None:
            try:
                active_retention.config = new_config
            except Exception:  # noqa: BLE001 - same for injected fakes
                logger.debug("retention job does not accept a new config", exc_info=True)
        active_playlist = getattr(app.state, "playlist_poller", None)
        if active_playlist is not None:
            try:
                active_playlist.config = new_config
            except Exception:  # noqa: BLE001 - same for injected fakes
                logger.debug("playlist poller does not accept a new config", exc_info=True)

    def abs_provider() -> AbsClient:
        if abs_client is not None:
            return abs_client
        active_pipeline = getattr(app.state, "pipeline", None)
        client = getattr(active_pipeline, "abs", None)
        if client is not None:
            return client
        return from_settings(settings.abs)

    app.state.db = database
    app.state.notifier = active_notifier
    app.state.ntfy = active_ntfy
    app.state.worker_notifier = worker_notifier
    app.state.pipeline = pipeline
    app.state.retention = retention_job
    app.state.updater = updater
    app.state.playlist_poller = playlist_poller
    app.state.worker_enabled = enabled

    # ------------------------------------------------------------------- API
    @app.post("/api/jobs", status_code=202)
    def create_job(payload: JobRequest) -> JSONResponse:
        try:
            video_id = extract_video_id(payload.url)
        except InvalidYouTubeURL as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row, action = database.create_or_requeue(
            video_id, retention_override=payload.retention_override
        )
        status_code = 202 if action == "created" else 200
        return JSONResponse(
            content=_serialise(
                row,
                created=action == "created",
                requeued=action == "requeued",
                config=current_config(),
            ),
            status_code=status_code,
        )

    @app.get("/api/jobs", response_model=None)
    def list_jobs(
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        if status is not None and status not in STATUS_LABELS:
            raise HTTPException(status_code=400, detail="Unknown status.")
        rows = database.list_videos(limit=limit, status=status)
        effective = current_config()
        return {
            "jobs": [_serialise(row, config=effective) for row in rows],
            "counts": database.counts(),
        }

    @app.get("/api/jobs/{video_id}")
    def get_job(video_id: str) -> dict[str, Any]:
        row = database.get_video(video_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such job.")
        return _serialise(row, config=current_config())

    @app.get("/api/policy")
    def get_policy(
        channel_id: str | None = Query(default=None, max_length=64),
        name: str | None = Query(default=None, max_length=200),
    ) -> dict[str, Any]:
        """Effective retention policy of a channel (used by the browser extension).

        Channels from ``config.yaml`` are matched by id first and then by display
        name (case-insensitive, so the popup can work before a video is marked).
        Unknown channels fall back to ``defaults``.
        """
        wanted_id = (channel_id or "").strip()
        wanted_name = (name or "").strip().casefold()
        effective = current_config()
        channel = effective.channels.get(wanted_id) if wanted_id else None
        matched_by = "id" if channel is not None else None
        if channel is None and wanted_name:
            for candidate in effective.channels.values():
                if (candidate.name or "").strip().casefold() == wanted_name:
                    channel = candidate
                    matched_by = "name"
                    break
        source = "channel"
        if channel is None:
            channel = effective.channel(wanted_id)
            source = "defaults"
        return {
            "channel_id": channel.channel_id or None,
            "channel_name": channel.name or (name or None),
            "retention_days": channel.retention_days,
            "protect_unfinished": channel.protect_unfinished,
            "source": source,
            "matched_by": matched_by,
        }

    def retention_state(*, summary: bool = False) -> dict[str, Any]:
        job = getattr(app.state, "retention", None)
        report = getattr(job, "last_report", None) if job is not None else None
        if report is None:
            report_payload = None
        elif summary:
            payload = report.as_dict()
            report_payload = {
                "started_at": payload["started_at"],
                "finished_at": payload["finished_at"],
                "checked": payload["checked"],
                "kept": payload["kept"],
                "deleted": len(payload["deleted"]),
                "reconciled": len(payload["reconciled"]),
                "resynced": len(payload["resynced"]),
                "deferred": len(payload["deferred"]),
                "errors": len(payload["errors"]),
                "dry_run": payload["dry_run"],
                "reconcile_blocked": payload["reconcile_blocked"],
            }
        else:
            report_payload = report.as_dict()
        effective = current_config()
        return {
            "enabled": job is not None,
            "running": bool(job is not None and getattr(job, "running", False)),
            "interval_minutes": effective.retention.interval_minutes,
            "run_on_startup": effective.retention.run_on_startup,
            "dry_run": effective.retention.dry_run,
            "reconcile_max_deletions": effective.retention.reconcile_max_deletions,
            "reconcile_max_fraction": effective.retention.reconcile_max_fraction,
            "progress_user": effective.abs.progress_user or None,
            "progress_scope": "all_users" if not effective.abs.progress_user else "single_user",
            "last_run": getattr(job, "last_run_at", None) if job is not None else None,
            "last_report": report_payload,
        }

    def ytdlp_state(*, live: bool = False) -> dict[str, Any]:
        active = getattr(app.state, "updater", None)
        version = getattr(active, "current_version", None) if active is not None else None
        if live and active is not None:
            version = active.version_of() or version
        result = getattr(active, "last_result", None) if active is not None else None
        return {
            "enabled": bool(active is not None and settings.ytdlp.update),
            "version": version,
            "interval_hours": settings.ytdlp.interval_hours,
            "bin_dir": settings.ytdlp_bin_dir,
            "last_update": result.as_dict() if result is not None else None,
        }

    def playlist_state(*, detail: bool = True) -> dict[str, Any]:
        """Playlist poller status; never exposes the (unlisted) playlist URL.

        Diagnostics live in ``/api/system`` (``detail=True``).  ``detail=False``
        keeps a payload that only says *whether* the last listing failed, for
        any caller that must stay cheap.
        """
        poller = getattr(app.state, "playlist_poller", None)
        thread = getattr(app.state, "playlist_thread", None)
        cfg = current_config().playlist
        report = getattr(poller, "last_result", None) if poller is not None else None
        last = None
        if report is not None:
            payload = report.as_dict()
            error = payload["error"]
            last = {
                "started_at": payload["started_at"],
                "finished_at": payload["finished_at"],
                "seen": payload["seen"],
                "queued": len(payload["queued"]),
                "skipped": payload["skipped"],
                "error": error if detail else bool(error),
                "disabled": payload["disabled"],
            }
        return {
            # ``enabled`` means the background thread is alive, so a configured
            # playlist switched off by the env kill-switch reports False.
            "enabled": bool(thread is not None and thread.is_alive()),
            "configured": cfg.configured,
            "interval_minutes": cfg.interval_minutes,
            "last_run": getattr(poller, "last_run_at", None) if poller is not None else None,
            "last_report": last,
        }

    def health_state() -> dict[str, Any]:
        """Public, minimal liveness payload: no paths, errors or counters."""
        alive: dict[str, bool] = {}
        degraded = False
        for name, attribute in (
            ("worker", "worker_thread"),
            ("retention", "retention_thread"),
            ("updater", "updater_thread"),
        ):
            thread = getattr(app.state, attribute, None)
            running = bool(thread is not None and thread.is_alive())
            alive[name] = running
            # Only a thread that was started and has died means trouble; a
            # disabled component has no thread at all.
            degraded = degraded or (thread is not None and not running)
        return {"status": "degraded" if degraded else "ok", "alive": alive}

    @app.get("/health")
    def health() -> JSONResponse:
        """Liveness probe: status plus thread liveness, no diagnostics."""
        state = health_state()
        return JSONResponse(
            state, status_code=200 if state["status"] == "ok" else 503
        )

    # ------------------------------------------------------ retention / updater
    @app.get("/api/retention")
    def get_retention() -> dict[str, Any]:
        return retention_state()

    @app.post("/api/retention/run")
    def run_retention(wait: bool = Query(default=False)) -> Any:
        """Start a retention pass in the background (202) by default.

        ``?wait=true`` runs it inside the request for debugging and E2E; the
        regular UI polls ``GET /api/retention`` instead of holding a proxy
        timeout for the whole ABS round-trip.
        """
        job = getattr(app.state, "retention", None)
        if job is None:
            raise HTTPException(
                status_code=409,
                detail="Retention job is disabled (worker disabled or ABS not configured).",
            )
        if job.running:
            raise HTTPException(
                status_code=409,
                detail="A retention pass is already running - check GET /api/retention.",
            )
        if wait:
            return job.run_once().as_dict()

        starter = getattr(job, "try_start", None)
        if starter is not None:
            if not starter():
                raise HTTPException(
                    status_code=409,
                    detail="A retention pass is already running - check GET /api/retention.",
                )
            return JSONResponse(
                status_code=202,
                content={"status": "started", "running": True, "poll": "/api/retention"},
            )

        def _run() -> None:
            try:
                job.run_once()
            except Exception:  # noqa: BLE001 - background pass must not kill the app
                logger.exception("manual retention run failed")

        thread = threading.Thread(target=_run, name="yt2abs-retention-manual", daemon=True)
        app.state.retention_run_thread = thread
        thread.start()
        return JSONResponse(
            status_code=202,
            content={
                "status": "started",
                "running": True,
                "poll": "/api/retention",
            },
        )

    @app.get("/api/retention/preview")
    def preview_retention() -> dict[str, Any]:
        """What the next pass would do, computed without any write."""
        job = getattr(app.state, "retention", None)
        if job is None:
            raise HTTPException(
                status_code=409,
                detail="Retention job is disabled (worker disabled or ABS not configured).",
            )
        if job.running:
            raise HTTPException(
                status_code=409,
                detail="A retention pass is running - the preview will be available when it finishes.",
            )
        return job.preview().as_dict()

    @app.get("/api/system")
    def get_system() -> dict[str, Any]:
        """Full diagnostics for the WebUI and the CLI."""
        notifier = getattr(app.state, "notifier", None)
        ntfy = getattr(app.state, "ntfy", None)
        return {
            "version": app.version,
            "health": health_state(),
            "api": {
                "public_url": settings.api.public_url or None,
                "rate_limit_per_minute": settings.api.rate_limit_per_minute,
                "public_rate_limit_per_minute": settings.api.public_rate_limit_per_minute,
                "trust_proxy": settings.api.trust_proxy,
                "trusted_proxies": list(settings.api.trusted_proxies),
            },
            "abs": {
                "url": settings.abs.url,
                "library_id": settings.abs.library_id,
                "library_path": settings.abs.library_path,
                "configured": settings.abs.configured,
            },
            "defaults": {
                "retention_days": settings.defaults.retention_days,
                "protect_unfinished": settings.defaults.protect_unfinished,
            },
            "channels": len(current_config().channels),
            "retention": retention_state(),
            "playlist": playlist_state(),
            "ytdlp": ytdlp_state(live=True),
            "notifications": {
                "enabled": bool(getattr(notifier, "enabled", False)),
                "events": sorted(getattr(notifier, "events", ())),
            },
            "ntfy": {
                "enabled": bool(getattr(ntfy, "enabled", False)),
                "configured": bool(getattr(ntfy, "configured", False)),
                "events": sorted(getattr(ntfy, "events", ())),
            },
            "counts": database.counts(),
        }

    @app.post("/api/ytdlp/update")
    def update_ytdlp() -> dict[str, Any]:
        active = getattr(app.state, "updater", None)
        if active is None:
            raise HTTPException(
                status_code=409,
                detail="yt-dlp updater is disabled (worker disabled).",
            )
        return active.update().as_dict()

    # -------------------------------------------------------- notifications API
    @app.get("/api/notifications")
    def get_notifications() -> dict[str, Any]:
        return notifications_state()

    @app.put("/api/notifications")
    def update_notifications(payload: NotificationSettingsRequest) -> dict[str, Any]:
        requested = [str(item).strip().lower() for item in payload.events]
        unknown = sorted({name for name in requested if name and name not in JOB_EVENTS})
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Unknown events: "
                    + ", ".join(unknown)
                    + ". Allowed: "
                    + ", ".join(JOB_EVENTS)
                    + "."
                ),
            )
        selected = sorted(normalise_events(requested))
        database.set_setting(NOTIFY_EVENTS_SETTING, ",".join(selected))
        active_notifier.set_events(selected)
        notify_source["value"] = "webui"
        logger.info(
            "notification events saved from webui: %s", ",".join(selected) or "none"
        )
        return notifications_state()

    @app.post("/api/notifications/test")
    def send_test_notification() -> JSONResponse:
        if not active_notifier.enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Notifications are disabled - set DISCORD_WEBHOOK_URL "
                    "(and check DISCORD_NOTIFY_DISABLED)."
                ),
            )
        result = active_notifier.send(
            "yt2abs: test notification from the settings panel.",
            title="🔔 yt2abs notification test",
        )
        body = {
            "delivered": bool(result.delivered),
            "status": result.status,
            "error": result.error,
        }
        return JSONResponse(content=body, status_code=200 if result.delivered else 502)

    # ------------------------------------------------------------------ pages
    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, url: str | None = Query(default=None)) -> HTMLResponse:
        if not url:
            return HTMLResponse(
                _page(
                    "Add video",
                    "<p>Missing <code>url</code> parameter. Use the bookmarklet or paste a link "
                    "on the <a href=\"/\">home page</a>.</p>",
                ),
                status_code=400,
            )
        try:
            video_id = extract_video_id(url)
        except InvalidYouTubeURL as exc:
            return HTMLResponse(
                _page("Invalid link", f"<p class=\"error\">{html.escape(str(exc))}</p>"),
                status_code=400,
            )
        # Never requeue here: opening the bookmarklet URL (or refreshing it with
        # F5) must not restart a failed or retention-deleted import. Re-running
        # is an explicit button (and POST /api/jobs/{id}/retry) only.
        row, action = database.create_or_requeue(video_id, requeue=False)
        if action == "created":
            message = "Added to the queue."
        elif row["status"] in ("failed", "deleted"):
            message = (
                "This video is already in the system with the \u201c"
                + STATUS_LABELS.get(str(row["status"]), str(row["status"]))
                + "\u201d status. Retrying the import is a separate action."
            )
        else:
            message = "This video is already in the system - showing its status."
        retry = row["status"] in ("failed", "deleted")
        body = f"""
        <h1>YouTube → Audiobookshelf</h1>
        <p class="{'error' if retry else 'ok'}">{html.escape(message)}</p>
        {_job_details_html(job_public(current_config(), row), retry=retry)}
        <p><a href="/">Home</a> \u00b7 <a href="/jobs/{html.escape(video_id)}">Details</a> · <a href="/api/jobs/{html.escape(video_id)}">JSON</a></p>
        <script>{_JOB_PAGE_JS.replace("__VIDEO_ID__", html.escape(video_id))}</script>
        """
        return HTMLResponse(_page("Video added", body))

    @app.get("/jobs/{video_id}", response_class=HTMLResponse)
    def job_page(video_id: str) -> HTMLResponse:
        """Read-only view of one job; the only action is an explicit retry."""
        if not JOB_ID_RE.fullmatch(video_id):
            raise HTTPException(status_code=404, detail="No such job.")
        row = database.get_video(video_id)
        if row is None:
            return HTMLResponse(
                _page(
                    "Not found",
                    "<p class=\"error\">No such job.</p>"
                    "<p><a href=\"/\">Home</a></p>",
                ),
                status_code=404,
            )
        payload = job_public(current_config(), row)
        retry = row["status"] in ("failed", "deleted")
        retry_hint = (
            "<p class=\"muted\">This video is no longer queued. \u201cRetry import\u201d "
            "will queue it again and download it only if the file is no longer "
            "in the library.</p>"
            if retry
            else ""
        )
        body = f"""
        <h1>Job {html.escape(video_id)}</h1>
        <p class="muted">Read-only view. Changing state is an explicit action below.</p>
        {_job_details_html(payload)}
        {retry_hint}
        <p id="action-result" role="status"></p>
        <p>
          <button type="button" id="retry-button"{'' if retry else ' hidden'}>Retry import</button>
          <a href="/">← Queue</a> · <a href="/api/jobs/{html.escape(video_id)}">JSON</a>
        </p>
        <script>{_JOB_PAGE_JS.replace("__VIDEO_ID__", html.escape(video_id))}</script>
        """
        return HTMLResponse(_page(f"Job {video_id}", body))

    @app.get("/bookmarklet", response_class=HTMLResponse)
    def bookmarklet_page(request: Request) -> HTMLResponse:
        base = (settings.api.public_url or str(request.base_url)).rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            base = str(request.base_url).rstrip("/")
        target = f"{base}/add?url="
        code = (
            "javascript:(()=>{const u=encodeURIComponent(location.href);"
            f"location.href={json.dumps(target)}+u;}})()"
        )
        safe_code = html.escape(code, quote=True)
        body = f"""
        <h1>Bookmarklet \u201cSend to ABS\u201d</h1>
        <p>Drag the button below to the Firefox bookmarks toolbar (or right-click
        \u2192 \u201cBookmark link\u201d). On a YouTube video page click the bookmark:
        the link goes to yt2abs, and the audio appears in Audiobookshelf as a podcast
        episode.</p>
        <p><a class="bookmarklet" href="{safe_code}">▶ Send to ABS</a></p>
        <p>If dragging does not work, create the bookmark manually and paste its address:</p>
        <pre>{html.escape(code)}</pre>
        <h2>Installing in Firefox (Windows)</h2>
        <ol>
          <li>Open this page in Firefox at the HTTPS address of your yt2abs server.</li>
          <li>Show the bookmarks toolbar: <em>View → Toolbars → Bookmarks Toolbar</em>
              (Ctrl+Shift+B).</li>
          <li>Drag the \u201cSend to ABS\u201d button to the bookmarks toolbar.</li>
          <li>Open any YouTube video and click the bookmark. The yt2abs page
              with the download status opens.</li>
          <li>Firefox may ask for permission to open the
              <code>javascript:</code> link — accept it, that is the bookmark content.</li>
        </ol>
        <p>The bookmarklet uses navigation (<code>/add?url=...</code>), not
        <code>fetch</code>, because YouTube blocks cross-origin requests.
        It works only with YouTube links; other addresses get a 4xx error.</p>
        <p><a href="/">← Home</a></p>
        """
        return HTMLResponse(_page("Bookmarklet", body))

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data: https://i.ytimg.com; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page() -> HTMLResponse:
        state = notifications_state()
        if state["enabled"]:
            status = '<p class="ok">Discord delivery: enabled.</p>'
        elif not state["configured"]:
            status = (
                '<p class="error">Delivery disabled: no <code>DISCORD_WEBHOOK_URL</code> '
                "in the app environment. The selection below will be saved, but messages "
                "will not be sent until the webhook is set.</p>"
            )
        else:
            status = (
                '<p class="error">Delivery disabled by <code>DISCORD_NOTIFY_DISABLED</code> '
                "or <code>NOTIFY_DISABLED</code>.</p>"
            )

        source_labels = {
            "webui": "panel (WebUI)",
            "env": "DISCORD_NOTIFY_EVENTS variable",
            "default": "default value",
        }
        source_key = str(state.get("source") or "default")
        boxes = "\n".join(
            f"""        <label class="event">
          <input type="checkbox" name="events" value="{html.escape(item['name'])}"{' checked' if item['enabled'] else ''}>
          <span><strong>{html.escape(item['label'])}</strong> <code>{html.escape(item['name'])}</code><br>
          <small>{html.escape(item['description'])}</small></span>
        </label>"""
            for item in state["events"]
        )
        body = f"""
        <h1>Discord notifications</h1>
        {status}
        <p>Event selection source: <strong>{html.escape(source_labels.get(source_key, source_key))}</strong>.
        Saving in the panel overrides <code>DISCORD_NOTIFY_EVENTS</code> and survives restarts.</p>
        <form id="notify-form">
          <fieldset>
            <legend>Which operations send a notification</legend>
{boxes}
          </fieldset>
          <p>
            <button type="submit">Save</button>
            <button type="button" id="test-button">Send a test notification</button>
          </p>
        </form>
        <p id="result" role="status"></p>
        <p><a href="/">← Home</a></p>
        <script>{_SETTINGS_PAGE_JS}</script>
        """
        return HTMLResponse(_page("Notifications", body))

    app.include_router(
        create_router(
            database=database,
            store=store,
            base_config=settings,
            config_provider=current_config,
            abs_client_provider=abs_provider,
            channel_resolver=channel_resolver
            or (
                lambda reference: resolve_channel_reference(
                    reference,
                    command=settings.ytdlp_command,
                    extra_args=settings.defaults.ytdlp_args,
                )
            ),
            on_config_changed=apply_config,
            retention_provider=lambda: getattr(app.state, "retention", None),
        )
    )

    return app


_CSS = """
body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1f2933}
h1{font-size:1.6rem} a{color:#0b5fff}
input[type=url]{width:min(32rem,100%);padding:.5rem;font-size:1rem}
button{padding:.5rem 1rem;font-size:1rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;border-bottom:1px solid #d9e2ec;padding:.4rem .3rem;vertical-align:top}
dt{font-weight:600;margin-top:.6rem} dd{margin:.1rem 0 0 0}
.status{text-transform:uppercase;font-size:.8rem;letter-spacing:.04em}
.ok{color:#0a7d32}.error{color:#b42318}
code{background:#f0f4f8;padding:.1rem .3rem}
fieldset{border:1px solid #d9e2ec;border-radius:.4rem;padding:.8rem 1rem;max-width:42rem}
legend{padding:0 .3rem;font-weight:600}
.event{display:flex;gap:.6rem;align-items:flex-start;padding:.45rem 0}
.event input{margin-top:.25rem}
.event small{color:#52606d}
.nav{margin:.3rem 0 1.1rem}
a.bookmarklet{display:inline-block;padding:.6rem 1rem;border:2px solid #0b5fff;border-radius:.4rem;
  text-decoration:none;font-weight:600;background:#f0f6ff}
pre{background:#f0f4f8;padding:.7rem;overflow-x:auto;font-size:.8rem}
"""

_ADD_PAGE_JS = """
const videoId = "__VIDEO_ID__";
const labels = {queued:"queued", downloading:"downloading", imported:"imported",
                failed:"failed", deleted:"deleted"};
async function refresh() {
  try {
    const response = await fetch(`/api/jobs/${videoId}`, {cache: "no-store"});
    if (!response.ok) return;
    const job = await response.json();
    document.getElementById("status").textContent = labels[job.status] || job.status;
    document.getElementById("stage").textContent = job.stage || "-";
    document.getElementById("title").textContent = job.title || "-";
    document.getElementById("error").textContent = job.error || "";
    if (job.status === "queued" || job.status === "downloading") {
      setTimeout(refresh, 2000);
    }
  } catch (err) {
    setTimeout(refresh, 5000);
  }
}
setTimeout(refresh, 1000);
"""

_SETTINGS_PAGE_JS = """
const form = document.getElementById("notify-form");
const result = document.getElementById("result");
function show(text, ok) {
  result.textContent = text;
  result.className = ok ? "ok" : "error";
}
function selectedEvents() {
  return [...form.querySelectorAll("input[name=events]:checked")].map((box) => box.value);
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  show("Saving…", true);
  try {
    const response = await fetch("/api/notifications", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({events: selectedEvents()}),
    });
    const data = await response.json();
    if (!response.ok) {
      show("Not saved: " + (data.detail || "HTTP " + response.status), false);
      return;
    }
    const anyEnabled = data.events.some((item) => item.enabled);
    show(
      anyEnabled
        ? "Saved. Selected operations will send Discord notifications."
        : "Saved. Job notifications are disabled.",
      true
    );
  } catch (err) {
    show("Network error: " + err, false);
  }
});
document.getElementById("test-button").addEventListener("click", async () => {
  show("Sending…", true);
  try {
    const response = await fetch("/api/notifications/test", {method: "POST"});
    const data = await response.json();
    if (response.ok && data.delivered) {
      show("Test notification delivered (HTTP " + data.status + "). Check the Discord channel.", true);
      return;
    }
    show("Failed: " + (data.error || data.detail || "HTTP " + response.status), false);
  } catch (err) {
    show("Network error: " + err, false);
  }
});
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · yt2abs</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


_JOB_PAGE_JS = """
const VIDEO_ID = "__VIDEO_ID__";
const button = document.getElementById("retry-button");
const result = document.getElementById("action-result");
function show(text, ok) {
  result.textContent = text;
  result.className = ok ? "ok" : "error";
}
async function postJson(path, body) {
  const options = {method: "POST", cache: "no-store", headers: {Accept: "application/json"}};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  return {ok: response.ok, status: response.status, data: data};
}
if (button) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    show("Retrying…", true);
    try {
      let answer = await postJson(`/api/jobs/${encodeURIComponent(VIDEO_ID)}/retry`);
      if (answer.status === 409) {
        // A deleted job has no episode to retry: queue the video explicitly.
        answer = await postJson("/api/jobs", {
          url: `https://www.youtube.com/watch?v=${VIDEO_ID}`,
        });
      }
      if (!answer.ok) {
        const detail =
          answer.data && typeof answer.data === "object" && answer.data.detail
            ? answer.data.detail
            : "HTTP " + answer.status;
        show("Failed: " + detail, false);
        button.disabled = false;
        return;
      }
      const status = document.getElementById("status");
      if (status) status.textContent = "queued";
      show("Added to the queue. Refresh the page to see the progress.", true);
      button.hidden = true;
    } catch (err) {
      show("Network error: " + err.message, false);
      button.disabled = false;
    }
  });
}
"""


def _job_details_html(payload: Mapping[str, Any], *, retry: bool = False) -> str:
    """Read-only definition list shared by ``/add`` and ``/jobs/{video_id}``."""
    video_id = str(payload.get("video_id") or "")
    url = str(payload.get("url") or (canonical_url(video_id) if video_id else "#"))
    status = str(payload.get("status") or "")
    retention = payload.get("retention") or {}
    if retention.get("keep"):
        retention_text = "no deadline (keep)"
    elif retention.get("deadline"):
        retention_text = f"{retention['deadline']} ({retention.get('days')} dni)"
    else:
        retention_text = "-"
    return f"""
        <dl>
          <dt>Video</dt><dd><a href="{html.escape(url)}">{html.escape(video_id)}</a></dd>
          <dt>Status</dt><dd><span id="status" class="status">{html.escape(STATUS_LABELS.get(status, status))}</span></dd>
          <dt>Stage</dt><dd id="stage">{html.escape(str(payload.get("stage") or "-"))}</dd>
          <dt>Title</dt><dd id="title">{html.escape(str(payload.get("title") or "-"))}</dd>
          <dt>Channel</dt><dd id="channel">{html.escape(str(payload.get("channel") or "-"))}</dd>
          <dt>Retention deadline</dt><dd id="retention">{html.escape(retention_text)}</dd>
          <dt>Error</dt><dd id="error" class="error">{html.escape(str(payload.get("error") or ""))}</dd>
        </dl>"""
