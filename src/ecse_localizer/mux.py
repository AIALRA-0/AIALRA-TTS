from __future__ import annotations

import logging
from pathlib import Path

from .utils import path_for_ffmpeg_filter, run_cmd


def mux_video(video: str | Path, zh_audio: str | Path, out_mp4: str | Path, config: dict, logger: logging.Logger | None = None) -> None:
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
            str(video),
            "-i",
            str(zh_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(out_mp4),
        ],
        logger=logger,
    )


def hardsub_video(in_mp4: str | Path, ass_path: str | Path, out_mp4: str | Path, logger: logging.Logger | None = None) -> bool:
    try:
        ass = path_for_ffmpeg_filter(ass_path)
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
                str(in_mp4),
                "-vf",
                f"ass='{ass}'",
                "-c:a",
                "copy",
                str(out_mp4),
            ],
            logger=logger,
        )
        return True
    except Exception as exc:
        if logger:
            logger.warning("Hard subtitle export failed: %s", exc)
        return False
