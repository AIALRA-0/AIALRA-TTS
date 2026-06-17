from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .subtitle_io import Segment


class BackendUnavailable(RuntimeError):
    pass


WHISPER_LANGUAGE_ALIASES = {
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hk": "zh",
    "cmn": "zh",
    "mandarin": "zh",
    "chinese": "zh",
    "yue": "zh",
    "cantonese": "zh",
    "en-us": "en",
    "en-gb": "en",
    "jp": "ja",
    "japanese": "ja",
    "kr": "ko",
    "korean": "ko",
}


def transcribe_audio(audio_path: str | Path, config: dict, logger: logging.Logger | None = None) -> tuple[list[Segment], str]:
    segments, backend, _metadata = transcribe_audio_with_metadata(audio_path, config, logger)
    return segments, backend


def transcribe_audio_with_metadata(
    audio_path: str | Path,
    config: dict,
    logger: logging.Logger | None = None,
) -> tuple[list[Segment], str, dict[str, Any]]:
    order = config.get("asr", {}).get("backend_order", ["faster_whisper"])
    last_error = ""
    for backend in order:
        if backend == "faster_whisper":
            try:
                segments, metadata = transcribe_faster_whisper_with_metadata(audio_path, config, logger)
                return segments, "faster_whisper", metadata
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
    segments, _metadata = transcribe_faster_whisper_with_metadata(audio_path, config, logger)
    return segments


def transcribe_faster_whisper_with_metadata(
    audio_path: str | Path,
    config: dict,
    logger: logging.Logger | None = None,
) -> tuple[list[Segment], dict[str, Any]]:
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
        model_name = "medium"
        compute_type = asr_cfg.get("fallback_compute_type", "int8_float16")
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(audio_path),
        language=asr_language(config),
        vad_filter=bool(asr_cfg.get("vad", True)),
        word_timestamps=bool(asr_cfg.get("word_timestamps", True)),
    )
    out: list[Segment] = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            out.append(Segment(len(out) + 1, float(seg.start), float(seg.end), text))
    metadata = asr_metadata_from_info(info, config, model_name=model_name, device=device, compute_type=compute_type)
    if logger:
        logger.info(
            "ASR produced %d segments; requested_language=%s detected_language=%s probability=%s",
            len(out),
            metadata.get("requested_language"),
            metadata.get("detected_language"),
            metadata.get("language_probability"),
        )
    return out, metadata


def asr_language(config: dict) -> str | None:
    requested = requested_asr_language(config)
    if requested == "auto":
        return None
    return whisper_language_code(requested)


def asr_language_label(config: dict) -> str:
    return requested_asr_language(config)


def requested_asr_language(config: dict) -> str:
    value = config.get("asr", {}).get("language", "auto")
    if value is None:
        return "auto"
    text = str(value).strip()
    if not text or text.lower() == "auto":
        return "auto"
    return text


def whisper_language_code(language: str) -> str:
    text = str(language or "").strip()
    lowered = text.lower().replace("_", "-")
    return WHISPER_LANGUAGE_ALIASES.get(lowered, lowered)


def asr_metadata_from_info(
    info: Any,
    config: dict,
    *,
    model_name: str,
    device: str,
    compute_type: str,
) -> dict[str, Any]:
    return {
        "requested_language": asr_language_label(config),
        "backend_language": asr_language(config) or "auto",
        "detected_language": info_value(info, "language"),
        "language_probability": rounded_float(info_value(info, "language_probability")),
        "duration": rounded_float(info_value(info, "duration")),
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "vad": bool(config.get("asr", {}).get("vad", True)),
        "word_timestamps": bool(config.get("asr", {}).get("word_timestamps", True)),
    }


def info_value(info: Any, key: str) -> Any:
    if isinstance(info, dict):
        return info.get(key)
    return getattr(info, key, None)


def rounded_float(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None
