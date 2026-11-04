#!/usr/bin/env python3
"""E2E test: SponsorBlock per channel, against a real YouTube video.

Runs three downloads of the same short-ish video (audio only):

 1. channel without any setting      -> file untouched (today's behaviour)
 2. channel with ``sponsorblock: remove`` -> file shorter by the sponsor segment
 3. channel with ``sponsorblock: mute``   -> same length, sponsor window silent

Usage (needs network + yt-dlp + ffmpeg/ffprobe):

    YT2ABS_E2E=1 python3 tests/e2e_sponsorblock.py

Optional env: YT2ABS_E2E_VIDEO (video id), YT2ABS_YTDLP, YT2ABS_E2E_KEEP=1.
Exit code 0 on success, 1 on any failed assertion.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt2abs.config import parse_config  # noqa: E402
from yt2abs.downloader import download_for_channel, probe_metadata  # noqa: E402
from yt2abs.sponsorblock import DEFAULT_API, fetch_segments  # noqa: E402

DEFAULT_VIDEO = "sI_MzL3HIa0"  # "Renault Megane e-Tech", ~1283 s, 1 sponsor segment
YTDLP = os.environ.get("YT2ABS_YTDLP", "yt-dlp")

failures: list[str] = []
notes: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("PASS  " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(out)


def window_volume(path: Path, start: float, length: float) -> float:
    """mean_volume (dB) of a window, e.g. -91.0 for silence."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", result.stderr)
    if not match:
        raise RuntimeError("volumedetect produced no mean_volume:\n" + result.stderr[-2000:])
    return float(match.group(1))


def main() -> int:
    if os.environ.get("YT2ABS_E2E") != "1":
        print("E2E pominiete: ustaw YT2ABS_E2E=1 aby uruchomic (siec + yt-dlp + ffmpeg).")
        return 0

    video_id = os.environ.get("YT2ABS_E2E_VIDEO", DEFAULT_VIDEO)
    url = f"https://www.youtube.com/watch?v={video_id}"
    workdir = Path(tempfile.mkdtemp(prefix="yt2abs-e2e-"))
    print(f"film: {url}\nkatalog: {workdir}\n")

    # Reference data straight from the SponsorBlock API.
    segments = fetch_segments(video_id, ("sponsor",), api=DEFAULT_API)
    check(len(segments) >= 1, f"SponsorBlock API zwraca {len(segments)} segment(ow) 'sponsor' dla {video_id}")
    if not segments:
        return 1
    sponsor_start, sponsor_end = segments[0].start, segments[0].end
    sponsor_len = sponsor_end - sponsor_start
    print(f"      segment sponsor: {sponsor_start:.3f}-{sponsor_end:.3f} ({sponsor_len:.2f} s)")

    meta = probe_metadata(url, ytdlp=YTDLP)
    detected_channel = meta["channel_id"]
    check(bool(detected_channel), f"wykryty kanal filmu: {detected_channel}")

    extra_args = shlex.split(os.environ.get("YT2ABS_E2E_YTDLP_ARGS", ""))
    config_doc = {
        "defaults": {"retention_days": 14, "sponsorblock": "off", "ytdlp_args": extra_args},
        "channels": {
            detected_channel or "UC_UNKNOWN": {"name": "remove", "sponsorblock": "remove:sponsor"},
            "UC_E2E_MUTE": {"name": "mute", "sponsorblock": "mute:sponsor"},
        },
    }
    config = parse_config(config_doc)

    results = {}

    # 1) no setting -> today's behaviour, no sponsorblock flags at all.
    out_off = workdir / "off"
    res_off = download_for_channel(url, out_off, config, channel_id="UC_E2E_NONE", ytdlp=YTDLP, log=lambda m: None)
    file_off = Path(res_off.path or "")
    check(file_off.exists(), f"[off] plik pobrany: {file_off.name}")
    check(res_off.mode == "off", "[off] tryb off (domyslny)")
    check(not any(a.startswith("--sponsorblock") for a in res_off.ytdlp_args), "[off] brak flag sponsorblock w yt-dlp")
    duration_off = ffprobe_duration(file_off)
    results["off"] = {"file": str(file_off), "duration": duration_off, "mode": res_off.mode}
    print(f"      [off] dlugosc: {duration_off:.2f} s")

    # 2) per-channel remove -> yt-dlp cuts the segment out.
    out_remove = workdir / "remove"
    res_remove = download_for_channel(url, out_remove, config, channel_id=detected_channel, ytdlp=YTDLP, log=lambda m: None)
    file_remove = Path(res_remove.path or "")
    check(file_remove.exists(), f"[remove] plik pobrany: {file_remove.name}")
    check(res_remove.mode == "remove", "[remove] ustawienie kanalu uzyte")
    check("--sponsorblock-remove" in res_remove.ytdlp_args, "[remove] yt-dlp dostal --sponsorblock-remove")
    duration_remove = ffprobe_duration(file_remove)
    results["remove"] = {"file": str(file_remove), "duration": duration_remove, "mode": res_remove.mode}
    expected_remove = duration_off - sponsor_len
    check(
        abs(duration_remove - expected_remove) <= 1.5,
        f"[remove] dlugosc {duration_remove:.2f} s == {expected_remove:.2f} s (off - sponsor, tolerancja 1.5 s)",
    )

    # 3) per-channel mute -> same length, sponsor window silent.
    out_mute = workdir / "mute"
    res_mute = download_for_channel(url, out_mute, config, channel_id="UC_E2E_MUTE", ytdlp=YTDLP, log=lambda m: None)
    file_mute = Path(res_mute.path or "")
    check(file_mute.exists(), f"[mute] plik pobrany: {file_mute.name}")
    check(res_mute.mode == "mute" and res_mute.muted, "[mute] segment wyciszony lokalnie (ffmpeg)")
    check(not any(a.startswith("--sponsorblock") for a in res_mute.ytdlp_args), "[mute] yt-dlp bez flag sponsorblock")
    duration_mute = ffprobe_duration(file_mute)
    results["mute"] = {"file": str(file_mute), "duration": duration_mute, "mode": res_mute.mode, "muted": res_mute.muted}
    check(abs(duration_mute - duration_off) <= 1.0, f"[mute] dlugosc {duration_mute:.2f} s == {duration_off:.2f} s (bez ciecia)")

    window_start = sponsor_start + 1.0
    window_len = max(1.0, sponsor_len - 2.0)
    volume_mute = window_volume(file_mute, window_start, window_len)
    volume_off = window_volume(file_off, window_start, window_len)
    results["volume_in_sponsor_window_db"] = {"off": volume_off, "mute": volume_mute}
    check(volume_mute < -70.0, f"[mute] okno sponsora ciche (mean_volume {volume_mute:.1f} dB < -70 dB)")
    check(volume_off > -45.0, f"[off] okno sponsora z dzwiekiem (mean_volume {volume_off:.1f} dB > -45 dB)")

    report = {
        "video_id": video_id,
        "url": url,
        "channel_id": detected_channel,
        "sponsor_segment": {"start": sponsor_start, "end": sponsor_end},
        "results": results,
        "failures": failures,
    }
    report_path = Path(__file__).resolve().parents[1] / "docs" / "e2e-sponsorblock-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nraport: {report_path}")

    if os.environ.get("YT2ABS_E2E_KEEP") == "1":
        notes.append(f"pliki zostawione w {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\nWYNIK: {'OK' if not failures else 'BLAD'} ({len(failures)} niepowodzen)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
