from __future__ import annotations

import logging
from pathlib import Path

from .subtitle_io import Segment


class BackendUnavailable(RuntimeError):
    pass


def transcribe_audio(audio_path: str | Path, config: dict, logger: logging.Logger | None = None) -> tuple[list[Segment], str]:
    order = config.get("asr", {}).get("backend_order", ["faster_whisper"])
    last_error = ""
    for backend in order:
        if backend == "faster_whisper":
            try:
                return transcribe_faster_whisper(audio_path, config, logger), "faster_whisper"
            except Exception as exc:
                last_error = f"faster_whisper failed: {exc}"
                if logger:
                    logger.warning(last_error)
        elif backend == "whisperx":
            last_error = "whisperx optional backend is not installed in this project"
            if logger:
                logger.info(last_error)
    raise BackendUnavailable(last_error or "No ASR backend available")


def transcribe_faster_whisper(audio_path: str | Path, config: dict, logger: logging.Logger | None = None) -> list[Segment]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise BackendUnavailable("Install faster-whisper to ASR videos without subtitles") from exc

    asr_cfg = config.get("asr", {})
    model_name = asr_cfg.get("model", "large-v3")
    device = config.get("gpu", {}).get("device", "cuda")
    compute_type = asr_cfg.get("compute_type", "float16")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception:
        if not config.get("gpu", {}).get("oom_fallback", True):
            raise
        model = WhisperModel("medium", device=device, compute_type=asr_cfg.get("fallback_compute_type", "int8_float16"))
    segments, _info = model.transcribe(
        str(audio_path),
        language=asr_cfg.get("language", "en"),
        vad_filter=bool(asr_cfg.get("vad", True)),
        word_timestamps=bool(asr_cfg.get("word_timestamps", True)),
    )
    out: list[Segment] = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            out.append(Segment(len(out) + 1, float(seg.start), float(seg.end), text))
    if logger:
        logger.info("ASR produced %d segments", len(out))
    return out
