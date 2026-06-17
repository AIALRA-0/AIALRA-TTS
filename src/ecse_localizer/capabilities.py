from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .llm_local import LocalLLMStatus
from .tts import tts_health


DEFAULT_ASR_LANGUAGES = [
    "auto",
    "en",
    "zh",
    "zh-CN",
    "yue",
    "ja",
    "ko",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "ru",
    "ar",
    "hi",
]
DEFAULT_TRANSLATION_TARGETS = [
    "zh-CN",
    "zh-TW",
    "zh-HK",
    "en",
    "ja",
    "ko",
    "es",
    "fr",
    "de",
    "it",
    "pt",
]
COSYVOICE_LANGUAGE_HINTS = ["zh", "zh-CN", "cmn", "mandarin", "yue", "cantonese"]


def language_capabilities(
    config: dict[str, Any],
    *,
    llm_status: LocalLLMStatus | dict[str, Any] | None = None,
    tts_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tts_status = tts_status or tts_health(config)
    llm = asdict(llm_status) if isinstance(llm_status, LocalLLMStatus) else (llm_status or {})
    asr_cfg = config.get("asr", {})
    translation_cfg = config.get("translation", {})
    tts_cfg = config.get("tts", {})

    asr_language = asr_cfg.get("language", "auto")
    asr_languages = configured_languages(asr_cfg, "supported_languages", DEFAULT_ASR_LANGUAGES)
    translation_targets = configured_languages(translation_cfg, "supported_target_languages", DEFAULT_TRANSLATION_TARGETS)
    tts_languages = supported_tts_languages(config, tts_status)
    tts_language = str(tts_cfg.get("language") or translation_cfg.get("target_language") or "zh-CN")
    translation_target = str(translation_cfg.get("target_language") or "zh-CN")

    llm_available = bool(llm.get("available"))
    return {
        "asr": {
            "backend_order": list(asr_cfg.get("backend_order", ["whisperx", "faster_whisper"])),
            "language": asr_language if asr_language is not None else "auto",
            "auto_detect": asr_language in (None, "", "auto"),
            "supported_languages": asr_languages,
            "current_supported": language_supported(asr_languages, "auto" if asr_language in (None, "", "auto") else str(asr_language)),
            "notes": "Whisper-class local ASR can auto-detect many spoken languages; explicit support can be overridden in config.",
        },
        "translation": {
            "backend": llm.get("backend") or "none",
            "available": llm_available,
            "model": llm.get("model"),
            "target_language": translation_target,
            "supported_target_languages": translation_targets,
            "supports_arbitrary_targets": llm_available,
            "current_supported": llm_available and (
                language_supported(translation_targets, translation_target) or bool(translation_cfg.get("allow_unlisted_targets", True))
            ),
            "notes": "Local LLM translation can attempt unlisted target languages, but unlisted targets should be treated as QA-required.",
        },
        "tts": {
            "backend": tts_status.get("backend") or "none",
            "language": tts_language,
            "supported_languages": tts_languages,
            "current_supported": language_supported(tts_languages, tts_language),
            "voice": tts_cfg.get("default_voice") or tts_cfg.get("cosyvoice_speaker") or "",
            "notes": tts_language_note(tts_status, tts_languages),
        },
    }


def configured_languages(config_section: dict[str, Any], key: str, fallback: list[str]) -> list[str]:
    value = config_section.get(key)
    if isinstance(value, list):
        langs = [str(item).strip() for item in value if str(item).strip()]
        return unique_languages(langs or fallback)
    if isinstance(value, str) and value.strip():
        return unique_languages([item.strip() for item in value.split(",") if item.strip()])
    return unique_languages(fallback)


def supported_tts_languages(config: dict[str, Any], tts_status: dict[str, Any]) -> list[str]:
    tts_cfg = config.get("tts", {})
    configured = configured_languages(tts_cfg, "supported_languages", [])
    if configured:
        return configured
    backend = str(tts_status.get("backend") or "")
    if backend == "cosyvoice_sft":
        return unique_languages(COSYVOICE_LANGUAGE_HINTS)
    if backend == "piper":
        inferred = infer_piper_language(str(tts_status.get("piper_model") or tts_cfg.get("piper_model") or ""))
        return unique_languages([inferred] if inferred else [])
    return []


def language_supported(supported_languages: list[str], requested_language: str | None) -> bool:
    if not requested_language:
        return False
    requested = normalize_language(requested_language)
    for item in supported_languages:
        supported = normalize_language(item)
        if supported in {"*", "any"}:
            return True
        if requested == supported:
            return True
        if requested.split("-")[0] == supported.split("-")[0] and supported.split("-")[0] in {"zh", "en", "ja", "ko"}:
            return True
    return False


def normalize_language(value: str) -> str:
    text = str(value or "").strip().replace("_", "-").lower()
    aliases = {
        "auto": "auto",
        "mandarin": "zh-cn",
        "cmn": "zh-cn",
        "cantonese": "yue",
        "zh-hans": "zh-cn",
        "zh-cn": "zh-cn",
        "zh-sg": "zh-cn",
        "zh-hant": "zh-tw",
        "zh-tw": "zh-tw",
        "zh-hk": "zh-hk",
    }
    return aliases.get(text, text)


def unique_languages(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = normalize_language(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def infer_piper_language(model_path: str) -> str | None:
    name = Path(model_path).name
    match = re.match(r"([a-z]{2,3})(?:[_-]([A-Z]{2}|[a-z]{2}))?", name)
    if not match:
        return None
    lang = match.group(1)
    region = match.group(2)
    if region:
        return f"{lang}-{region.upper()}"
    return lang


def tts_language_note(tts_status: dict[str, Any], supported_languages: list[str]) -> str:
    backend = str(tts_status.get("backend") or "none")
    if backend == "ffmpeg_tone_fallback":
        return "No real local TTS model is currently available; tone fallback is for pipeline testing only."
    if supported_languages:
        return "Supported TTS languages are inferred from the active local TTS backend or tts.supported_languages."
    return "No supported TTS language list could be inferred; configure tts.supported_languages before using this backend for public jobs."
