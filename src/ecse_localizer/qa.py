from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .align import overlap_count
from .ffmpeg_utils import media_duration
from .glossary import GlossaryTerm
from .subtitle_io import Segment
from .translation_quality import assess_translation_quality, quality_flag_severity, target_uses_compact_script


COMPACT_TRANSLATION_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def run_qa(
    outputs: dict[str, str],
    en_segments: list[Segment],
    zh_segments: list[Segment],
    glossary: dict[str, GlossaryTerm],
    traces: list[Any],
    tts_info: dict[str, Any],
    video_duration: float,
    config: dict,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for name, path in outputs.items():
        if not Path(path).exists():
            issues.append({"type": "missing_output", "severity": "high", "path": path, "name": name})

    if overlap_count(en_segments):
        issues.append({"type": "en_overlap", "severity": "high", "count": overlap_count(en_segments)})
    if overlap_count(zh_segments):
        issues.append({"type": "zh_overlap", "severity": "high", "count": overlap_count(zh_segments)})

    empty_zh = [s.id for s in zh_segments if not s.text.strip()]
    if empty_zh:
        issues.append({"type": "empty_translation", "severity": "high", "segments": empty_zh[:20]})

    for en, zh in zip(en_segments, zh_segments):
        if not has_usable_translation_text(zh.text, config):
            issues.append({"type": "invalid_translation_text", "severity": "high", "segment_id": zh.id, "text": zh.text})
        if looks_like_non_translation_narration(zh.text):
            issues.append({"type": "non_translation_narration", "severity": "high", "segment_id": zh.id, "text": zh.text})
        original_nums = re.findall(r"\d+(?:\.\d+)?", en.text)
        zh_nums = re.findall(r"\d+(?:\.\d+)?", zh.text)
        missing = [n for n in original_nums if n not in zh_nums]
        if missing:
            issues.append({"type": "number_mismatch", "severity": "medium", "segment_id": en.id, "missing": missing})
        ascii_letters = sum(1 for ch in zh.text if ch.isascii() and ch.isalpha())
        if target_uses_compact_script(config) and ascii_letters / max(1, len(zh.text)) > float(config.get("qa", {}).get("flag_untranslated_ascii_ratio", 0.5)):
            issues.append({"type": "possibly_untranslated", "severity": "medium", "segment_id": zh.id})
        quality_flags = assess_translation_quality(en.text, zh.text, "", config)
        if quality_flags:
            issues.append(
                {
                    "type": "translation_quality_heuristic",
                    "severity": quality_flag_severity(quality_flags),
                    "segment_id": zh.id,
                    "flags": quality_flags,
                }
            )

    tts_duration = float(tts_info.get("duration") or 0)
    if abs(tts_duration - video_duration) > max(2.0, video_duration * 0.03):
        issues.append(
            {
                "type": "tts_duration_deviation",
                "severity": "medium",
                "tts_duration": tts_duration,
                "video_duration": video_duration,
            }
        )
    for flag in tts_info.get("flags", []):
        issues.append({"type": "tts_overrun", "severity": "medium", **flag})
    prevented_overlaps = int(tts_info.get("would_overlap_without_prevention_count") or 0)
    if prevented_overlaps:
        issues.append(
            {
                "type": "tts_audio_overlap_prevented",
                "severity": "medium",
                "count": prevented_overlaps,
                "max_audio_delay_seconds": float(tts_info.get("max_audio_delay_seconds") or 0),
            }
        )
    truncated_audio = int(tts_info.get("truncated_audio_count") or 0)
    if truncated_audio:
        issues.append({"type": "tts_audio_truncated", "severity": "medium", "count": truncated_audio})
    delay_warning = float(config.get("tts", {}).get("max_audio_delay_warning_seconds", 1.5))
    max_delay = float(tts_info.get("max_audio_delay_seconds") or 0)
    if max_delay > delay_warning:
        issues.append({"type": "tts_audio_delay_high", "severity": "medium", "max_audio_delay_seconds": max_delay, "threshold": delay_warning})

    mp4 = outputs.get("zh_dub_mp4")
    if mp4 and Path(mp4).exists():
        try:
            media_duration(mp4)
        except Exception as exc:
            issues.append({"type": "ffprobe_failed", "severity": "high", "path": mp4, "error": str(exc)})

    first_ten = [
        {"id": en.id, "start": en.start, "end": en.end, "en": en.text, "zh": zh.text}
        for en, zh in list(zip(en_segments, zh_segments))[:10]
    ]
    glossary_sample = [
        {
            "source_term": t.source_term,
            "zh_term": t.zh_term,
            "type": t.type,
            "confidence": t.confidence,
        }
        for t in list(sorted(glossary.values(), key=lambda x: (-x.confidence, x.source_term.lower())))[:10]
    ]
    trace_flags = collect_trace_flags(traces)
    actionable_trace_flags = {flag: count for flag, count in trace_flags.items() if is_actionable_trace_flag(flag)}
    if actionable_trace_flags:
        issues.append({"type": "translation_trace_flags", "severity": "medium", "flags": actionable_trace_flags})
    if config.get("qa", {}).get("fail_on_rule_fallback_translation", True):
        fallback_flags = [
            "LOCAL_RULE_FALLBACK_REVIEW_REQUIRED",
            "LLM_CHUNK_FAILED_FALLBACK",
            "LOW_CAPACITY_LLM_BYPASSED_FOR_LONG_VIDEO",
        ]
        matched = {flag: trace_flags[flag] for flag in fallback_flags if flag in trace_flags}
        if matched:
            issues.append({"type": "translation_fallback_used", "severity": "high", "flags": matched})

    return {
        "pass": not any(i.get("severity") == "high" for i in issues),
        "issues": issues,
        "first_10_subtitles": first_ten,
        "glossary_sample": glossary_sample,
        "tts": tts_info,
        "video_duration": video_duration,
        "trace_flags": trace_flags,
        "actionable_trace_flags": actionable_trace_flags,
        "translation_flag_samples": collect_trace_flag_samples(traces),
    }


def collect_trace_flags(traces: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trace in traces:
        for flag in trace_value(trace, "flags", []) or []:
            if str(flag).startswith("KEEP_") or "'type': 'KEEP'" in str(flag):
                continue
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def collect_trace_flag_samples(traces: list[Any], limit: int = 12) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for trace in traces:
        flags = [
            str(flag)
            for flag in trace_value(trace, "flags", []) or []
            if not str(flag).startswith("KEEP_") and "'type': 'KEEP'" not in str(flag)
        ]
        actionable = [flag for flag in flags if is_actionable_trace_flag(flag)]
        if not actionable:
            continue
        samples.append(
            {
                "segment_id": trace_value(trace, "segment_id", None),
                "flags": actionable,
                "original_text": trace_value(trace, "original_text", ""),
                "zh_literal": trace_value(trace, "zh_literal", ""),
                "zh_lecture": trace_value(trace, "zh_lecture", ""),
                "paragraph_id": trace_value(trace, "paragraph_id", None),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def trace_value(trace: Any, key: str, default: Any = None) -> Any:
    if isinstance(trace, dict):
        return trace.get(key, default)
    return getattr(trace, key, default)


def is_actionable_trace_flag(flag: str) -> bool:
    text = str(flag or "")
    if not text:
        return False
    prefixes = (
        "MISSING_NUMBER",
        "MISSING_PROTECTED_TERM",
        "ZH_OVER_TARGET_LENGTH",
        "HIGH_ASCII_RATIO_TRANSLATION",
        "POSSIBLY_OVERCOMPRESSED_TRANSLATION",
        "LECTURE_REWRITE_UNCHANGED_REVIEW_REQUIRED",
        "SUMMARY_STYLE_TRANSLATION",
        "REVIEW_COMMENTARY_LEAK",
        "REPEATED_PHRASE_REVIEW_REQUIRED",
        "AWKWARD_TECHNICAL_CALQUE",
        "VAGUE_OBJECT_REFERENCE",
        "ENGLISH_WORD_ORDER_CALQUE",
        "DUPLICATED_CALQUE",
        "LOW_CAPACITY_LLM_REVIEW_REQUIRED",
        "LOW_CAPACITY_LLM_BYPASSED_FOR_LONG_VIDEO",
        "LOCAL_RULE_FALLBACK_REVIEW_REQUIRED",
        "LLM_CHUNK_FAILED_FALLBACK",
    )
    return text.startswith(prefixes)


def has_usable_translation_text(text: str, config: dict[str, Any] | None = None) -> bool:
    stripped = (text or "").strip()
    if stripped in {"...", "…", "N/A", "null", "None"}:
        return False
    if re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE) in {"对", "是", "好", "嗯", "行"}:
        return True
    non_punct = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    if target_uses_compact_script(config):
        if not COMPACT_TRANSLATION_SCRIPT_RE.search(stripped):
            return False
        return len(non_punct) >= 2
    if not any(ch.isalpha() for ch in stripped):
        return False
    return len(non_punct) >= 2


def has_usable_chinese(text: str) -> bool:
    return has_usable_translation_text(text, {"translation": {"target_language": "zh-CN"}})


def looks_like_non_translation_narration(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text or "")
    forbidden = [
        "这一段是在",
        "这一段主要围绕",
        "请结合英文字幕复核",
        "本段主要",
        "该片段主要",
    ]
    return any(token in stripped for token in forbidden)
