from __future__ import annotations

import re
from typing import Any

from .text_protection import protect_text


HIGH_SEVERITY_QUALITY_FLAGS = {
    "SUMMARY_STYLE_TRANSLATION",
    "REVIEW_COMMENTARY_LEAK",
}

AWKWARD_ZH_PATTERNS: list[tuple[str, str]] = [
    (r"这一段(?:主要)?(?:是在|围绕|讲|讨论|介绍)", "SUMMARY_STYLE_TRANSLATION"),
    (r"(?:本段|该片段|这里主要)(?:是在|围绕|讲|讨论|介绍)", "SUMMARY_STYLE_TRANSLATION"),
    (r"请结合英文字幕复核|待人工复核", "REVIEW_COMMENTARY_LEAK"),
    (r"漏斗化漏斗化", "DUPLICATED_CALQUE"),
    (r"如何多层布线", "AWKWARD_TECHNICAL_CALQUE"),
    (r"(?:这个|那个|这些|那些)东西", "VAGUE_OBJECT_REFERENCE"),
    (r"被用来去|用来去|去进行|进行一个", "ENGLISH_WORD_ORDER_CALQUE"),
    (r"Suehart|WEdwardDimming|W\.?Edward(?:s)?Dimming|Stuart(?:和|与|and)Dimming", "KNOWN_NAME_ASR_CONFUSION"),
    (r"(?:SBC|SVC)(?:图表|charts?)", "KNOWN_SPC_CHART_ASR_CONFUSION"),
]
COMPACT_TARGET_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def translation_target_language(config: dict[str, Any] | None = None) -> str:
    translation = (config or {}).get("translation", {}) if isinstance((config or {}).get("translation", {}), dict) else {}
    return str(
        translation.get("target_language")
        or translation.get("target_subtitle_language")
        or translation.get("language")
        or "zh-CN"
    ).strip().lower()


def target_uses_compact_script(config: dict[str, Any] | None = None) -> bool:
    target = translation_target_language(config)
    return target.startswith(("zh", "yue", "cmn", "wuu", "ja", "jp", "ko")) or any(
        marker in target for marker in ["chinese", "cantonese", "mandarin", "japanese", "korean"]
    )


def assess_translation_quality(
    source: str,
    zh: str,
    literal: str = "",
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Return deterministic review flags for subtitle translation quality.

    These checks are intentionally conservative. They do not replace the local
    LLM fidelity audit; they make common failures visible in trace/report files
    even when the model returns syntactically valid Chinese.
    """
    flags: list[str] = []
    compact_zh = compact_text(zh)
    for pattern, flag in AWKWARD_ZH_PATTERNS:
        if re.search(pattern, compact_zh):
            flags.append(flag)

    if has_repeated_short_phrase(compact_zh):
        flags.append("REPEATED_PHRASE_REVIEW_REQUIRED")

    source_words = english_word_count(source)
    if source_words >= 10 and literal and compact_text(literal) == compact_zh:
        flags.append("LECTURE_REWRITE_UNCHANGED_REVIEW_REQUIRED")

    if target_uses_compact_script(config) and is_possibly_overcompressed(source, zh, config):
        flags.append("POSSIBLY_OVERCOMPRESSED_TRANSLATION")

    if target_uses_compact_script(config) and untranslated_ascii_ratio(zh) > float((config or {}).get("qa", {}).get("flag_untranslated_ascii_ratio", 0.5)):
        flags.append("HIGH_ASCII_RATIO_TRANSLATION")

    return sorted(set(flags))


def protected_terms_missing(source: str, translated: str) -> list[str]:
    """Return source technical tokens that should survive translation intact."""
    terms = protected_source_terms(source)
    missing: list[str] = []
    for term in terms:
        if not protected_term_present(term, translated):
            missing.append(term)
    return missing


def protected_source_terms(source: str) -> list[str]:
    protected = protect_text(source or "")
    terms: list[str] = []
    seen: set[str] = set()
    for raw in protected.mapping.values():
        term = raw.strip()
        if not term:
            continue
        normalized = normalize_protected_term(term)
        if not normalized or normalized in seen:
            continue
        terms.append(term)
        seen.add(normalized)
    return terms


def protected_term_present(term: str, translated: str) -> bool:
    text = translated or ""
    variants = protected_term_variants(term)
    compact_text_value = compact_protected_value(text)
    return any(variant in text or compact_protected_value(variant) in compact_text_value for variant in variants)


def protected_term_variants(term: str) -> list[str]:
    stripped = term.strip()
    variants = [stripped]
    if len(stripped) >= 2 and stripped[0] == "`" and stripped[-1] == "`":
        variants.append(stripped[1:-1])
    return [variant for variant in variants if variant]


def normalize_protected_term(term: str) -> str:
    return compact_protected_value(term).lower()


def compact_protected_value(value: str) -> str:
    return re.sub(r"[\s`]+", "", value or "")


def quality_flag_severity(flags: list[str]) -> str:
    return "high" if any(flag in HIGH_SEVERITY_QUALITY_FLAGS for flag in flags) else "medium"


def is_possibly_overcompressed(source: str, zh: str, config: dict[str, Any] | None = None) -> bool:
    if not target_uses_compact_script(config):
        return False
    source_words = english_word_count(source)
    if source_words < int((config or {}).get("qa", {}).get("overcompression_min_source_words", 16)):
        return False
    zh_cjk = len(COMPACT_TARGET_SCRIPT_RE.findall(zh or ""))
    ratio = float((config or {}).get("qa", {}).get("overcompression_min_zh_per_en_word", 0.45))
    return zh_cjk < max(8, int(source_words * ratio))


def untranslated_ascii_ratio(text: str) -> float:
    letters = sum(1 for ch in text or "" if ch.isascii() and ch.isalpha())
    return letters / max(1, len(text or ""))


def has_repeated_short_phrase(text: str) -> bool:
    # Catch obvious TTS/LLM stutters such as "多层布线多层布线" without flagging
    # normal two-character emphasis.
    return bool(re.search(r"([\u4e00-\u9fff]{3,8})\1", text or ""))


def english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$%.-]+", text or ""))


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")
