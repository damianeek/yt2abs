"""WebUI JSON API: job actions, channel policies and the live ABS status.

The HTML/JS frontend is served from ``yt2abs/static``; this module only adds
the endpoints it needs.  Keeping the routes here keeps ``app.py`` focused on
the worker/retention lifespan.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Literal, Mapping

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .abs_client import AbsClient, AbsError
from .channels import (
    INHERIT,
    MAX_RETENTION_DAYS,
    UNSET,
    ChannelInputError,
    ChannelPolicyStore,
    channel_payload,
)
from .config import Config
from .db import Database, as_public
from .retention import (
    RetentionActionError,
    RetentionJob,
    parse_timestamp,
    resolve_retention_days,
)
from .sponsorblock import format_setting

logger = logging.getLogger(__name__)


class ChannelCreateRequest(BaseModel):
    channel: str = Field(..., description="Channel ID (UC...) or YouTube link")
    name: str | None = Field(default=None, description="Display name")
    retention_days: int | Literal["default"] | None = Field(
        default="default",
        description="Number of days, null = keep forever, 'default' = channel policy",
    )
    sponsorblock: str | None = Field(
        default=None, description="off | remove | mark | mute | <mode>:<categories>"
    )
    protect_unfinished: bool | None = None


class ChannelUpdateRequest(BaseModel):
    name: str | None = None
    retention_days: int | Literal["default"] | None = None
    sponsorblock: str | None = None
    protect_unfinished: bool | None = None


class JobRetentionRequest(BaseModel):
    override: int | Literal["default"] | None = Field(
        default=None,
        description=(
            "Retention days (0..36500); 0 = keep forever, null/\"default\" = "
            "return to the channel policy. The field is required so an empty body "
            "cannot wipe the setting by accident."
        ),
    )


def retention_details(config: Config, row: Mapping[str, Any]) -> dict[str, Any]:
    """Effective retention for one job row, including its deadline."""
    override = row.get("retention_override")
    channel_id = str(row.get("channel_id") or "")
    if override is not None:
        try:
            source = "keep" if int(override) == 0 else "override"
        except (TypeError, ValueError):
            source = "override"
    elif channel_id and channel_id in config.channels:
        source = "channel"
    else:
        source = "defaults"
    days = resolve_retention_days(config, row)
    deadline: str | None = None
    if days is not None:
        added_at = parse_timestamp(row.get("added_at"))
        if added_at is not None:
            try:
                deadline = (added_at + timedelta(days=days)).isoformat().replace(
                    "+00:00", "Z"
                )
            except (OverflowError, ValueError):
                deadline = None
    return {
        "days": days,
        "keep": days is None,
        "due_now": bool(days == 0),
        "source": source,
        "override": override,
        "deadline": deadline,
    }


def job_public(
    config: Config,
    row: Mapping[str, Any],
    *,
    created: bool = False,
    requeued: bool = False,
) -> dict[str, Any]:
    """Serialise one job for the API and the WebUI (adds retention details)."""
    payload = as_public(row)
    payload["retention"] = retention_details(config, row)
    payload["created"] = created
    payload["requeued"] = requeued
    return payload


def create_router(
    *,
    database: Database,
    store: ChannelPolicyStore,
    base_config: Config,
    config_provider: Callable[[], Config],
    abs_client_provider: Callable[[], AbsClient],
    channel_resolver: Callable[[str], str],
    on_config_changed: Callable[[Config], None],
    retention_provider: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter()

    def config() -> Config:
        return config_provider()

    def refresh_config() -> Config:
        effective = store.apply(base_config)
        on_config_changed(effective)
        return effective

    def job(row: Mapping[str, Any]) -> dict[str, Any]:
        return job_public(config(), row)

    def channel_item(channel_id: str) -> dict[str, Any] | None:
        effective = config()
        overrides = store.load()
        db_row = database.get_channel(channel_id)
        if (
            channel_id not in effective.channels
            and channel_id not in overrides
            and db_row is None
        ):
            return None
        return channel_payload(
            effective,
            channel_id,
            override=overrides.get(channel_id),
            in_config=channel_id in base_config.channels,
            db_row=db_row,
            base_config=base_config,
        )

    # ------------------------------------------------------------------ jobs
    @router.post("/api/jobs/{video_id}/retry")
    def retry_job(video_id: str) -> dict[str, Any]:
        row = database.get_video(video_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such job.")
        if row["status"] != "failed":
            raise HTTPException(
                status_code=409,
                detail="Only a job with the \u201cfailed\u201d status can be retried.",
            )
        updated, action = database.create_or_requeue(video_id)
        logger.info("webui: retried %s (%s)", video_id, action)
        return job(updated)

    @router.post("/api/jobs/{video_id}/retention")
    def set_job_retention(video_id: str, payload: JobRetentionRequest) -> dict[str, Any]:
        if "override" not in payload.model_fields_set:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Provide the override field: number of days (0 = keep forever) or "
                    "null/\"default\" (return to the channel policy)."
                ),
            )
        row = database.get_video(video_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such job.")
        if row["status"] == "deleted":
            raise HTTPException(
                status_code=409,
                detail="The job is already deleted - add the video again to change retention.",
            )
        override = None if payload.override in (None, "default") else int(payload.override)
        if override is not None and not 0 <= override <= MAX_RETENTION_DAYS:
            raise HTTPException(
                status_code=422,
                detail=f"override must be in the 0..{MAX_RETENTION_DAYS} day range.",
            )
        updated = database.update_video(video_id, retention_override=override)
        logger.info("webui: retention override for %s set to %r", video_id, override)
        return job(updated or row)

    @router.delete("/api/jobs/{video_id}")
    def delete_job(video_id: str) -> dict[str, Any]:
        row = database.get_video(video_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such job.")
        if row["status"] == "deleted":
            return job(row)
        job_runner = None
        if retention_provider is not None:
            candidate = retention_provider()
            if candidate is not None and hasattr(candidate, "delete_video"):
                job_runner = candidate
        if job_runner is None:
            # Worker disabled (or injected fake): use a one-shot job with its
            # own lock; the regular retention thread is not running then.
            job_runner = RetentionJob(config(), database, abs_client_provider())
        try:
            updated = job_runner.delete_video(video_id)
        except KeyError as exc:  # pragma: no cover - checked above
            raise HTTPException(status_code=404, detail="No such job.") from exc
        except RetentionActionError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not delete the episode from Audiobookshelf: {exc}",
            ) from exc
        logger.info("webui: deleted %s", video_id)
        return job(updated)

    # -------------------------------------------------------------- channels
    @router.get("/api/channels")
    def list_channels() -> dict[str, Any]:
        effective = config()
        overrides = store.load()
        db_rows = {
            str(row.get("channel_id")): row
            for row in database.list_channels()
            if row.get("channel_id")
        }
        ids = list(effective.channels)
        for channel_id in db_rows:
            if channel_id not in ids:
                ids.append(channel_id)
        items = [
            channel_payload(
                effective,
                channel_id,
                override=overrides.get(channel_id),
                in_config=channel_id in base_config.channels,
                db_row=db_rows.get(channel_id),
                base_config=base_config,
            )
            for channel_id in ids
        ]
        items.sort(key=lambda item: (str(item["name"]).casefold(), item["channel_id"]))
        defaults = effective.defaults
        return {
            "channels": items,
            "defaults": {
                "retention_days": defaults.retention_days,
                "protect_unfinished": defaults.protect_unfinished,
                "sponsorblock": format_setting(defaults.sponsorblock),
            },
            "overrides_count": len(overrides),
        }

    @router.post("/api/channels")
    def add_channel(payload: ChannelCreateRequest) -> JSONResponse:
        try:
            channel_id = channel_resolver(payload.channel)
        except ChannelInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        effective = config()
        existed = bool(
            channel_id in effective.channels
            or channel_id in store.load()
            or database.get_channel(channel_id) is not None
        )
        fields = payload.model_fields_set
        name: Any = UNSET
        if "name" in fields:
            name = INHERIT if not (payload.name or "").strip() else payload.name
        retention: Any = UNSET
        if "retention_days" in fields:
            retention = (
                INHERIT
                if payload.retention_days == "default"
                else payload.retention_days
            )
        sponsorblock: Any = UNSET
        if "sponsorblock" in fields:
            sponsorblock = (
                INHERIT if payload.sponsorblock is None else payload.sponsorblock
            )
        protect: Any = UNSET
        if "protect_unfinished" in fields:
            protect = (
                INHERIT
                if payload.protect_unfinished is None
                else payload.protect_unfinished
            )
        try:
            store.upsert(
                channel_id,
                name=name,
                retention_days=retention,
                sponsorblock=sponsorblock,
                protect_unfinished=protect,
            )
        except ChannelInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refresh_config()
        item = channel_item(channel_id)
        if item is None:  # pragma: no cover - the override was just created
            raise HTTPException(status_code=500, detail="Could not save the channel.")
        logger.info("webui: added channel %s (existing=%s)", channel_id, existed)
        return JSONResponse(
            {"created": not existed, "channel": item},
            status_code=200 if existed else 201,
        )

    @router.put("/api/channels/{channel_id}")
    def update_channel(channel_id: str, payload: ChannelUpdateRequest) -> dict[str, Any]:
        current = channel_item(channel_id)
        if current is None:
            raise HTTPException(status_code=404, detail="No such channel.")
        fields = payload.model_fields_set
        changes: dict[str, Any] = {}
        if "name" in fields:
            changes["name"] = (
                INHERIT if not (payload.name or "").strip() else payload.name
            )
        if "retention_days" in fields:
            changes["retention_days"] = (
                INHERIT
                if payload.retention_days == "default"
                else payload.retention_days
            )
        if "sponsorblock" in fields:
            changes["sponsorblock"] = (
                INHERIT if payload.sponsorblock is None else payload.sponsorblock
            )
        if "protect_unfinished" in fields:
            changes["protect_unfinished"] = (
                INHERIT
                if payload.protect_unfinished is None
                else payload.protect_unfinished
            )
        if not changes:
            return current
        try:
            store.upsert(channel_id, **changes)
        except ChannelInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refresh_config()
        updated = channel_item(channel_id)
        if updated is None:  # pragma: no cover - the channel existed a moment ago
            raise HTTPException(status_code=404, detail="No such channel.")
        logger.info("webui: updated channel %s: %s", channel_id, sorted(changes))
        return updated

    @router.delete("/api/channels/{channel_id}")
    def reset_channel(channel_id: str) -> dict[str, Any]:
        if not store.clear(channel_id):
            raise HTTPException(
                status_code=404,
                detail="This channel has no change saved in the panel.",
            )
        refresh_config()
        return {"removed": True, "channel": channel_item(channel_id)}

    # ------------------------------------------------------------- ABS status
    @router.get("/api/abs/status")
    def abs_status() -> dict[str, Any]:
        settings = base_config.abs
        payload: dict[str, Any] = {
            "configured": settings.configured,
            "url": settings.url,
            "library_id": settings.library_id,
            "library_path": settings.library_path,
            "reachable": False,
            "version": None,
            "is_scanning": None,
            "error": None,
        }
        if not settings.configured:
            payload["error"] = "Audiobookshelf is not configured (url/key/library)."
            return payload
        client = abs_client_provider()
        try:
            status = client.server_status()
        except AbsError as exc:
            payload["error"] = str(exc)
            return payload
        version = status.get("serverVersion") or status.get("version")
        payload.update(
            reachable=True,
            version=str(version) if version else None,
            is_scanning=status.get("isScanning"),
        )
        return payload

    return router

