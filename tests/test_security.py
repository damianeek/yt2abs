"""LAN-mode HTTP surface: no authentication, proxy trust and rate limiting.

yt2abs runs on a trusted LAN, so the token, the session cookie, the login form
and the ``?token=`` bootstrap are gone.  The tests below pin what replaced them:

* every endpoint answers without credentials - no ``401``, no ``Set-Cookie``,
  and a stray ``Authorization`` header or ``?token=`` parameter changes nothing;
* forwarding headers are honoured only for peers in ``api.trusted_proxies`` and
  the right-most ``X-Forwarded-For`` hop wins, so a spoofed header cannot pick
  a fresh rate-limit bucket - including a repeated header: proxies append their
  own line after the client-supplied one, so every line must be read;
* the rate limit still applies per client (stricter for ``/add``), while
  ``/health`` stays reachable.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from yt2abs.app import create_app
from yt2abs.config import ApiConfig, parse_config
from yt2abs.db import Database
from yt2abs.security import (
    SlidingWindowLimiter,
    TrustedProxies,
    _trusts_forwarded_headers,
    client_ip,
    rate_limit_key,
)

PUBLIC_URL = "https://yt2abs.example.com"
VIDEO = "https://youtu.be/dQw4w9WgXcQ"


def make_config(root: Path, *, api: dict[str, Any] | None = None):
    section: dict[str, Any] = {"public_url": PUBLIC_URL}
    if api is not None:
        section = api
    return parse_config(
        {
            "defaults": {"retention_days": 14},
            "abs": {
                "url": "http://abs.local",
                "api_key": "key",
                "library_id": "lib",
                "library_path": "/lib",
            },
            "paths": {"data_dir": str(root / "data"), "tmp_dir": str(root / "tmp")},
            "api": section,
            "channels": {},
        }
    )


class ClientTestCase(unittest.TestCase):
    """Shared setup: a temporary config root and a started ``TestClient``."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_client(
        self,
        api: dict[str, Any] | None = None,
        *,
        client: tuple[str, int] = ("testclient", 50000),
        base_url: str = "http://testserver",
    ) -> TestClient:
        config = make_config(self.root, api=api)
        app = create_app(
            config, db=Database(self.root / "db.sqlite3"), worker_enabled=False
        )
        client_obj = TestClient(app, client=client, base_url=base_url)
        client_obj.__enter__()
        self.addCleanup(client_obj.__exit__, None, None, None)
        return client_obj


