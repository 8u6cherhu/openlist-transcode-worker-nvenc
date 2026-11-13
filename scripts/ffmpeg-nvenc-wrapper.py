#!/usr/bin/env python3
"""Inject optional NVENC output options without changing the upstream worker."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CONFIG = Path(os.environ.get("OPENLIST_CONFIG_FILE", "/var/lib/openlist-worker/config.json"))
REAL_FFMPEG = os.environ.get("OPENLIST_REAL_FFMPEG", "/usr/bin/ffmpeg")


def read_config() -> dict:
    try:
        with CONFIG.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def option_present(args: list[str], names: set[str]) -> bool:
    return any(arg in names for arg in args)


def add_if_missing(out: list[str], args: list[str], names: set[str], *values: str) -> None:
    if not option_present(args, names):
        out.extend(values)


def option_value(args: list[str], names: set[str]) -> str | None:
    for index, arg in enumerate(args[:-1]):
        if arg in names:
            return args[index + 1]
    return None


def rewrite_worker_source_origin(args: list[str], config: dict) -> list[str]:
    """Use the Worker-to-Master origin while preserving the signed URL path."""
    worker_config = config.get("worker", {})
    if not isinstance(worker_config, dict):
        return list(args)
    master = worker_config.get("master")
    if not isinstance(master, str) or not master:
        return list(args)
    try:
        master_url = urlsplit(master)
    except ValueError:
        return list(args)
    if master_url.scheme not in {"http", "https"} or not master_url.netloc:
        return list(args)
    master_prefix = master_url.path.rstrip("/")
    if not master_prefix:
        return list(args)

    out = list(args)
    source_index = next(
        (index + 1 for index, arg in enumerate(out[:-1]) if arg == "-i"),
        None,
    )
    if source_index is None:
        return out
    try:
        source_url = urlsplit(out[source_index])
    except ValueError:
        return out
    if (
        source_url.scheme not in {"http", "https"}
        or not source_url.netloc
        or not source_url.path.startswith(master_prefix + "/")
    ):
        return out
    out[source_index] = urlunsplit(
        (
            master_url.scheme,
            master_url.netloc,
            source_url.path,
            source_url.query,
            source_url.fragment,
        )
    )
    return out


def main() -> int:
    args = sys.argv[1:]
    if not args:
        os.execv(REAL_FFMPEG, [REAL_FFMPEG])

    config = read_config()
    args = rewrite_worker_source_origin(args, config)

    # The upstream worker puts -c:v before the HLS output. Only touch NVENC jobs.
    codec = ""
    for index, arg in enumerate(args[:-1]):
        if arg in ("-c:v", "-vcodec", "-c:v:0"):
            codec = args[index + 1].lower()
            break
    if not codec.endswith("_nvenc"):
        os.execv(REAL_FFMPEG, [REAL_FFMPEG, *args])

    config = config.get("nvenc", {})
    if not isinstance(config, dict) or not config.get("enabled", True):
        os.execv(REAL_FFMPEG, [REAL_FFMPEG, *args])

    # Insert before the final HLS playlist path so all options apply to the output.
    output_index = len(args)
    if output_index:
        output_index -= 1
    injected: list[str] = []
    preset = str(config.get("preset", "")).strip()
    if preset:
        add_if_missing(injected, args, {"-preset"}, "-preset", preset)
    tune = str(config.get("tune", "")).strip()
    if tune:
        add_if_missing(injected, args, {"-tune"}, "-tune", tune)
    rc = str(config.get("rc", "")).strip()
    if rc:
        add_if_missing(injected, args, {"-rc"}, "-rc", rc)
    cq = config.get("cq", 0)
    if cq not in (None, "", 0, "0") and str(cq).isdigit():
        add_if_missing(injected, args, {"-cq"}, "-cq", str(cq))
    multipass = str(config.get("multipass", "")).strip()
    if multipass:
        add_if_missing(injected, args, {"-multipass"}, "-multipass", multipass)
    lookahead = config.get("lookahead", 0)
    if bool(lookahead):
        add_if_missing(injected, args, {"-look_ahead", "-lookahead"}, "-look_ahead", "1")
    bf = config.get("bf", None)
    if bf not in (None, ""):
        bf_text = str(bf).strip()
        if bf_text.isdigit():
            add_if_missing(injected, args, {"-bf"}, "-bf", bf_text)
    # OpenList's HLS uploader records the configured segment duration. NVENC
    # otherwise follows the source GOP, which can be much longer than
    # -hls_time and produces segments whose internal duration disagrees with
    # the playlist metadata. Force video keyframes at the HLS interval when
    # the upstream command has not already supplied a keyframe policy.
    hls_time = option_value(args, {"-hls_time"})
    if hls_time and not option_present(args, {"-force_key_frames", "-force_keyframe"}):
        try:
            segment_seconds = float(hls_time)
        except ValueError:
            segment_seconds = 0
        if segment_seconds > 0:
            expression = f"expr:gte(t,n_forced*{segment_seconds:g})"
            injected.extend(("-force_key_frames", expression))
            add_if_missing(injected, args, {"-forced-idr"}, "-forced-idr", "1")
    b_ref_mode = str(config.get("b_ref_mode", "")).strip()
    if b_ref_mode:
        add_if_missing(injected, args, {"-b_ref_mode"}, "-b_ref_mode", b_ref_mode)
    if bool(config.get("spatial_aq", False)):
        add_if_missing(injected, args, {"-spatial_aq"}, "-spatial_aq", "1")
    if bool(config.get("temporal_aq", False)):
        add_if_missing(injected, args, {"-temporal_aq"}, "-temporal_aq", "1")
    aq_strength = config.get("aq_strength", 0)
    if aq_strength not in (None, "", 0, "0") and str(aq_strength).isdigit():
        add_if_missing(injected, args, {"-aq-strength"}, "-aq-strength", str(aq_strength))
    # H.264 NVENC on the target GPU cannot encode 10-bit input. Keep the
    # conversion scoped to H.264; HEVC jobs retain the source/profile choice.
    if codec == "h264_nvenc":
        add_if_missing(injected, args, {"-pix_fmt"}, "-pix_fmt", "yuv420p")

    final_args = args[:output_index] + injected + args[output_index:]
    if os.environ.get("OPENLIST_LOG_FFMPEG_ARGS", "0") == "1":
        print("[ffmpeg-wrapper]", REAL_FFMPEG, *final_args, file=sys.stderr, flush=True)
    os.execv(REAL_FFMPEG, [REAL_FFMPEG, *final_args])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
