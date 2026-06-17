from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .align import overlap_count
from .ffmpeg_utils import media_duration
from .glossary import GlossaryTerm
from .subtitle_io import Segment
from .translation_quality import assess_translation_quality, quality_flag_severity


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
        if not has_usable_chinese(zh.text):
            issues.append({"type": "invalid_translation_text", "severity": "high", "segment_id": zh.id, "text": zh.text})
        if looks_like_non_translation_narration(zh.text):
            issues.append({"type": "non_translation_narration", "severity": "high", "segment_id": zh.id, "text": zh.text})
        original_nums = re.findall(r"\d+(?:\.\d+)?", en.text)
        zh_nums = re.findall(r"\d+(?:\.\d+)?", zh.text)
        missing = [n for n in original_nums if n not in zh_nums]
        if missing:
            issues.append({"type": "number_mismatch", "severity": "medium", "segment_id": en.id, "missing": missing})
        ascii_letters = sum(1 for ch in zh.text if ch.isascii() and ch.isalpha())
        if ascii_letters / max(1, len(zh.text)) > float(config.get("qa", {}).get("flag_untranslated_ascii_ratio", 0.5)):
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
    }


def collect_trace_flags(traces: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trace in traces:
        for flag in getattr(trace, "flags", []) or []:
            if str(flag).startswith("KEEP_") or "'type': 'KEEP'" in str(flag):
                continue
            counts[flag] = counts.get(flag, 0) + 1
    return counts


def has_usable_chinese(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped in {"...", "…", "N/A", "null", "None"}:
        return False
    if not re.search(r"[\u4e00-\u9fff]", stripped):
        return False
    if re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE) in {"对", "是", "好", "嗯", "行"}:
        return True
    non_punct = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    return len(non_punct) >= 2


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
