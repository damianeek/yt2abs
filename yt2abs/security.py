"""Rate limiting for the HTTP surface of a trusted-LAN deployment.

yt2abs is meant to run inside a private network: it has no user accounts and no
API token, so *network reachability is the access control*.  The service binds
to one address (``BIND_ADDR``) and must not be published on a public address;
the reverse proxy, if any, belongs on the same host and should carry a LAN ACL
of its own.

What lives here:

* a per-client-IP sliding-window rate limit: ``api.rate_limit_per_minute`` for
  the API and pages, the stricter ``api.public_rate_limit_per_minute`` for
  ``/add``.  The bucket table is LRU-bounded, so rotating keys cannot exhaust
  memory;
* proxy trust: ``X-Forwarded-For`` / ``X-Real-IP`` are honoured only when the
  TCP peer matches ``api.trusted_proxies``, so a client cannot choose its own
  rate-limit bucket by sending a forged header.  Every line of a repeated
  header is read: proxies such as HAProxy append a *new* ``X-Forwarded-For``
  line after the client-supplied one, so the right-most entry of the *last*
  line is the only hop the trusted proxy actually saw.

The rate limit is a guard against runaway loops (a retrying script, a broken
integration), not a security boundary.  There is no token, session cookie,
login form or ``?token=`` bootstrap any more, so nothing can leak into a URL, a
proxy log or the browser history.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Iterable, MutableMapping
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse

from .config import ApiConfig

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """Per-key request counter over a sliding time window.

    Keys are kept in an LRU-ordered mapping with a hard cap (``max_keys``).
    Without the cap a client could send a fresh ``X-Forwarded-For`` value per
    request and grow the dictionary without bound.
    """

    def __init__(
        self,
        limit: int,
        *,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self.limit = int(limit)
        #: ``api.rate_limit_per_minute: 0`` means "no limit", not one request.
        self.enabled = self.limit > 0
        self.window = float(window)
        self.clock = clock
        self.max_keys = max(1, int(max_keys))
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def hit(self, key: str) -> tuple[bool, float]:
        """Register a request; returns ``(allowed, retry_after_seconds)``."""
        if not self.enabled:
            return True, 0.0
        now = self.clock()
        with self._lock:
            bucket = self._events.get(key)
            if bucket is None:
                self._make_room(now)
                bucket = deque()
                self._events[key] = bucket
            else:
                self._events.move_to_end(key)
            while bucket and now - bucket[0] >= self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry = self.window - (now - bucket[0])
                return False, max(0.0, retry)
            bucket.append(now)
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def _make_room(self, now: float) -> None:
        """Ensure one free slot in O(1) work.

        Drops the least recently used bucket - first when it already expired,
        otherwise unconditionally, because the cap is hard: under a rotating
        key (spoofed ``X-Forwarded-For``) scanning the whole mapping per request
        would itself be a CPU denial of service.
        """
        if len(self._events) < self.max_keys:
            return
        while self._events and len(self._events) >= self.max_keys:
            key, bucket = next(iter(self._events.items()))
            if bucket and now - bucket[-1] < self.window:
                self._events.popitem(last=False)  # still active: evict LRU
            else:
                self._events.pop(key, None)  # expired: reclaim for free


class TrustedProxies:
    """CIDR allow-list of reverse proxies whose forwarding headers are trusted.

    Entries are expected to be normalised already (``config._parse_cidr_list``
    does that and rejects host bits).  Parsing is strict here as well: a value
    like ``10.1.2.1/8`` must fail closed instead of silently widening the list
    to ``10.0.0.0/8``.
    """

    def __init__(self, entries: Iterable[str] = ()) -> None:
        cleaned = tuple(str(entry).strip() for entry in entries if str(entry).strip())
        self.entries = cleaned
        self.networks = tuple(
            ipaddress.ip_network(entry, strict=True) for entry in cleaned
        )

    def __bool__(self) -> bool:
        return bool(self.networks)

    def contains(self, address: str) -> bool:
        if not self.networks or not address:
            return False
        normalised = _normalise_ip(address)
        if normalised is None:
            return False
        # Check both spellings: ``::ffff:a.b.c.d`` is normalised to IPv4 for the
        # rate-limit key, but an IPv6-wide entry such as ``::/0`` (or a mapped
        # /96+ block that config did not reduce) still covers the original
        # address, exactly as it did before the IPv4 mapping was introduced.
        candidates = {normalised}
        raw = _strip_port(address)
        if raw:
            candidates.add(raw)
        for candidate in candidates:
            try:
                parsed = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if any(parsed in network for network in self.networks):
                return True
        return False


def _strip_port(value: str) -> str:
    """Drop a ``host:port`` / ``[v6]:port`` suffix from a forwarding entry."""
    text = value.strip()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end != -1 else text
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host
    return text


def _normalise_ip(value: str) -> str | None:
    """Return the canonical text form of an IP or ``None`` for garbage.

    Accepts the spellings proxies put in ``X-Forwarded-For``: a bare address,
    ``address:port`` and ``[v6]:port``.  IPv4-mapped IPv6 addresses are reduced
    to their IPv4 form, so ``::ffff:198.51.100.7`` and ``198.51.100.7`` share a
    rate-limit bucket and match the same allow-list entry.
    """
    text = _strip_port(value or "")
    if not text:
        return None
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    return str(parsed)


#: IPv6 clients are normally delegated a whole /64, so aggregating the limiter
#: key at /64 keeps one bucket per subscriber instead of one per rotating
#: address.  IPv4 keeps the exact address: a /32 is already one host.
RATE_LIMIT_IPV6_PREFIX = 64


def rate_limit_key(address: str) -> str:
    """Return the rate-limiter bucket key for a client address.

    ``client_ip`` already reduces IPv4-mapped IPv6 addresses to IPv4; this
    function normalises again so a direct caller cannot accidentally put
    ``::ffff:a.b.c.d`` into a /64 bucket.  A real IPv6 client can rotate
    addresses inside its own /64 (and they arrive through the trusted proxy as
    genuine entries), which would give every request a fresh bucket and evict
    other clients from the LRU.  The key is therefore the /64 network; limits
    and the LRU size are unchanged.
    """
    normalised = _normalise_ip(address)
    if normalised is None:
        return address
    parsed = ipaddress.ip_address(normalised)
    if parsed.version == 6:
        return str(
            ipaddress.ip_network(f"{parsed}/{RATE_LIMIT_IPV6_PREFIX}", strict=False)
        )
    return str(parsed)


def _right_most_forwarded(headers: Headers, name: str) -> str | None:
    """Right-most entry across every *line* of a forwarding header.

    ``Headers.get`` returns only the first line.  HAProxy with ``option
    forwardfor`` appends its own ``X-Forwarded-For`` line *after* the
    client-supplied one, so reading just the first line would hand the choice of
    the rate-limit bucket back to the client (its right-most entry is under
    client control).  Joining all lines puts the proxy-written line last, which
    is the only hop the trusted proxy actually saw.
    """
    lines = [value for value in headers.getlist(name) if value]
    if not lines:
        return None
    entries = [
        entry.strip() for value in lines for entry in value.split(",") if entry.strip()
    ]
    return entries[-1] if entries else None


def client_ip(
    scope: MutableMapping[str, Any],
    *,
    trust_proxy: bool = False,
    trusted_proxies: TrustedProxies | None = None,
) -> str:
    """Best-effort client address.

    Forwarding headers are used only when ``trust_proxy`` is enabled *and* the
    connecting peer passes the ``trusted_proxies`` allow-list (an empty list
    means the legacy ``trust_proxy: true`` mode: trust every peer).  All lines
    of a repeated ``X-Forwarded-For`` header are joined and the right-most entry
    of the result wins: the last proxy appends the address it actually accepted
    the connection from, so everything before it is client-controlled.
    """
    client = scope.get("client")
    peer = str(client[0]) if client else "unknown"
    if not trust_proxy:
        return peer
    trusted = trusted_proxies or TrustedProxies()
    if trusted and not trusted.contains(peer):
        return peer

    headers = Headers(scope=scope)
    candidate = _normalise_ip(_right_most_forwarded(headers, "x-forwarded-for") or "")
    if candidate:
        return candidate
    real = _normalise_ip(_right_most_forwarded(headers, "x-real-ip") or "")
    if real:
        return real
    return peer


def _trusts_forwarded_headers(
    scope: MutableMapping[str, Any],
    config: ApiConfig,
    trusted: TrustedProxies,
) -> bool:
    """Whether forwarding headers from this peer may be used."""
    if not config.trust_proxy and not trusted:
        return False
    if not trusted:
        # Legacy ``trust_proxy: true`` without an allow-list trusts every peer.
        return True
    client = scope.get("client")
    peer = str(client[0]) if client else ""
    return trusted.contains(peer)


#: Methods that change state; a browser cross-site request may not perform them.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_name(value: str) -> str | None:
    """Normalise a ``Host``/``Origin`` authority to a bare lowercase hostname."""
    raw = (value or "").strip().rstrip(".")
    if not raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end].lower() if end != -1 else None
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        if port.isdigit():
            return host.lower()
    return raw.lower()


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _allowed_names(config: ApiConfig) -> set[str]:
    names = {str(item).strip().lower().rstrip(".") for item in config.allowed_hosts}
    if config.public_url:
        try:
            public = urlsplit(config.public_url).hostname
        except ValueError:
            public = None
        if public:
            names.add(public.rstrip(".").lower())
    return {name for name in names if name}


def host_allowed(host_header: str, config: ApiConfig) -> bool:
    """Whether a ``Host`` header may address this service.

    A LAN deployment is reached by an IP literal, ``localhost`` or a single-label
    name (``nas``) - none of those can be registered by an attacker, so they are
    always accepted.  Dotted names must be listed in ``api.allowed_hosts`` or
    match ``api.public_url``.  That blocks DNS rebinding: a page on
    ``evil.example`` resolves to the LAN address, but the browser still sends
    ``evil.example`` in ``Host``, so the request is refused.
    """
    name = _host_name(host_header)
    if not name:
        return False
    if name == "localhost" or _is_ip_literal(name) or "." not in name:
        return True
    return name in _allowed_names(config)


def cross_origin_write(
    scope: MutableMapping[str, Any], headers: Headers, config: ApiConfig
) -> bool:
    """Whether an unsafe method arrives from a browser origin that is not ours.

    Reads and ``GET /add`` stay open (the bookmarklet is a cross-site top-level
    navigation by design); every state-changing request must come from one of our
    own host names.  A non-browser client sends no ``Origin`` and no
    ``Sec-Fetch-Site``, so scripts are unaffected.
    """
    method = (scope.get("method") or "GET").upper()
    if method not in UNSAFE_METHODS:
        return False
    origin = (headers.get("origin") or "").strip()
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return True
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return True
        return not host_allowed(parsed.hostname, config)
    return (headers.get("sec-fetch-site") or "").strip().lower() == "cross-site"


def _apply_public_scheme(scope: MutableMapping[str, Any], public_url: str) -> None:
    """Restore ``https`` for requests that carry the public proxy host.
    Behind NPM uvicorn sees ``http`` (``--no-proxy-headers``), and Starlette
    builds absolute ``Location`` headers from ``scope["scheme"]``.  When the
    request carries the https host of ``api.public_url``, restore the scheme so
    a ``/jobs/`` typo redirects the browser back to https instead of plain
    http.  A client-controlled Host can only ever *upgrade* the scheme here.
    """
    if not public_url or scope.get("scheme") == "https":
        return
    try:
        public = urlsplit(public_url)
    except ValueError:
        return
    if public.scheme != "https" or not public.hostname:
        return
    host = (Headers(scope=scope).get("host") or "").strip().rstrip(".").lower()
    if not host:
        return
    if host.startswith("["):
        end = host.find("]")
        hostname = host[1:end] if end != -1 else host
    else:
        hostname = host.split(":", 1)[0]
    if hostname == public.hostname.rstrip(".").lower():
        scope["scheme"] = "https"


def _error_response(
    *, status: int, detail: str, html: bool, headers: dict[str, str] | None = None
):
    extra = dict(headers or {})
    if html:
        body = (
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            f"<title>{status} · yt2abs</title>"
            "<body style=\"font-family:system-ui;max-width:40rem;margin:3rem auto\">"
            "<h1>Too many requests</h1>"
            f"<p>{detail}</p>"
            "<p><a href=\"/\">Home</a></p></body></html>"
        )
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                **extra,
            },
        )
    return JSONResponse(
        {"detail": detail},
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            **extra,
        },
    )


class SecurityMiddleware:
    """Pure ASGI middleware: per-client rate limiting.

    The class name is kept so the wiring (``app.add_middleware``) and the
    imports stay untouched; authentication is gone, see the module docstring.
    """

    def __init__(self, app, config: ApiConfig) -> None:
        self.app = app
        self.config = config
        self.api_limiter = SlidingWindowLimiter(config.rate_limit_per_minute)
        self.public_limiter = SlidingWindowLimiter(config.public_rate_limit_per_minute)
        # Parsed once: the allow-list is immutable for the app's lifetime.
        self.trusted_proxies = TrustedProxies(config.trusted_proxies)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _apply_public_scheme(scope, self.config.public_url)

        headers = Headers(scope=scope)
        if not host_allowed(headers.get("host") or "", self.config):
            response = _error_response(
                status=403,
                detail=(
                    "Host not allowed: add this name to api.allowed_hosts "
                    "(or api.public_url) if the service should answer to it."
                ),
                html=False,
            )
            await response(scope, receive, send)
            return
        if cross_origin_write(scope, headers, self.config):
            response = _error_response(
                status=403,
                detail=(
                    "State-changing requests must come from this host "
                    "(cross-site write refused)."
                ),
                html=False,
            )
            await response(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if path == "/health":
            # A liveness probe must keep answering even when a stuck client is
            # burning through the API budget.
            await self.app(scope, receive, send)
            return

        trust_forwarded = _trusts_forwarded_headers(
            scope, self.config, self.trusted_proxies
        )
        ip = rate_limit_key(
            client_ip(
                scope,
                trust_proxy=trust_forwarded,
                trusted_proxies=self.trusted_proxies,
            )
        )
        limiter = self.public_limiter if path == "/add" else self.api_limiter
        allowed, retry_after = limiter.hit(ip)
        if not allowed:
            response = _error_response(
                status=429,
                detail="Too many requests - try again in a moment.",
                html=not path.startswith("/api/"),
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
