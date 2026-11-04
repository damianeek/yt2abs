"""CLI: ``python3 -m yt2abs.cli`` -> check | sb-args | fetch."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config
from .db import Database
from .downloader import DownloadError, download_for_channel, probe_metadata
from .notify import DiscordNotifier
from .ntfy import NtfyNotifier
from .sponsorblock import ytdlp_args


def _db_channel_ids(config: Config) -> dict[str, int] | None:
    """Channel ids present in the database (``None`` when there is no DB yet)."""
    path = Path(config.paths.database_path)
    if not path.exists():
        return None
    try:
        return Database(path).channel_ids()
    except Exception as exc:  # noqa: BLE001 - check must not crash on a locked DB
        print(f"WARNING: cannot read database {path}: {exc}", file=sys.stderr)
        return None


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"ERROR: configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"Configuration: {config.path}")
    defaults = config.defaults
    print(
        "defaults: retention={ret}, sponsorblock={sb} ({cats}), api={api}".format(
            ret="keep" if defaults.retention_days is None else f"{defaults.retention_days}d",
            sb=defaults.sponsorblock.mode,
            cats=",".join(defaults.sponsorblock.categories) or "-",
            api=defaults.sponsorblock_api,
        )
    )
    for channel_id, channel in config.channels.items():
        print(
            "channel {cid} | {name} | retention={ret} | sponsorblock={sb} ({cats})".format(
                cid=channel_id,
                name=channel.name or "-",
                ret="keep" if channel.retention_days is None else f"{channel.retention_days}d",
                sb=channel.sponsorblock.mode,
                cats=",".join(channel.sponsorblock.categories) or "-",
            )
        )
    print(f"channels: {len(config.channels)}")
    print(
        "api: rate={rate}/min | public={public}/min | public_url={url}"
        " | trust_proxy={trust} | trusted_proxies={proxies}".format(
            rate=config.api.rate_limit_per_minute or "no limit",
            public=config.api.public_rate_limit_per_minute or "no limit",
            url=config.api.public_url or "-",
            trust="yes" if config.api.trust_proxy else "no",
            proxies=",".join(config.api.trusted_proxies) or "-",
        )
    )
    retention = config.retention
    print(
        "retention: every {interval} min | on startup={startup} | dry_run={dry} | "
        "reconcile: max {deletions} wierszy / {fraction:.0%} | yt-dlp={ver}".format(
            interval=retention.interval_minutes,
            startup="yes" if retention.run_on_startup else "no",
            dry="yes (no writes)" if retention.dry_run else "no",
            deletions=retention.reconcile_max_deletions or "no limit",
            fraction=retention.reconcile_max_fraction,
            ver=config.ytdlp_command,
        )
    )

    protect_channels = [
        channel_id
        for channel_id, channel in config.channels.items()
        if channel.protect_unfinished
    ]
    if config.abs.progress_user:
        print(f"ABS progress: only account {config.abs.progress_user!r}")
    else:
        print(
            "ABS progress: all accounts via /api/users (requires an admin key; "
            "fallback to the API key owner account with a warning in the report)"
        )
    if (defaults.protect_unfinished or protect_channels) and not config.abs.configured:
        print(
            "WARNING: protect_unfinished is enabled, but abs.url/api_key/library_id/"
            "library_path are incomplete - unfinished protection will not work.",
            file=sys.stderr,
        )

    db_channels = _db_channel_ids(config)
    if db_channels is None:
        print(
            f"database: {config.paths.database_path} does not exist yet "
            "(config channels have not been used yet)"
        )
    else:
        missing = [cid for cid in config.channels if cid not in db_channels]
        if missing:
            print(
                "WARNING: config channels without any database row (typo? "
                "such a channel gets the defaults policy): " + ", ".join(sorted(missing)),
                file=sys.stderr,
            )
        print(f"channels in database: {len(db_channels)}")

    if config.warnings:
        print("WARNING: configuration has issues (non-blocking):", file=sys.stderr)
        for warning in config.warnings:
            print(f" - {warning}", file=sys.stderr)

    playlist = config.playlist
    print(
        "playlist: {state} | every {interval} min | url={url}".format(
            state="enabled" if playlist.enabled else "disabled",
            interval=playlist.interval_minutes,
            # The (unlisted) playlist id is a capability token: never echo it,
            # ``cli check`` output often ends up in logs or a bug report.
            url="set" if playlist.configured else "-",
        )
    )
    return 0


def _cmd_sb_args(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"ERROR: configuration error: {exc}", file=sys.stderr)
        return 2

    channel_id = (args.channel_id or "").strip() or None
    if channel_id is None and args.url:
        channel_id = probe_metadata(args.url)["channel_id"]
    channel = config.channel(channel_id)
    args_list = ytdlp_args(channel.sponsorblock, api=config.defaults.sponsorblock_api)
    print(
        json.dumps(
            {
                "channel_id": channel.channel_id or None,
                "sponsorblock": channel.sponsorblock.as_dict(),
                "ytdlp_args": args_list,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"ERROR: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        result = download_for_channel(
            args.url,
            args.dest,
            config,
            channel_id=args.channel_id,
            video_id=args.video_id,
        )
    except DownloadError as exc:
        print(f"ERROR: download failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_notify_test(args: argparse.Namespace) -> int:
    notifier = DiscordNotifier.from_env()
    if not notifier.enabled:
        print(
            "ERROR: Discord notifications are disabled - set DISCORD_WEBHOOK_URL "
            "(and check DISCORD_NOTIFY_DISABLED).",
            file=sys.stderr,
        )
        return 2

    message = args.message or "yt2abs: Discord notification test - test message."
    result = notifier.send(message, title="yt2abs: notification test")
    if result.delivered:
        print(f"OK: notification delivered (HTTP {result.status}).")
        return 0
    print(f"ERROR: notification not delivered: {result.error}", file=sys.stderr)
    return 1


def _cmd_ntfy_test(args: argparse.Namespace) -> int:
    notifier = NtfyNotifier.from_env()
    if not notifier.enabled:
        if not notifier.topic:
            reason = "NTFY_TOPIC is empty"
        else:
            reason = notifier.config_error or "NTFY_NOTIFY_DISABLED is set"
        print(
            f"ERROR: ntfy notifications are disabled: {reason}",
            file=sys.stderr,
        )
        return 2

    message = args.message or "yt2abs: ntfy notification test - test message."
    result = notifier.send(
        message,
        title=args.title or "yt2abs: ntfy notification test",
        tags=("test_tube",),
        priority=3,
    )
    if result.delivered:
        print(f"OK: ntfy notification delivered (HTTP {result.status}).")
        return 0
    print(f"ERROR: ntfy notification not delivered: {result.error}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yt2abs", description="yt2abs: download YT audio with per-channel policies")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="print and validate the configuration")
    check.add_argument("--config", default=None)
    check.set_defaults(func=_cmd_check)

    sb = sub.add_parser("sb-args", help="show the sponsorblock setting and yt-dlp args for a channel")
    sb.add_argument("--config", default=None)
    sb.add_argument("--channel-id", default=None)
    sb.add_argument("--url", default=None, help="if channel-id is missing, detect the channel from the URL")
    sb.set_defaults(func=_cmd_sb_args)

    fetch = sub.add_parser("fetch", help="download audio with the channel policy")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--config", default=None)
    fetch.add_argument("--dest", required=True)
    fetch.add_argument("--channel-id", default=None, help="skip channel detection (faster, deterministic)")
    fetch.add_argument("--video-id", default=None)
    fetch.set_defaults(func=_cmd_fetch)

    notify_test = sub.add_parser(
        "notify-test", help="send a test Discord notification (DISCORD_WEBHOOK_URL)"
    )
    notify_test.add_argument("--message", default=None)
    notify_test.set_defaults(func=_cmd_notify_test)

    ntfy_test = sub.add_parser(
        "ntfy-test", help="send a test ntfy notification (NTFY_TOPIC)"
    )
    ntfy_test.add_argument("--message", default=None)
    ntfy_test.add_argument("--title", default=None)
    ntfy_test.set_defaults(func=_cmd_ntfy_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
