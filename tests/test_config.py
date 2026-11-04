"""Unit tests for yt2abs.config (JSON fallback + YAML when available)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from yt2abs.config import (
    ConfigError,
    DEFAULT_API,
    DEFAULT_RETENTION_DAYS,
    Channel,
    apply_env_overrides,
    load_config,
    parse_config,
)
from yt2abs.sponsorblock import MODE_MUTE, MODE_OFF, MODE_REMOVE

try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ImportError:  # pragma: no cover
    HAVE_YAML = False


DOC = {
    "defaults": {
        "retention_days": 14,
        "protect_unfinished": True,
        "sponsorblock": "off",
    },
    "channels": {
        "UC_cut": {"name": "Ciecie", "retention_days": 7, "sponsorblock": "remove"},
        "UC_mute": {"name": "Wyciszanie", "sponsorblock": "mute:sponsor,selfpromo"},
        "UC_keep": {"name": "Keep", "retention_days": None},
    },
}


class ParseConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_config(DOC)

    def test_defaults(self):
        self.assertEqual(self.config.defaults.retention_days, DEFAULT_RETENTION_DAYS)
        self.assertEqual(self.config.defaults.sponsorblock.mode, MODE_OFF)
        self.assertEqual(self.config.defaults.sponsorblock_api, DEFAULT_API)

    def test_channel_override(self):
        channel = self.config.channel("UC_cut")
        self.assertEqual(channel.retention_days, 7)
        self.assertEqual(channel.sponsorblock.mode, MODE_REMOVE)
        self.assertEqual(channel.sponsorblock.categories, ("sponsor",))
        self.assertEqual(channel.name, "Ciecie")

    def test_channel_inherits_defaults_when_key_absent(self):
        channel = self.config.channel("UC_keep")
        self.assertIsNone(channel.retention_days)
        self.assertEqual(channel.sponsorblock.mode, MODE_OFF)

    def test_mute_channel(self):
        channel = self.config.channel("UC_mute")
        self.assertEqual(channel.sponsorblock.mode, MODE_MUTE)
        self.assertEqual(channel.sponsorblock.categories, ("sponsor", "selfpromo"))

    def test_unknown_channel_gets_defaults(self):
        channel = self.config.channel("UC_unknown")
        self.assertIsInstance(channel, Channel)
        self.assertEqual(channel.retention_days, DEFAULT_RETENTION_DAYS)
        self.assertEqual(channel.sponsorblock.mode, MODE_OFF)

    def test_empty_channel_id_gets_defaults(self):
        self.assertEqual(self.config.channel(None).sponsorblock.mode, MODE_OFF)

    def test_defaults_sponsorblock_applies_when_channel_does_not_define_it(self):
        config = parse_config({"defaults": {"sponsorblock": "remove"}, "channels": {"UC_x": {"name": "X"}}})
        self.assertEqual(config.channel("UC_x").sponsorblock.mode, MODE_REMOVE)
        self.assertEqual(config.channel("UC_other").sponsorblock.mode, MODE_REMOVE)

    def test_channels_as_list(self):
        config = parse_config(
            {"channels": [{"channel_id": "UC_list", "sponsorblock": "mark"}]}
        )
        self.assertEqual(config.channel("UC_list").sponsorblock.mode, "mark")

    def test_invalid_sponsorblock_mentions_channel(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config({"channels": {"UC_bad": {"sponsorblock": "remove:bogus"}}})
        self.assertIn("UC_bad", str(ctx.exception))

    def test_invalid_retention(self):
        with self.assertRaises(ConfigError):
            parse_config({"defaults": {"retention_days": "7d"}})
        with self.assertRaises(ConfigError):
            parse_config({"defaults": {"retention_days": -1}})

    def test_invalid_protect_unfinished(self):
        with self.assertRaises(ConfigError):
            parse_config({"defaults": {"protect_unfinished": "yes"}})

    def test_unknown_top_level_keys_are_kept(self):
        config = parse_config({"custom": {"topic": "yt2abs"}, "channels": {}})
        self.assertEqual(config.raw["custom"]["topic"], "yt2abs")

    def test_defaults_ytdlp_args(self):
        config = parse_config(
            {"defaults": {"ytdlp_args": ["--extractor-args", "youtube:player_client=web_embedded"]}}
        )
        self.assertEqual(
            config.defaults.ytdlp_args, ("--extractor-args", "youtube:player_client=web_embedded")
        )

    def test_defaults_ytdlp_args_must_be_a_list(self):
        with self.assertRaises(ConfigError):
            parse_config({"defaults": {"ytdlp_args": "--no-playlist"}})
        with self.assertRaises(ConfigError):
            parse_config({"defaults": {"ytdlp_args": [1]}})

    def test_empty_document(self):
        config = parse_config({})
        self.assertEqual(config.channels, {})
        self.assertEqual(config.defaults.retention_days, DEFAULT_RETENTION_DAYS)


class LoadConfigTests(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/config.yaml")

    def test_json_fallback_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(json.dumps(DOC), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.path, path)
            self.assertEqual(config.channel("UC_cut").sponsorblock.mode, MODE_REMOVE)

    def test_discovery_of_default_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(json.dumps({"defaults": {"sponsorblock": "mute"}}), encoding="utf-8")
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                config = load_config()
            finally:
                os.chdir(previous)
            self.assertEqual(config.channel("UC_any").sponsorblock.mode, MODE_MUTE)

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_yaml_with_comments_and_nesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "# komentarz\n"
                "defaults:\n"
                "  retention_days: 30\n"
                "  sponsorblock: remove:sponsor,intro\n"
                "channels:\n"
                "  UC_yt:\n"
                "    name: 'Kanal YT'\n",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.defaults.retention_days, 30)
            self.assertEqual(config.channel("UC_yt").sponsorblock.categories, ("sponsor", "intro"))

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_example_config_is_not_used_as_a_fallback(self):
        """A missing config must stop the app, not silently start the example one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.example.yaml").write_text("defaults: {}\n", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(root)
            try:
                with self.assertRaises(ConfigError):
                    load_config()
            finally:
                os.chdir(previous)

    def test_invalid_yaml_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("defaults: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class SecurityAndSchedulingConfigTests(unittest.TestCase):
    def test_defaults_when_sections_are_missing(self):
        config = parse_config({})
        self.assertEqual(config.api.rate_limit_per_minute, 60)
        self.assertEqual(config.api.public_rate_limit_per_minute, 10)
        self.assertEqual(config.api.public_url, "")
        # Forwarding headers are opt-in: a forged X-Forwarded-For must not pick
        # the rate-limit bucket by default.
        self.assertFalse(config.api.trust_proxy)
        self.assertEqual(config.api.trusted_proxies, ())
        self.assertEqual(config.retention.interval_minutes, 60)
        self.assertTrue(config.retention.run_on_startup)
        self.assertTrue(config.ytdlp.update)
        self.assertEqual(config.ytdlp.interval_hours, 24)

    def test_allowed_hosts_accepts_a_list_or_csv(self):
        self.assertEqual(
            parse_config({"api": {"allowed_hosts": "NAS.lan, yt2abs.example.com."}}).api.allowed_hosts,
            ("nas.lan", "yt2abs.example.com"),
        )
        self.assertEqual(
            parse_config({"api": {"allowed_hosts": ["nas.lan", "nas.lan"]}}).api.allowed_hosts,
            ("nas.lan",),
        )
        with self.assertRaises(ConfigError):
            parse_config({"api": {"allowed_hosts": ["bad/host"]}})

    def test_api_section(self):
        config = parse_config(
            {
                "api": {
                    "public_url": "https://yt2abs.example.com/",
                    "rate_limit_per_minute": 120,
                    "public_rate_limit_per_minute": 5,
                    "trust_proxy": False,
                }
            }
        )
        self.assertEqual(config.api.public_url, "https://yt2abs.example.com")
        self.assertEqual(config.api.rate_limit_per_minute, 120)
        self.assertEqual(config.api.public_rate_limit_per_minute, 5)
        self.assertFalse(config.api.trust_proxy)

    def test_trusted_proxies_accept_a_list_or_csv_and_are_normalised(self):
        as_list = parse_config(
            {
                "api": {
                    "trusted_proxies": [
                        "192.168.10.0/24",
                        "192.168.1.10/32",
                        "192.168.10.0/24",
                    ]
                }
            }
        )
        self.assertEqual(
            as_list.api.trusted_proxies, ("192.168.10.0/24", "192.168.1.10/32")
        )

        as_csv = parse_config(
            {"api": {"trusted_proxies": " 192.168.10.0/24 , 192.168.1.10 "}}
        )
        self.assertEqual(
            as_csv.api.trusted_proxies, ("192.168.10.0/24", "192.168.1.10/32")
        )

    def test_invalid_trusted_proxy_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            parse_config({"api": {"trusted_proxies": ["not-a-cidr"]}})
        self.assertIn("trusted_proxies", str(caught.exception))

    def test_trusted_proxy_with_host_bits_is_rejected_instead_of_widened(self):
        """Review #6: ``10.1.2.1/8`` used to widen silently to ``10.0.0.0/8``."""
        with self.assertRaises(ConfigError) as caught:
            parse_config({"api": {"trusted_proxies": ["10.1.2.1/8"]}})
        message = str(caught.exception)
        self.assertIn("host bits", message)
        self.assertIn("10.0.0.0/8", message)
        self.assertIn("10.1.2.1/32", message)

        with self.assertRaises(ConfigError):
            parse_config({"api": {"trusted_proxies": ["2001:db8::1/32"]}})

    def test_ipv4_mapped_allow_list_entry_is_normalised(self):
        """A dual-stack listener reports IPv4 peers as ``::ffff:a.b.c.d``.

        The operator copies that spelling out of the access log, but
        ``client_ip`` maps such peers back to IPv4, so the entry has to be
        reduced as well - otherwise it would silently stop matching.
        """
        mapped = parse_config(
            {"api": {"trusted_proxies": ["::ffff:10.1.2.1/128"]}}
        )
        self.assertEqual(mapped.api.trusted_proxies, ("10.1.2.1/32",))

        subnet = parse_config(
            {"api": {"trusted_proxies": ["::ffff:10.1.2.0/120"]}}
        )
        self.assertEqual(subnet.api.trusted_proxies, ("10.1.2.0/24",))

        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            every = parse_config({"api": {"trusted_proxies": ["::ffff:0:0/96"]}})
        self.assertEqual(every.api.trusted_proxies, ("0.0.0.0/0",))
        self.assertIn("every peer", "\n".join(captured.output))

    def test_wide_trusted_proxy_prefix_logs_a_warning(self):
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config({"api": {"trusted_proxies": ["10.0.0.0/8"]}})
        self.assertEqual(config.api.trusted_proxies, ("10.0.0.0/8",))
        rendered = "\n".join(captured.output)
        self.assertIn("10.0.0.0/8", rendered)
        self.assertIn("client_addr", rendered)

    def test_explicit_default_route_is_accepted_with_a_warning(self):
        """``0.0.0.0/0`` may only arrive when the operator spelled it out."""
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config({"api": {"trusted_proxies": ["0.0.0.0/0"]}})
        self.assertEqual(config.api.trusted_proxies, ("0.0.0.0/0",))
        self.assertIn("every peer", "\n".join(captured.output))

        with self.assertLogs("yt2abs.config", level="WARNING"):
            v6 = parse_config({"api": {"trusted_proxies": ["::/0"]}})
        self.assertEqual(v6.api.trusted_proxies, ("::/0",))

    def test_empty_allow_list_is_the_default_and_logs_nothing(self):
        with self.assertNoLogs("yt2abs.config", level="WARNING"):
            config = parse_config({"api": {"trusted_proxies": []}})
        self.assertEqual(config.api.trusted_proxies, ())
        self.assertFalse(config.api.trust_proxy)

    def test_zero_prefix_is_accepted_with_a_warning(self):
        """``0.0.0.0/0`` is a "trust every peer" mode; it must be visible."""
        api = {
            "public_url": "https://example.test",
            "trust_proxy": True,
            "trusted_proxies": ["0.0.0.0/0"],
        }
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config({"api": dict(api)})
        self.assertEqual(config.api.trusted_proxies, ("0.0.0.0/0",))
        message = "\n".join(captured.output)
        self.assertIn("covers every peer", message)
        self.assertIn("trust_any_peer", message)

        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            accepted = parse_config({"api": {**api, "trust_any_peer": True}})
        self.assertTrue(accepted.api.trust_any_peer)
        self.assertIn("any client can spoof", "\n".join(captured.output))

        with self.assertLogs("yt2abs.config", level="WARNING"):
            v6 = parse_config({"api": {**api, "trusted_proxies": ["::/0"]}})
        self.assertEqual(v6.api.trusted_proxies, ("::/0",))

    def test_split_prefixes_that_cover_every_peer_still_warn(self):
        """``/1`` + ``/1`` is the same mode as ``/0``; the warning must appear."""
        for entries in (
            ["0.0.0.0/1", "128.0.0.0/1"],
            ["::/1", "8000::/1"],
        ):
            with self.subTest(entries=entries), self.assertLogs(
                "yt2abs.config", level="WARNING"
            ) as captured:
                config = parse_config(
                    {
                        "api": {
                            "public_url": "https://example.test",
                            "trusted_proxies": entries,
                        }
                    }
                )
            self.assertEqual(config.api.trusted_proxies, tuple(entries))
            self.assertIn("covers every peer", "\n".join(captured.output))

        # A list that covers everything only inside the LAN is still fine.
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config(
                {
                    "api": {
                        "public_url": "https://example.test",
                        "trusted_proxies": ["0.0.0.0/1"],
                    }
                }
            )
        self.assertEqual(config.api.trusted_proxies, ("0.0.0.0/1",))
        self.assertNotIn("covers every peer", "\n".join(captured.output))

    def test_trust_any_peer_is_settable_through_the_environment(self):
        """Production configures with env; the explicit switch must work there."""
        from yt2abs.config import apply_env_overrides

        document = apply_env_overrides(
            {"channels": {}},
            {
                "YT2ABS_PUBLIC_URL": "https://example.test",
                "YT2ABS_TRUSTED_PROXIES": "0.0.0.0/0",
                "YT2ABS_TRUST_ANY_PEER": "1",
            },
        )
        with self.assertLogs("yt2abs.config", level="WARNING"):
            config = parse_config(document)
        self.assertTrue(config.api.trust_any_peer)
        self.assertEqual(config.api.trusted_proxies, ("0.0.0.0/0",))

        defaulted = apply_env_overrides(
            {"channels": {}},
            {
                "YT2ABS_PUBLIC_URL": "https://example.test",
                "YT2ABS_TRUSTED_PROXIES": "0.0.0.0/0",
            },
        )
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config(defaulted)
        self.assertFalse(config.api.trust_any_peer)
        self.assertIn("covers every peer", "\n".join(captured.output))

    def test_env_only_legacy_trust_proxy_logs_a_warning(self):
        """Production configures through env; the warning must appear there too."""
        from yt2abs.config import apply_env_overrides

        document = apply_env_overrides(
            {"channels": {}},
            {
                "YT2ABS_PUBLIC_URL": "https://example.test",
                "YT2ABS_TRUST_PROXY": "1",
                "YT2ABS_TRUSTED_PROXIES": "",
            },
        )
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config(document)
        self.assertTrue(config.api.trust_proxy)
        self.assertIn("trusts every peer", "\n".join(captured.output))

    def test_legacy_trust_proxy_without_a_token_logs_a_warning(self):
        with self.assertLogs("yt2abs.config", level="WARNING") as captured:
            config = parse_config({"api": {"trust_proxy": True}})
        self.assertTrue(config.api.trust_proxy)
        self.assertEqual(config.api.trusted_proxies, ())
        self.assertIn("trusts every peer", "\n".join(captured.output))

    def test_public_url_is_optional_but_must_be_absolute(self):
        self.assertEqual(parse_config({"api": {}}).api.public_url, "")
        with self.assertRaises(ConfigError) as caught:
            parse_config({"api": {"public_url": "yt2abs.example.com"}})
        self.assertIn("absolute", str(caught.exception))

    def test_retention_and_ytdlp_sections(self):
        config = parse_config(
            {
                "retention": {"interval_minutes": 15, "run_on_startup": False},
                "ytdlp": {
                    "update": False,
                    "interval_hours": 6,
                    "bin_dir": "/data/bin",
                    "command": "/opt/yt-dlp",
                },
            }
        )
        self.assertEqual(config.retention.interval_minutes, 15)
        self.assertFalse(config.retention.run_on_startup)
        self.assertFalse(config.ytdlp.update)
        self.assertEqual(config.ytdlp.interval_hours, 6)
        self.assertEqual(config.ytdlp_bin_dir, "/data/bin")
        self.assertEqual(config.ytdlp_command, "/opt/yt-dlp")

    def test_env_overrides(self):
        from yt2abs.config import apply_env_overrides

        document = apply_env_overrides(
            {"channels": {}},
            {
                "YT2ABS_PUBLIC_URL": "https://example.test",
                "YT2ABS_API_RATE_LIMIT": "30",
                "YT2ABS_PUBLIC_RATE_LIMIT": "3",
                "YT2ABS_TRUST_PROXY": "0",
                "YT2ABS_RETENTION_INTERVAL_MINUTES": "5",
                "YT2ABS_RETENTION_ON_STARTUP": "false",
                "YT2ABS_YTDLP_UPDATE": "off",
                "YT2ABS_YTDLP_UPDATE_INTERVAL_HOURS": "12",
            },
        )
        config = parse_config(document)
        self.assertEqual(config.api.public_url, "https://example.test")
        self.assertEqual(config.api.rate_limit_per_minute, 30)
        self.assertEqual(config.api.public_rate_limit_per_minute, 3)
        self.assertFalse(config.api.trust_proxy)
        self.assertEqual(config.retention.interval_minutes, 5)
        self.assertFalse(config.retention.run_on_startup)
        self.assertFalse(config.ytdlp.update)
        self.assertEqual(config.ytdlp.interval_hours, 12)

    def test_sponsorblock_false_means_off(self):
        config = parse_config({"defaults": {"sponsorblock": False}})
        self.assertEqual(config.defaults.sponsorblock.mode, MODE_OFF)

    def test_invalid_rate_limit(self):
        with self.assertRaises(ConfigError):
            parse_config({"api": {"rate_limit_per_minute": "many"}})
        with self.assertRaises(ConfigError):
            parse_config({"api": {"trust_proxy": "maybe"}})

    def test_default_bin_dir_is_under_data_dir(self):
        config = parse_config({"paths": {"data_dir": "/data"}})
        self.assertEqual(config.ytdlp_bin_dir, "/data/bin")

    def test_managed_binary_wins_over_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "yt-dlp"
            binary.write_text("#!/bin/sh\necho test\n")
            binary.chmod(0o755)
            config = parse_config(
                {"paths": {"data_dir": tmp}, "ytdlp": {"bin_dir": tmp}}
            )
            self.assertEqual(config.ytdlp_command, str(binary))


class SettingsAndSafetyConfigTests(unittest.TestCase):
    def _base(self, **extra: Any) -> dict[str, Any]:
        document = {
            "defaults": {"retention_days": 14},
            "abs": {
                "url": "http://abs.local",
                "api_key": "key",
                "library_id": "lib",
                "library_path": "/youtube-podcasts",
            },
            "paths": {"data_dir": "data", "tmp_dir": "tmp"},
            "channels": {},
        }
        document.update(extra)
        return document

    def test_retention_safety_settings_and_defaults(self) -> None:
        config = parse_config(self._base())
        self.assertFalse(config.retention.dry_run)
        self.assertEqual(config.retention.reconcile_max_deletions, 20)
        self.assertEqual(config.retention.reconcile_max_fraction, 0.2)

        custom = parse_config(
            self._base(
                retention={
                    "dry_run": True,
                    "reconcile_max_deletions": 3,
                    "reconcile_max_fraction": 0.5,
                }
            )
        )
        self.assertTrue(custom.retention.dry_run)
        self.assertEqual(custom.retention.reconcile_max_deletions, 3)
        self.assertEqual(custom.retention.reconcile_max_fraction, 0.5)

    def test_reconcile_limit_applies_both_caps(self) -> None:
        config = parse_config(
            self._base(retention={"reconcile_max_deletions": 20, "reconcile_max_fraction": 0.2})
        )
        retention = config.retention
        self.assertEqual(retention.reconcile_limit(0), 0)
        # The percentage cap protects small libraries too.
        self.assertEqual(retention.reconcile_limit(3), 1)
        self.assertEqual(retention.reconcile_limit(9), 2)
        # The absolute cap wins for larger libraries.
        self.assertEqual(retention.reconcile_limit(10), 2)
        self.assertEqual(retention.reconcile_limit(50), 10)
        self.assertEqual(retention.reconcile_limit(500), 20)

    def test_reconcile_limit_zero_disables_a_cap(self) -> None:
        only_fraction = parse_config(
            self._base(retention={"reconcile_max_deletions": 0, "reconcile_max_fraction": 0.5})
        )
        self.assertEqual(only_fraction.retention.reconcile_limit(100), 50)
        unlimited = parse_config(
            self._base(retention={"reconcile_max_deletions": 0, "reconcile_max_fraction": 0})
        )
        self.assertEqual(unlimited.retention.reconcile_limit(100), 100)

    def test_invalid_reconcile_fraction(self) -> None:
        for value in (-0.1, 1.5, "many"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_config(self._base(retention={"reconcile_max_fraction": value}))

    def test_progress_user_is_parsed(self) -> None:
        config = parse_config(
            self._base(
                abs={
                    "url": "http://abs",
                    "api_key": "k",
                    "library_id": "l",
                    "library_path": "/p",
                    "progress_user": "kid",
                }
            )
        )
        self.assertEqual(config.abs.progress_user, "kid")

    def test_env_progress_user(self) -> None:
        with mock.patch.dict(os.environ, {"ABS_PROGRESS_USER": "root"}, clear=False):
            config = parse_config(apply_env_overrides(self._base()))
        self.assertEqual(config.abs.progress_user, "root")

    def test_env_dry_run(self) -> None:
        with mock.patch.dict(os.environ, {"YT2ABS_RETENTION_DRY_RUN": "1"}, clear=False):
            config = parse_config(apply_env_overrides(self._base()))
        self.assertTrue(config.retention.dry_run)

    def test_rate_limit_zero_means_disabled(self) -> None:
        config = parse_config(
            self._base(api={"rate_limit_per_minute": 0, "public_rate_limit_per_minute": 0})
        )
        self.assertEqual(config.api.rate_limit_per_minute, 0)
        self.assertEqual(config.api.public_rate_limit_per_minute, 0)

    def test_invalid_channel_id_is_reported(self) -> None:
        config = parse_config(
            self._base(
                channels={
                    "UC_keep": {"retention_days": None},
                    "UCuAXFkgsw1L7xaCfnd5JJOw": {"retention_days": 7},
                }
            )
        )
        self.assertEqual(len(config.warnings), 1)
        self.assertIn("UC_keep", config.warnings[0])
        self.assertIn("defaults", config.warnings[0])

    def test_valid_channel_id_produces_no_warning(self) -> None:
        config = parse_config(
            self._base(channels={"UCuAXFkgsw1L7xaCfnd5JJOw": {"retention_days": 7}})
        )
        self.assertEqual(config.warnings, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
