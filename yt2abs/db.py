"""SQLite persistence: the durable job queue and per-channel state."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

STATUSES = ("queued", "downloading", "imported", "failed", "deleted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id           TEXT PRIMARY KEY,
  channel_id         TEXT NOT NULL DEFAULT '',
  channel_title      TEXT,
  title              TEXT NOT NULL DEFAULT '',
  description        TEXT,
  published_at       TEXT,
  status             TEXT NOT NULL,
  stage              TEXT,
  file_path          TEXT,
  abs_item_id        TEXT,
  abs_episode_id     TEXT,
  added_at           TEXT NOT NULL,
  retention_override INTEGER,
  deleted_at         TEXT,
  error              TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  next_attempt_at    TEXT,
  source             TEXT NOT NULL DEFAULT 'api',
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_queue
  ON videos(status, next_attempt_at, added_at);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);

CREATE TABLE IF NOT EXISTS channels (
  channel_id       TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  folder_name      TEXT NOT NULL,
  cover_written_at TEXT,
  created_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_folder ON channels(folder_name);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

# Columns callers are allowed to update through Database.update_video().
_UPDATABLE = frozenset(
    {
        "channel_id",
        "channel_title",
        "title",
        "description",
        "published_at",
        "status",
        "stage",
        "file_path",
        "abs_item_id",
        "abs_episode_id",
        "added_at",
        "retention_override",
        "deleted_at",
        "error",
        "attempts",
        "next_attempt_at",
    }
)


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(seconds: float, *, now: datetime | None = None) -> str:
    """ISO-8601 timestamp ``seconds`` in the future."""
    base = now or datetime.now(timezone.utc)
    moment = base + timedelta(seconds=seconds)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    """Thin SQLite wrapper; every operation opens its own connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Bring a database created by an older version up to the schema.

        ``CREATE TABLE IF NOT EXISTS`` never adds columns, so additive changes
        need an explicit ``ALTER TABLE`` for existing volumes.
        """
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(videos)")
        }
        if "source" not in columns:
            connection.execute(
                "ALTER TABLE videos ADD COLUMN source TEXT NOT NULL DEFAULT 'api'"
            )

    # ------------------------------------------------------------------ videos
    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_videos(
        self, *, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM videos"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY added_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_by_status(
        self, status: str, *, limit: int | None = None, newest_first: bool = False
    ) -> list[dict[str, Any]]:
        """All videos in ``status`` (retention needs every imported row).

        Unlike :meth:`list_videos` this is not capped at 500 rows; pass a
        ``limit`` when the caller only needs a batch.
        """
        query = "SELECT * FROM videos WHERE status = ?"
        order = "DESC" if newest_first else "ASC"
        query += f" ORDER BY added_at {order}, video_id ASC"
        params: list[Any] = [str(status)]
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def create_or_requeue(
        self,
        video_id: str,
        *,
        retention_override: int | None = None,
        requeue: bool = True,
    ) -> tuple[dict[str, Any], str]:
        """Insert a job or return the existing one.

        Returns ``(row, action)`` where action is ``created``, ``existing`` or
        ``requeued`` (a previously failed/deleted video starts over).  With
        ``requeue=False`` (the ``/add`` bookmarklet page) a failed or deleted
        job is reported as ``existing`` and left untouched: re-running an
        import must be an explicit action, never a side effect of opening or
        refreshing a URL.

        ``file_path`` survives a requeue of a *failed* job so the pipeline can
        reuse a file that was already published before an ABS import failed.
        A *deleted* row loses it: retention removed the audio from disk, so the
        path is stale and the next attempt must download again. The pipeline
        also ignores the path when the file turns out to be gone.
        """
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM videos WHERE video_id = ?", (video_id,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO videos
                          (video_id, status, added_at, updated_at, retention_override, attempts)
                        VALUES (?, 'queued', ?, ?, ?, 0)
                        """,
                        (video_id, now, now, retention_override),
                    )
                    action = "created"
                elif row["status"] in ("queued", "downloading", "imported") or not requeue:
                    action = "existing"
                    if (
                        retention_override is not None
                        and retention_override != row["retention_override"]
                    ):
                        connection.execute(
                            "UPDATE videos SET retention_override = ?, updated_at = ? "
                            "WHERE video_id = ?",
                            (retention_override, now, video_id),
                        )
                else:  # failed / deleted -> start over
                    action = "requeued"
                    connection.execute(
                        """
                        UPDATE videos SET
                          status = 'queued',
                          stage = NULL,
                          attempts = 0,
                          error = NULL,
                          next_attempt_at = NULL,
                          deleted_at = NULL,
                          abs_item_id = NULL,
                          abs_episode_id = NULL,
                          file_path = CASE WHEN status = 'deleted' THEN NULL ELSE file_path END,
                          added_at = ?,
                          updated_at = ?,
                          retention_override = COALESCE(?, retention_override)
                        WHERE video_id = ?
                        """,
                        (now, now, retention_override, video_id),
                    )
                current = connection.execute(
                    "SELECT * FROM videos WHERE video_id = ?", (video_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(current), action

    def queue_new(
        self, video_id: str, *, source: str = "playlist"
    ) -> tuple[dict[str, Any] | None, bool]:
        """Queue a video only when its id is completely unknown.

        This is the playlist-poller path: unlike :meth:`create_or_requeue` it
        never touches an existing row, so a video that is ``failed`` or was
        ``deleted`` (by retention or by hand) is not resurrected by the next
        poll.  ``source`` records the *first* way a video entered the queue;
        a later manual retry/requeue keeps the original value.  Returns
        ``(row, created)``.
        """
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO videos
                  (video_id, status, added_at, updated_at, attempts, source)
                VALUES (?, 'queued', ?, ?, 0, ?)
                ON CONFLICT(video_id) DO NOTHING
                """,
                (video_id, now, now, source),
            )
            created = cursor.rowcount > 0
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return (dict(row) if row else None), created

    def claim_next(self, *, now: str | None = None) -> dict[str, Any] | None:
        """Atomically pick the oldest due job and mark it as downloading."""
        moment = now or utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM videos
                    WHERE status = 'queued'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY added_at ASC, video_id ASC
                    LIMIT 1
                    """,
                    (moment,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    """
                    UPDATE videos
                    SET status = 'downloading',
                        stage = 'starting',
                        attempts = attempts + 1,
                        error = NULL,
                        updated_at = ?
                    WHERE video_id = ? AND status = 'queued'
                    """,
                    (moment, row["video_id"]),
                )
                claimed = connection.execute(
                    "SELECT * FROM videos WHERE video_id = ?", (row["video_id"],)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(claimed) if claimed else None

    def update_video(self, video_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update columns: {sorted(unknown)}")
        if not fields:
            return self.get_video(video_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [video_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE videos SET {assignments} WHERE video_id = ?", values
            )
        return self.get_video(video_id)

    def set_stage(self, video_id: str, stage: str) -> None:
        self.update_video(video_id, stage=stage)

    def mark_imported(self, video_id: str, **fields: Any) -> dict[str, Any] | None:
        payload = dict(fields)
        payload.update(status="imported", stage=None, error=None, next_attempt_at=None)
        unknown = set(payload) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update columns: {sorted(unknown)}")
        payload["updated_at"] = utc_now()
        assignments = ", ".join(f"{name} = ?" for name in payload)
        values = list(payload.values()) + [video_id]
        with self.connect() as connection:
            # WebUI deletion wins even when it happened while the worker was
            # finishing: never resurrect a row the user removed.
            connection.execute(
                f"UPDATE videos SET {assignments} WHERE video_id = ? AND status <> 'deleted'",
                values,
            )
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return dict(row) if row else None

    def schedule_retry(self, video_id: str, error: str, delay_seconds: float) -> None:
        self.update_video(
            video_id,
            status="queued",
            stage=None,
            error=error[:2000],
            next_attempt_at=utc_after(delay_seconds),
        )

    def mark_failed(self, video_id: str, error: str) -> None:
        self.update_video(
            video_id,
            status="failed",
            stage=None,
            error=error[:2000],
            next_attempt_at=None,
        )

    def mark_deleted(
        self,
        video_id: str,
        *,
        deleted_at: str | None = None,
        reason: str | None = None,
        only_status: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a video as gone (retention or ABS reconcile).

        ``only_status`` makes the update conditional (``imported`` for the
        automated retention/reconcile paths), so a row that the worker has
        already moved on cannot be overwritten by a stale pass.
        """
        now = utc_now()
        fields = {
            "status": "deleted",
            "stage": None,
            "deleted_at": deleted_at or now,
            "error": reason[:2000] if reason else None,
            "next_attempt_at": None,
            "updated_at": now,
        }
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values: list[Any] = list(fields.values()) + [video_id]
        query = f"UPDATE videos SET {assignments} WHERE video_id = ?"
        if only_status is not None:
            query += " AND status = ?"
            values.append(str(only_status))
        with self.connect() as connection:
            connection.execute(query, values)
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return dict(row) if row else None

    def reset_inflight(self) -> int:
        """Move jobs stuck in ``downloading`` (crash/restart) back to the queue."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE videos
                SET status = 'queued', stage = NULL, updated_at = ?
                WHERE status = 'downloading'
                """,
                (utc_now(),),
            )
            return cursor.rowcount

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS amount FROM videos GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({row["status"]: row["amount"] for row in rows})
        return counts

    # ---------------------------------------------------------------- channels
    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        if not channel_id:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return dict(row) if row else None

    def folder_in_use(self, folder_name: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT channel_id FROM channels WHERE folder_name = ?", (folder_name,)
            ).fetchone()
        return row["channel_id"] if row else None

    def save_channel(self, channel_id: str, title: str, folder_name: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO channels (channel_id, title, folder_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                  title = excluded.title,
                  folder_name = excluded.folder_name
                """,
                (channel_id, title, folder_name, now),
            )
            row = connection.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return dict(row)

    def list_channels(self) -> list[dict[str, Any]]:
        """All channels the pipeline has seen (folder names, titles)."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM channels ORDER BY title COLLATE NOCASE ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def channel_ids(self) -> dict[str, int]:
        """``{channel_id: rows}`` for every channel present in ``videos``.

        ``yt2abs check`` uses it to point at config entries that match no row
        (a typo like ``UC_keep`` silently falls back to ``defaults``).
        """
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT channel_id, COUNT(*) AS total FROM videos "
                "WHERE channel_id <> '' GROUP BY channel_id"
            ).fetchall()
        return {str(row["channel_id"]): int(row["total"]) for row in rows}

    def mark_cover_written(self, channel_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE channels SET cover_written_at = ? WHERE channel_id = ?",
                (utc_now(), channel_id),
            )

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str) -> str | None:
        """Return a stored setting, or ``None`` when it was never written."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (str(key),)
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Insert or replace a setting (used by the WebUI for live config)."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (str(key), str(value), utc_now()),
            )

    def delete_setting(self, key: str) -> None:
        """Remove a setting so the environment/default value applies again."""
        with self.connect() as connection:
            connection.execute("DELETE FROM settings WHERE key = ?", (str(key),))


def as_public(job: Mapping[str, Any]) -> dict[str, Any]:
    """Serialise a job row for the API (adds the canonical URL, drops nothing)."""
    from .urls import canonical_url

    payload = dict(job)
    video_id = str(payload.get("video_id") or "")
    payload["url"] = canonical_url(video_id) if video_id else None
    return payload
