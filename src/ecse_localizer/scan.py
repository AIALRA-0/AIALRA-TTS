from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .ffmpeg_utils import video_summary
from .subtitle_io import read_subtitles


VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
SUBTITLE_SUFFIXES = {".srt", ".vtt", ".ass"}
SKIP_DIRS = {"_localizer_project", "_localizer_output", "tools", ".venv", "__pycache__"}


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS or part.startswith(".") for part in path.parts)


def find_videos(input_dir: str | Path) -> list[Path]:
    root = Path(input_dir)
    videos = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and not should_skip(p.relative_to(root))
    ]
    return sorted(videos, key=lambda p: p.name.lower())


def find_subtitles(input_dir: str | Path) -> list[Path]:
    root = Path(input_dir)
    subs = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUBTITLE_SUFFIXES and not should_skip(p.relative_to(root))
    ]
    return sorted(subs, key=lambda p: p.name.lower())


def matching_subtitles(video: str | Path) -> list[Path]:
    video = Path(video)
    matches: list[Path] = []
    for suffix in SUBTITLE_SUFFIXES:
        candidate = video.with_suffix(suffix)
        if candidate.exists():
            matches.append(candidate)
    prefix = video.stem
    for p in video.parent.glob(f"{prefix}*"):
        if p.suffix.lower() in SUBTITLE_SUFFIXES and p not in matches:
            matches.append(p)
    return sorted(matches, key=lambda p: (p.suffix != ".vtt", p.name.lower()))


def subtitle_quality(path: Path, video_duration: float) -> dict[str, Any]:
    try:
        segments = read_subtitles(path)
    except Exception as exc:
        return {"path": str(path), "ok": False, "error": str(exc), "segments": 0, "coverage": 0.0}
    if not segments:
        return {"path": str(path), "ok": False, "segments": 0, "coverage": 0.0}
    coverage = min(1.0, max(s.end for s in segments) / video_duration) if video_duration else 0.0
    overlaps = sum(1 for a, b in zip(segments, segments[1:]) if b.start < a.end)
    empty = sum(1 for s in segments if not s.text.strip())
    return {
        "path": str(path),
        "ok": coverage > 0.2 and overlaps == 0 and empty == 0,
        "segments": len(segments),
        "coverage": coverage,
        "overlaps": overlaps,
        "empty": empty,
    }


def audit_input(input_dir: str | Path, logger: logging.Logger | None = None) -> dict[str, Any]:
    videos = find_videos(input_dir)
    subtitles = find_subtitles(input_dir)
    records: list[dict[str, Any]] = []
    for video in videos:
        try:
            summary = video_summary(video)
        except Exception as exc:
            summary = {"path": str(video), "error": str(exc), "duration": 0.0, "resolution": "", "audio_tracks": 0}
            if logger:
                logger.exception("ffprobe failed for %s", video)
        subs = matching_subtitles(video)
        summary["subtitles"] = [subtitle_quality(s, float(summary.get("duration") or 0)) for s in subs]
        summary["needs_asr"] = not any(s.get("ok") for s in summary["subtitles"])
        records.append(summary)
    return {
        "input_dir": str(input_dir),
        "video_count": len(videos),
        "subtitle_count": len(subtitles),
        "videos": records,
    }


def select_existing_subtitle(video: str | Path, duration: float) -> Path | None:
    candidates = matching_subtitles(video)
    for candidate in candidates:
        q = subtitle_quality(candidate, duration)
        if q.get("ok"):
            return candidate
    return candidates[0] if candidates else None
