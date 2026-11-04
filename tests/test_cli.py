"""``yt2abs check``: config warnings, channel ids and retention safety output."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from yt2abs.cli import main
from yt2abs.db import Database

VALID = "UCuAXFkgsw1L7xaCfnd5JJOw"
OTHER = "UC0000000000000000000000"


class CheckCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.config_path = self.root / "config.json"
        self.db = Database(self.data / "yt2abs.sqlite3")
        self.db.initialize()

    def write_config(self, channels: dict[str, Any], **extra: Any) -> None:
        document: dict[str, Any] = {
            "defaults": {"retention_days": 14},
            "abs": {
                "url": "http://abs.local",
                "api_key": "key",
                "library_id": "lib",
                "library_path": "/youtube-podcasts",
            },
            "paths": {"data_dir": str(self.data), "tmp_dir": str(self.root / "tmp")},
            "channels": channels,
        }
        document.update(extra)
        self.config_path.write_text(json.dumps(document), encoding="utf-8")

    def run_check(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["check", "--config", str(self.config_path)])
        return code, out.getvalue(), err.getvalue()

    def test_invalid_channel_id_is_printed_as_warning(self) -> None:
        self.write_config({"UC_keep": {"retention_days": None}})
        code, _out, err = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("UC_keep", err)
        self.assertIn("defaults", err)

    def test_channel_from_config_missing_in_db_is_reported(self) -> None:
        self.write_config({VALID: {"retention_days": 7}})
        _code, _out, err = self.run_check()
        self.assertIn("without any database row", err)
        self.assertIn(VALID, err)

    def test_channel_present_in_db_is_not_reported(self) -> None:
        self.write_config({VALID: {"retention_days": 7}})
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.update_video("dQw4w9WgXcQ", channel_id=VALID)
        _code, out, err = self.run_check()
        self.assertNotIn("without any database row", err)
        self.assertIn("channels in database: 1", out)

    def test_missing_database_is_reported_without_crashing(self) -> None:
        (self.data / "yt2abs.sqlite3").unlink()
        self.write_config({})
        code, out, _err = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("does not exist yet", out)

    def test_retention_safety_and_progress_scope_are_printed(self) -> None:
        self.write_config(
            {},
            retention={"dry_run": True, "reconcile_max_deletions": 4},
            abs={
                "url": "http://abs.local",
                "api_key": "key",
                "library_id": "lib",
                "library_path": "/youtube-podcasts",
                "progress_user": "kid",
            },
        )
        _code, out, _err = self.run_check()
        self.assertIn("dry_run=yes", out)
        self.assertIn("max 4 wierszy", out)
        self.assertIn("ABS progress: only account 'kid'", out)

    def test_zero_rate_limit_is_shown_as_unlimited(self) -> None:
        self.write_config({}, api={"rate_limit_per_minute": 0})
        _code, out, _err = self.run_check()
        self.assertIn("rate=no limit", out)


if __name__ == "__main__":
    unittest.main()
