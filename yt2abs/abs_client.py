"""Thin Audiobookshelf API client (2.37.x).

Only the calls needed by yt2abs are implemented: library/item scans, lookup by
path and file name, episode/podcast metadata edits and hard-delete of an
episode together with its audio file.

The HTTP layer is a small injectable ``sender`` so tests never touch the
network. Secrets (the API key) are only sent in the Authorization header and
never logged.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

Sender = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]


class AbsError(RuntimeError):
    """Non-successful Audiobookshelf API response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AbsNotFound(AbsError):
    """Requested Audiobookshelf object does not exist."""


class AbsConnectionError(AbsError):
    """Network/transport failure while talking to Audiobookshelf."""


def _urlopen_sender(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, *, timeout: float
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - http(s) only
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise AbsConnectionError(f"ABS connection error: {exc}") from exc


def _media_episodes(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    media = item.get("media") or {}
    episodes = media.get("episodes") or media.get("podcastEpisodes") or []
    return [dict(episode) for episode in episodes if isinstance(episode, Mapping)]


@dataclass(frozen=True)
class EpisodeProgress:
    """One user's playback progress for a podcast episode (ABS ``mediaProgress``)."""

    current_time: float = 0.0
    duration: float = 0.0
    progress: float = 0.0
    is_finished: bool = False
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def started(self) -> bool:
        """True when playback began (even if it is currently at 0:00)."""
        return bool(self.started_at) or self.current_time > 0 or self.progress > 0

    @property
    def unfinished(self) -> bool:
        return self.started and not self.is_finished


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _progress_from_entry(entry: Mapping[str, Any]) -> EpisodeProgress:
    """Build :class:`EpisodeProgress` from an ABS ``mediaProgress`` object."""
    return EpisodeProgress(
        current_time=_float(entry.get("currentTime")),
        duration=_float(entry.get("duration")),
        progress=_float(entry.get("progress")),
        is_finished=bool(entry.get("isFinished")),
        started_at=str(entry.get("startedAt") or "") or None,
        finished_at=str(entry.get("finishedAt") or "") or None,
    )


@dataclass
class AbsClient:
    base_url: str
    api_key: str
    timeout: float = 30.0
    sender: Sender | None = None
    progress_user: str = ""
    _extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    # ---------------------------------------------------------------- transport
    def _send(self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
              body: Mapping[str, Any] | None = None, parse_json: bool = True) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "yt2abs/0.1",
            **self._extra_headers,
        }
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if self.sender is not None:
            status, raw = self.sender(method, url, headers, payload)
        else:
            status, raw = _urlopen_sender(method, url, headers, payload, timeout=self.timeout)

        text = raw.decode("utf-8", errors="replace") if raw else ""
        if status == 404:
            raise AbsNotFound(f"ABS {method} {path} -> 404", status_code=404)
        if status >= 400:
            raise AbsError(
                f"ABS {method} {path} -> {status}: {text[:300]}", status_code=status
            )
        if not text:
            return None
        if not parse_json:
            return text
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AbsError(f"ABS {method} {path} returned invalid JSON") from exc

    # ------------------------------------------------------------------ library
    def server_status(self) -> dict[str, Any]:
        """Return the public ``/status`` document (server version, etc.)."""
        response = self._send("GET", "/status")
        if not isinstance(response, Mapping):
            raise AbsError("ABS /status: unexpected response")
        return dict(response)

    def scan_library(self, library_id: str) -> Any:
        # ABS 2.37.1 answers with text/plain "OK", not JSON.
        return self._send(
            "POST",
            f"/api/libraries/{quote(library_id, safe='')}/scan",
            parse_json=False,
        )

    def get_library(self, library_id: str) -> dict[str, Any]:
        """Return one library; used as a cheap preflight before downloading."""
        response = self._send("GET", f"/api/libraries/{quote(library_id, safe='')}")
        if not isinstance(response, Mapping):
            raise AbsError(f"ABS library {library_id}: unexpected response")
        return dict(response)

    def scan_item(self, item_id: str) -> Any:
        return self._send(
            "POST", f"/api/items/{quote(item_id, safe='')}/scan", parse_json=False
        )

    def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        response = self._send(
            "GET", f"/api/libraries/{quote(library_id, safe='')}/items", query={"limit": 500}
        )
        if isinstance(response, Mapping):
            results = response.get("results")
            if not isinstance(results, list):
                # A 200 without "results" is not a valid listing (wrong URL,
                # proxy page, changed API): treat it as an error, never as "the
                # whole library is empty".
                raise AbsError("ABS library listing: response has no results list")
        elif isinstance(response, list):
            results = response
        else:
            raise AbsError("ABS library listing: unexpected response")
        return [dict(item) for item in results if isinstance(item, Mapping)]

    def get_item(self, item_id: str, *, expanded: bool = True) -> dict[str, Any]:
        response = self._send(
            "GET",
            f"/api/items/{quote(item_id, safe='')}",
            query={"expanded": 1} if expanded else None,
        )
        if not isinstance(response, Mapping):
            raise AbsError(f"ABS item {item_id}: unexpected response")
        return dict(response)

    def find_item_by_path(
        self, library_id: str, path: str, *, items: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        target = os.path.normpath(path)
        candidates = items if items is not None else self.list_library_items(library_id)
        for item in candidates:
            item_path = item.get("path")
            if isinstance(item_path, str) and os.path.normpath(item_path) == target:
                return dict(item)
        return None

    # ----------------------------------------------------------------- episodes
    @staticmethod
    def episodes(item: Mapping[str, Any]) -> list[dict[str, Any]]:
        return _media_episodes(item)

    def get_episodes(self, item_id: str) -> list[dict[str, Any]]:
        return _media_episodes(self.get_item(item_id))

    def find_episodes_by_filename(
        self, item_id: str, filename: str, *, item: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """All episodes whose audio file name matches ``filename``.

        More than one match means the ABS item has duplicate episodes with the
        same file name (possible after repeated manual imports); callers must
        not guess which one is "the" episode in that case.
        """
        target = os.path.basename(filename)
        payload = item if item is not None else self.get_item(item_id)
        matches: list[dict[str, Any]] = []
        for episode in _media_episodes(payload):
            audio = episode.get("audioFile") or {}
            metadata = audio.get("metadata") or {}
            candidates = (
                metadata.get("filename"),
                os.path.basename(str(metadata.get("path") or "")),
                os.path.basename(str(audio.get("path") or "")),
            )
            if any(candidate and candidate == target for candidate in candidates):
                matches.append(dict(episode))
        return matches

    def find_episode_by_filename(
        self, item_id: str, filename: str, *, item: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        matches = self.find_episodes_by_filename(item_id, filename, item=item)
        return matches[0] if matches else None

    def get_episode(self, item_id: str, episode_id: str) -> dict[str, Any]:
        response = self._send(
            "GET",
            f"/api/podcasts/{quote(item_id, safe='')}/episode/{quote(episode_id, safe='')}",
        )
        if not isinstance(response, Mapping):
            raise AbsError("ABS episode: unexpected response")
        return dict(response)

    @staticmethod
    def find_episode_by_id(
        item: Mapping[str, Any], episode_id: str
    ) -> dict[str, Any] | None:
        for episode in _media_episodes(item):
            if str(episode.get("id") or "") == episode_id:
                return episode
        return None

    def get_media_progress(
        self, item_id: str, episode_id: str
    ) -> EpisodeProgress | None:
        """Playback progress of the API-key user for one episode.

        ABS returns 404 when nothing was ever played, which is not an error:
        the caller gets ``None`` (meaning "not started").
        """
        try:
            response = self._send(
                "GET",
                f"/api/me/progress/{quote(item_id, safe='')}/{quote(episode_id, safe='')}",
            )
        except AbsNotFound:
            return None
        if not isinstance(response, Mapping):
            return None
        return _progress_from_entry(response)

    def list_users(self) -> list[dict[str, Any]]:
        """All ABS users (admin key required; the list has no mediaProgress)."""
        response = self._send("GET", "/api/users")
        if isinstance(response, Mapping):
            users = response.get("users")
            if not isinstance(users, list):
                raise AbsError("ABS /api/users: response has no user list")
        elif isinstance(response, list):
            users = response
        else:
            raise AbsError("ABS /api/users: unexpected response")
        return [dict(user) for user in users if isinstance(user, Mapping)]

    def user_progress(self, user_id: str) -> list[dict[str, Any]]:
        """One user's ``mediaProgress`` entries (``GET /api/users/{id}``)."""
        response = self._send("GET", f"/api/users/{quote(user_id, safe='')}")
        if not isinstance(response, Mapping):
            raise AbsError(f"ABS user {user_id}: unexpected response")
        entries = response.get("mediaProgress") or []
        return [dict(entry) for entry in entries if isinstance(entry, Mapping)]

    @staticmethod
    def _progress_map_from_entries(
        entries: Any,
    ) -> dict[tuple[str, str], EpisodeProgress]:
        result: dict[tuple[str, str], EpisodeProgress] = {}
        if not isinstance(entries, list):
            return result
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            item_id = str(entry.get("libraryItemId") or "")
            if not item_id:
                continue
            result[(item_id, str(entry.get("episodeId") or ""))] = _progress_from_entry(entry)
        return result

    def _user_ids(self) -> list[tuple[str, str]]:
        """``(id, username)`` of every ABS user, for ``abs.progress_user`` lookup."""
        return [
            (str(user.get("id") or ""), str(user.get("username") or ""))
            for user in self.list_users()
            if user.get("id")
        ]

    def media_progress_map(
        self, user: str | None = None
    ) -> dict[tuple[str, str], EpisodeProgress]:
        """Playback progress of one ABS account, keyed by ``(item, episode)``.

        ``user`` may be a user id or a username; an empty value means the owner
        of the API key (``/api/me``). Reading another account requires an admin
        key, which is exactly what makes ``protect_unfinished`` cover every
        listener instead of only the service account.
        """
        wanted = (user or "").strip()
        if not wanted:
            response = self._send("GET", "/api/me")
            if not isinstance(response, Mapping):
                raise AbsError("ABS /api/me: unexpected response")
            entries = response.get("mediaProgress")
            return self._progress_map_from_entries(entries)

        match_id = ""
        for user_id, username in self._user_ids():
            if wanted in (user_id, username) or wanted.casefold() == username.casefold():
                match_id = user_id
                break
        if not match_id:
            raise AbsError(f"ABS user {wanted!r} not found")
        return self._progress_map_from_entries(self.user_progress(match_id))

    def all_media_progress(self) -> dict[tuple[str, str], EpisodeProgress]:
        """Merged progress of every ABS user (admin key required).

        Audiobookshelf returns only bare user objects from ``/api/users``, so
        every account needs one ``/api/users/{id}`` call. When two listeners
        disagree, the unfinished entry wins: the goal is to protect an episode
        somebody is still listening to, not to report exact playback state.
        """
        merged: dict[tuple[str, str], EpisodeProgress] = {}
        users = self._user_ids()
        if not users:
            raise AbsError("ABS /api/users returned no users")
        for user_id, _username in users:
            for key, entry in self._progress_map_from_entries(self.user_progress(user_id)).items():
                current = merged.get(key)
                if current is None or (entry.unfinished and not current.unfinished):
                    merged[key] = entry
        return merged

    def update_episode(
        self,
        item_id: str,
        episode_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        pub_date: str | None = None,
        published_at_ms: int | None = None,
        **extra: Any,
    ) -> Any:
        body: dict[str, Any] = dict(extra)
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if pub_date is not None:
            body["pubDate"] = pub_date
        if published_at_ms is not None:
            # ABS sorts and shows episodes by ``publishedAt`` (epoch ms) and
            # does not derive it from ``pubDate``; send both.
            body["publishedAt"] = int(published_at_ms)
        return self._send(
            "PATCH",
            f"/api/podcasts/{quote(item_id, safe='')}/episode/{quote(episode_id, safe='')}",
            body=body,
        )

    def edit_podcast(self, item_id: str, metadata: Mapping[str, Any]) -> Any:
        return self._send(
            "PATCH",
            f"/api/items/{quote(item_id, safe='')}/media",
            body={"metadata": dict(metadata)},
        )

    def delete_episode(self, item_id: str, episode_id: str, *, hard: bool = True) -> Any:
        return self._send(
            "DELETE",
            f"/api/podcasts/{quote(item_id, safe='')}/episode/{quote(episode_id, safe='')}",
            query={"hard": 1 if hard else None},
            parse_json=False,
        )


def from_settings(settings: Any) -> AbsClient:
    """Build a client from :class:`yt2abs.config.AbsConfig`."""
    return AbsClient(
        base_url=settings.url,
        api_key=settings.api_key,
        timeout=settings.timeout,
        progress_user=getattr(settings, "progress_user", ""),
    )
