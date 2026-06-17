from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .utils import run_cmd


def ffprobe_json(path: str | Path) -> dict[str, Any]:
    proc = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        check=True,
    )
    return json.loads(proc.stdout)


def media_duration(path: str | Path) -> float:
    info = ffprobe_json(path)
    duration = info.get("format", {}).get("duration")
    if duration is None:
        for stream in info.get("streams", []):
            if stream.get("duration"):
                return float(stream["duration"])
        return 0.0
    return float(duration)


def cut_video(src: str | Path, dst: str | Path, seconds: int, logger: logging.Logger | None = None) -> None:
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-t",
            str(seconds),
            "-map",
            "0",
            "-c",
            "copy",
            str(dst),
        ],
        logger=logger,
    )


def extract_audio(src: str | Path, dst_wav: str | Path, logger: logging.Logger | None = None) -> None:
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(dst_wav),
        ],
        logger=logger,
    )


def audio_duration(path: str | Path) -> float:
    return media_duration(path)


def has_audio_stream(path: str | Path) -> bool:
    info = ffprobe_json(path)
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def video_summary(path: str | Path) -> dict[str, Any]:
    info = ffprobe_json(path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    return {
        "path": str(path),
        "duration": float(info.get("format", {}).get("duration") or 0),
        "size_bytes": int(info.get("format", {}).get("size") or 0),
        "resolution": f"{video.get('width', 0)}x{video.get('height', 0)}" if video else "",
        "video_codec": video.get("codec_name", ""),
        "audio_tracks": len(audio),
        "audio_codecs": [a.get("codec_name", "") for a in audio],
    }
