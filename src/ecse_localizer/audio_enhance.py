from __future__ import annotations

import logging
from pathlib import Path

from .utils import run_cmd


def enhance_audio_ffmpeg(src_wav: str | Path, dst_wav: str | Path, logger: logging.Logger | None = None) -> str:
    filters = "highpass=f=80,lowpass=f=7600,loudnorm=I=-16:TP=-1.5:LRA=11"
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
            str(src_wav),
            "-af",
            filters,
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
    return "ffmpeg_loudnorm_highpass_lowpass"


def enhance_audio(src_wav: str | Path, dst_wav: str | Path, config: dict, logger: logging.Logger | None = None) -> str:
    order = config.get("audio", {}).get("enhancement_backend_order", ["ffmpeg"])
    notes: list[str] = []
    for backend in order:
        if backend == "ffmpeg":
            return enhance_audio_ffmpeg(src_wav, dst_wav, logger)
        notes.append(f"{backend}: not installed")
    if logger:
        logger.warning("Audio enhancement optional backends unavailable: %s", "; ".join(notes))
    return enhance_audio_ffmpeg(src_wav, dst_wav, logger)