class NoAuthTests(ClientTestCase):
    """Nothing on the HTTP surface asks for credentials any more."""

    def test_api_works_without_credentials(self) -> None:
        client = self.make_client()
        self.assertEqual(client.get("/api/jobs").status_code, 200)
        self.assertEqual(client.get("/api/system").status_code, 200)
        created = client.post("/api/jobs", json={"url": VIDEO})
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(
            [job["video_id"] for job in client.get("/api/jobs").json()["jobs"]],
            ["dQw4w9WgXcQ"],
        )

    def test_pages_open_without_a_cookie(self) -> None:
        client = self.make_client()
        for path in ("/", "/bookmarklet", "/settings"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, f"{path}: {response.text}")
            self.assertNotIn("set-cookie", response.headers)

    def test_add_queues_a_job(self) -> None:
        """The exact bookmarklet flow: a plain navigation with no cookie."""
        client = self.make_client()
        response = client.get("/add", params={"url": VIDEO})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Added to the queue", response.text)
        self.assertIn("dQw4w9WgXcQ", response.text)

    def test_add_rejects_non_youtube_url(self) -> None:
        client = self.make_client()
        bad = client.get("/add", params={"url": "https://vimeo.com/1"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("YouTube", bad.text)
        self.assertEqual(client.get("/add").status_code, 400)

    def test_session_endpoint_is_gone(self) -> None:
        client = self.make_client()
        self.assertEqual(client.post("/api/session", json={"token": "x"}).status_code, 404)
        self.assertEqual(client.delete("/api/session").status_code, 404)

    def test_legacy_query_token_is_ignored(self) -> None:
        """``?token=`` used to redirect and set a cookie; now it is noise."""
        client = self.make_client()
        page = client.get("/", params={"token": "whatever"}, follow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn("set-cookie", page.headers)
        add = client.get(
            "/add", params={"url": VIDEO, "token": "whatever"}, follow_redirects=False
        )
        self.assertEqual(add.status_code, 200, add.text)
        self.assertIn("Added to the queue", add.text)

    def test_stray_authorization_header_is_ignored(self) -> None:
        client = self.make_client()
        response = client.get(
            "/api/jobs", headers={"Authorization": "Bearer nonsense"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["jobs"], [])

    def test_system_reports_rate_limits_without_an_auth_section(self) -> None:
        client = self.make_client(
            api={"rate_limit_per_minute": 30, "public_rate_limit_per_minute": 5}
        )
        payload = client.get("/api/system").json()
        self.assertNotIn("auth", payload)
        self.assertEqual(payload["api"]["rate_limit_per_minute"], 30)
        self.assertEqual(payload["api"]["public_rate_limit_per_minute"], 5)

    def test_add_is_rate_limited(self) -> None:
        client = self.make_client(api={"public_rate_limit_per_minute": 2})
        for _ in range(2):
            self.assertEqual(client.get("/add", params={"url": VIDEO}).status_code, 200)
        blocked = client.get("/add", params={"url": VIDEO})
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("retry-after", blocked.headers)
        self.assertIn("Too many", blocked.text)

    def test_api_is_rate_limited(self) -> None:
        client = self.make_client(api={"rate_limit_per_minute": 3})
        statuses = [client.get("/api/jobs").status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 429])

    def test_health_is_never_rate_limited(self) -> None:
        client = self.make_client(api={"rate_limit_per_minute": 1})
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 200)


class RateLimiterTests(unittest.TestCase):
    def test_ipv6_addresses_share_one_bucket_per_64(self) -> None:
        """Review #6: rotating addresses inside one /64 must not reset the limit."""
        first = rate_limit_key("2001:db8:1:2::1")
        second = rate_limit_key("2001:db8:1:2::ffff")
        other_64 = rate_limit_key("2001:db8:1:3::1")
        self.assertEqual(first, "2001:db8:1:2::/64")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_64)

    def test_rate_limit_key_keeps_ipv4_and_garbage_addresses_exact(self) -> None:
        self.assertEqual(rate_limit_key("198.51.100.7"), "198.51.100.7")
        self.assertEqual(rate_limit_key("::ffff:198.51.100.7"), "198.51.100.7")
        self.assertEqual(rate_limit_key("unknown"), "unknown")

    def test_window_slides(self) -> None:
        now = [0.0]
        limiter = SlidingWindowLimiter(2, window=60, clock=lambda: now[0])
        self.assertEqual(limiter.hit("a")[0], True)
        self.assertEqual(limiter.hit("a")[0], True)
        allowed, retry = limiter.hit("a")
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        # Another key has its own bucket.
        self.assertTrue(limiter.hit("b")[0])
        now[0] = 61.0
        self.assertTrue(limiter.hit("a")[0])

    def test_zero_limit_disables_the_limiter(self) -> None:
        limiter = SlidingWindowLimiter(0, window=60, clock=lambda: 0.0)
        for _ in range(100):
            self.assertEqual(limiter.hit("a"), (True, 0.0))

    def test_client_ip_respects_proxy_flag(self) -> None:
        scope = {
            "client": ("198.51.100.7", 1234),
            "headers": [(b"x-forwarded-for", b"203.0.113.5")],
        }
        self.assertEqual(client_ip(scope), "198.51.100.7")
        self.assertEqual(client_ip(scope, trust_proxy=True), "203.0.113.5")

    def test_key_table_is_bounded(self) -> None:
        limiter = SlidingWindowLimiter(5, max_keys=3)
        for index in range(10):
            limiter.hit(f"k{index}")
        self.assertLessEqual(len(limiter), 3)

    def test_expired_keys_are_reclaimed(self) -> None:
        now = [0.0]
        limiter = SlidingWindowLimiter(1, window=10, clock=lambda: now[0], max_keys=2)
        limiter.hit("a")
        limiter.hit("b")
        now[0] = 11.0
        limiter.hit("c")
        self.assertLessEqual(len(limiter), 2)

    def test_live_lru_bucket_is_evicted_when_the_cap_is_hit(self) -> None:
        limiter = SlidingWindowLimiter(5, window=60, clock=lambda: 0.0, max_keys=2)
        limiter.hit("a")
        limiter.hit("b")
        limiter.hit("c")  # cap reached: the oldest live bucket goes
        self.assertEqual(len(limiter), 2)
        self.assertTrue(limiter.hit("a")[0])

    def test_reset_clears_buckets(self) -> None:
        limiter = SlidingWindowLimiter(1)
        limiter.hit("a")
        limiter.reset()
        self.assertEqual(len(limiter), 0)
        self.assertTrue(limiter.hit("a")[0])


class ProxyHeaderTests(ClientTestCase):
    def scope(self, peer: str, headers: dict[str, str]) -> dict[str, Any]:
        return {
            "client": (peer, 1234),
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in headers.items()
            ],
        }

    def test_untrusted_peer_ignores_forwarding_headers(self) -> None:
        scope = self.scope("198.51.100.7", {"x-forwarded-for": "203.0.113.5"})
        trusted = TrustedProxies(["10.0.0.0/8"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted),
            "198.51.100.7",
        )

    def test_trusted_proxy_uses_right_most_hop(self) -> None:
        scope = self.scope(
            "10.1.2.1", {"x-forwarded-for": "203.0.113.5, 198.51.100.9"}
        )
        trusted = TrustedProxies(["10.1.2.1/32"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted),
            "198.51.100.9",
        )

    def test_repeated_xff_lines_use_the_line_the_proxy_appended(self) -> None:
        scope = {
            "client": ("10.1.2.1", 1234),
            "headers": [
                (b"x-forwarded-for", b"203.0.113.5"),
                (b"x-forwarded-for", b"198.51.100.9"),
            ],
        }
        trusted = TrustedProxies(["10.1.2.1/32"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted),
            "198.51.100.9",
        )

    def test_x_real_ip_is_the_fallback(self) -> None:
        scope = self.scope("10.1.2.1", {"x-real-ip": "198.51.100.9"})
        trusted = TrustedProxies(["10.1.2.1/32"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted),
            "198.51.100.9",
        )

    def test_garbage_forwarding_headers_fall_back_to_the_peer(self) -> None:
        scope = self.scope("10.1.2.1", {"x-forwarded-for": "not-an-ip"})
        trusted = TrustedProxies(["10.1.2.1/32"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted), "10.1.2.1"
        )

    def test_cidr_matching(self) -> None:
        trusted = TrustedProxies(["10.0.0.0/8", "2001:db8::/32"])
        self.assertTrue(trusted.contains("10.4.5.6"))
        self.assertFalse(trusted.contains("11.4.5.6"))
        self.assertTrue(trusted.contains("2001:db8::1"))
        self.assertFalse(trusted.contains("2001:db9::1"))
        self.assertFalse(TrustedProxies().contains("10.4.5.6"))

    def test_trusted_proxies_reject_host_bits(self) -> None:
        with self.assertRaises(ValueError):
            TrustedProxies(["10.1.2.1/8"])

    def test_forwarded_entries_with_a_port_are_accepted(self) -> None:
        scope = self.scope("10.1.2.1", {"x-forwarded-for": "198.51.100.9:41234"})
        trusted = TrustedProxies(["10.1.2.1/32"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted),
            "198.51.100.9",
        )

    def test_ipv6_forwarded_address_and_allow_list(self) -> None:
        scope = self.scope("2001:db8:1:2::5", {"x-forwarded-for": "[2001:db8:9::7]:443"})
        trusted = TrustedProxies(["2001:db8:1:2::/64"])
        self.assertEqual(
            client_ip(scope, trust_proxy=True, trusted_proxies=trusted),
            "2001:db8:9::7",
        )

    def test_ipv4_mapped_ipv6_shares_the_ipv4_bucket(self) -> None:
        self.assertEqual(
            rate_limit_key("::ffff:198.51.100.7"), rate_limit_key("198.51.100.7")
        )

    def test_ipv4_mapped_allow_list_entry_matches_both_spellings(self) -> None:
        trusted = TrustedProxies(["192.0.2.0/24"])
        self.assertTrue(trusted.contains("192.0.2.7"))
        self.assertTrue(trusted.contains("::ffff:192.0.2.7"))

    def test_legacy_trust_proxy_without_a_list_still_trusts_every_peer(self) -> None:
        """Documented escape hatch; the app logs a warning for it at startup."""
        scope = self.scope("198.51.100.7", {"x-forwarded-for": "203.0.113.5"})
        self.assertEqual(client_ip(scope, trust_proxy=True), "203.0.113.5")

    def test_forwarding_headers_need_the_flag_or_the_allow_list(self) -> None:
        scope = self.scope("10.1.2.1", {"x-forwarded-for": "203.0.113.5"})
        trusted = TrustedProxies(["10.1.2.1/32"])
        # An allow-list entry is itself the permission: the listed proxy is trusted.
        self.assertTrue(_trusts_forwarded_headers(scope, ApiConfig(), trusted))
        # No list and no flag: forwarding headers are ignored.
        self.assertFalse(
            _trusts_forwarded_headers(scope, ApiConfig(), TrustedProxies())
        )
        # Legacy flag with an empty list trusts every peer (warned at startup).
        self.assertTrue(
            _trusts_forwarded_headers(scope, ApiConfig(trust_proxy=True), TrustedProxies())
        )

    def test_spoofed_xff_does_not_create_a_new_bucket(self) -> None:
        """Review #2: rotating X-Forwarded-For must not bypass the limiter."""
        client = self.make_client(
            api={
                "rate_limit_per_minute": 2,
                "trust_proxy": True,
                "trusted_proxies": ["10.0.0.0/8"],
            }
        )
        first = client.get("/api/jobs", headers={"X-Forwarded-For": "203.0.113.1"})
        second = client.get("/api/jobs", headers={"X-Forwarded-For": "203.0.113.2"})
        third = client.get("/api/jobs", headers={"X-Forwarded-For": "203.0.113.3"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 429)

    def test_trusted_proxy_buckets_by_right_most_hop(self) -> None:
        client = self.make_client(
            api={
                "rate_limit_per_minute": 1,
                "trust_proxy": True,
                "trusted_proxies": ["10.0.0.0/8"],
            },
            client=("10.0.0.5", 50000),
        )
        # Same real client, spoofed left-most hop: still one bucket -> 429.
        first = client.get(
            "/api/jobs", headers={"X-Forwarded-For": "203.0.113.1, 198.51.100.7"}
        )
        second = client.get(
            "/api/jobs", headers={"X-Forwarded-For": "203.0.113.2, 198.51.100.7"}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        # A different real client (right-most hop) gets its own bucket.
        other = client.get("/api/jobs", headers={"X-Forwarded-For": "192.0.2.9"})
        self.assertEqual(other.status_code, 200)

    def test_ipv6_client_behind_a_trusted_proxy_gets_one_bucket_per_64(self) -> None:
        client = self.make_client(
            api={
                "rate_limit_per_minute": 1,
                "trust_proxy": True,
                "trusted_proxies": ["10.0.0.0/8"],
            },
            client=("10.0.0.5", 50000),
        )

        def call(address: str) -> int:
            return client.get(
                "/api/jobs", headers={"X-Forwarded-For": address}
            ).status_code

        self.assertEqual(call("2001:db8:1:2::1"), 200)
        self.assertEqual(call("2001:db8:1:2::2"), 429)
        self.assertEqual(call("2001:db8:1:3::1"), 200)


class HostAndOriginTests(ClientTestCase):
    """DNS rebinding and cross-site writes are refused without any login."""

    def test_ip_literal_host_is_accepted(self) -> None:
        client = self.make_client()
        self.assertEqual(client.get("/api/jobs").status_code, 200)

    def test_single_label_host_is_accepted(self) -> None:
        client = self.make_client(base_url="http://nas")
        self.assertEqual(client.get("/api/jobs").status_code, 200)

    def test_unknown_dotted_host_is_refused(self) -> None:
        client = self.make_client(base_url="http://evil.example")
        response = client.get("/api/jobs")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("Host not allowed", response.json()["detail"])

    def test_public_url_host_is_accepted(self) -> None:
        client = self.make_client(base_url=PUBLIC_URL)
        self.assertEqual(client.get("/api/jobs").status_code, 200)

    def test_explicit_allowed_hosts_entry_is_accepted(self) -> None:
        client = self.make_client(
            api={"allowed_hosts": ["yt2abs.home.example"]},
            base_url="http://yt2abs.home.example",
        )
        self.assertEqual(client.get("/api/jobs").status_code, 200)

    def test_foreign_origin_cannot_write(self) -> None:
        client = self.make_client()
        response = client.post(
            "/api/jobs",
            json={"url": VIDEO},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("cross-site", response.json()["detail"])

    def test_same_origin_write_is_allowed(self) -> None:
        client = self.make_client(base_url="http://192.168.1.10:8099")
        response = client.post(
            "/api/jobs",
            json={"url": VIDEO},
            headers={"Origin": "http://192.168.1.10:8099"},
        )
        self.assertEqual(response.status_code, 202, response.text)

    def test_cross_site_fetch_metadata_cannot_write(self) -> None:
        client = self.make_client()
        response = client.post(
            "/api/jobs",
            json={"url": VIDEO},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_script_without_origin_can_write(self) -> None:
        client = self.make_client()
        response = client.post("/api/jobs", json={"url": VIDEO})
        self.assertEqual(response.status_code, 202, response.text)

    def test_bookmarklet_navigation_stays_open(self) -> None:
        """GET /add is a cross-site navigation by design; only writes are gated."""
        client = self.make_client()
        response = client.get(
            "/add",
            params={"url": VIDEO},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Added to the queue", response.text)


class DeploymentInvariantTests(unittest.TestCase):
    def test_the_app_disables_uvicorn_proxy_headers(self) -> None:
        """SEC-1b/1d: default ``--proxy-headers`` rewrites scope.client from XFF.

        SEC-1d moved the switch from the Dockerfile ``CMD`` to the entry point:
        the image starts ``python -m yt2abs``, which passes
        ``proxy_headers=False`` to uvicorn so the middleware alone decides
        whether forwarding headers may be trusted.
        """
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (root / "yt2abs" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('["python", "-m", "yt2abs"]', dockerfile)
        self.assertIn("proxy_headers=False", entrypoint)


if __name__ == "__main__":
    unittest.main()
