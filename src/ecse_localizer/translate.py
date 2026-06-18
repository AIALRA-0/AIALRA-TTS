from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from .glossary import GlossaryTerm
from .llm_local import LocalLLMClient
from .subtitle_io import Segment
from .text_protection import protect_text, restore_text
from .translation_quality import assess_translation_quality, protected_terms_missing, target_uses_compact_script, translation_target_language


@dataclass
class TranslationTrace:
    segment_id: int
    original_text: str
    zh_literal: str
    zh_lecture: str
    duration: float
    target_char_limit: int
    flags: list[str]
    paragraph_id: int | None = None
    paragraph_segment_ids: list[int] | None = None
    paragraph_text: str = ""


@dataclass
class TranslationParagraph:
    id: int
    segment_ids: list[int]
    start: float
    end: float
    text: str


FALLBACK_PHRASES = [
    (r"\bwelcome everybody\b", "欢迎大家"),
    (r"\bthis is the first class\b|\bthis is the 1st class\b", "这是第一节课"),
    (r"\bi will introduce\b", "我会介绍"),
    (r"\bagenda\b", "今天的安排"),
    (r"\bsyllabus\b", "教学大纲"),
    (r"\bquestions?\b", "问题"),
    (r"\bsemiconductor\b", "半导体"),
    (r"\bworkforce development\b", "人才培养"),
    (r"\bprofessor\b", "教授"),
    (r"\bdepartment\b", "系里"),
    (r"\bslides?\b", "课件"),
    (r"\bnext\b", "下一页"),
]

SAFE_SHORT_PHRASES = {
    "good to go": "可以开始了。",
    "okay": "好的。",
    "ok": "好的。",
    "thank you": "谢谢。",
    "thanks": "谢谢。",
    "right": "对吧？",
    "yes": "是的。",
    "yeah": "对。",
    "no": "不是。",
    "so": "所以，",
    "and": "而且，",
    "but": "不过，",
    "then": "然后，",
    "when": "当时，",
    "because": "因为，",
    "or": "或者，",
    "well": "那么，",
    "now": "现在，",
    "so that's": "大概就是这样。",
    "that's number one": "这是第一点。",
    "that is number one": "这是第一点。",
}

SHORT_DISCOURSE_SOURCE_TOKENS = {
    "a",
    "ah",
    "alright",
    "and",
    "anyway",
    "but",
    "fine",
    "good",
    "great",
    "hmm",
    "let's",
    "no",
    "now",
    "ok",
    "okay",
    "right",
    "so",
    "start",
    "sure",
    "thanks",
    "then",
    "uh",
    "um",
    "well",
    "yeah",
    "yep",
    "yes",
}

SINGLE_SEGMENT_RESCUE_PROMPT = """You are a local lecture subtitle translator for engineering lectures.
Return strict JSON only.

Task:
- Translate exactly one subtitle segment into the requested target language.
- If the source is an incomplete spoken fragment, translate it as a natural incomplete target-language classroom fragment. Do not complete it with invented information.
- Preserve numbers, formulas, variables, code, file paths, URLs, names, acronyms, and protected placeholders.
- Keep technical terms accurate and concise.
- Do not summarize, explain, evaluate, or say that the user should review the subtitle.
- The field zh_literal is faithful; zh_lecture is a natural target-language teaching subtitle with the same meaning.
"""


def translate_segments(
    segments: list[Segment],
    glossary: dict[str, GlossaryTerm],
    config: dict,
    trace_path: str | Path,
    logger: logging.Logger | None = None,
) -> tuple[list[Segment], list[TranslationTrace], str]:
    client = LocalLLMClient(config)
    status = client.status()
    if logger:
        logger.info("LLM status: %s", status)
    if status.available:
        if should_bypass_low_capacity_llm(status.model, len(segments), config):
            if logger:
                logger.warning(
                    "Bypassing low-capacity local LLM model %s for %d segments; using rule fallback.",
                    status.model,
                    len(segments),
                )
            zh_segments, traces = translate_with_rules(segments, glossary, config)
            for trace in traces:
                trace.flags.append("LOW_CAPACITY_LLM_BYPASSED_FOR_LONG_VIDEO")
            Path(trace_path).write_text(json.dumps([asdict(t) for t in traces], ensure_ascii=False, indent=2), encoding="utf-8")
            return zh_segments, traces, "rule_based_local_fallback_low_capacity_llm_bypass"
        try:
            zh_segments, traces = translate_with_llm(segments, glossary, config, trace_path, client, logger)
            backend = "local_llm_with_rule_fallback" if any("LLM_PARTIAL_OR_EMPTY_FALLBACK" in t.flags for t in traces) else "local_llm"
            return zh_segments, traces, backend
        except Exception as exc:
            if logger:
                logger.warning("Local LLM translation failed, using fallback: %s", exc)
    zh_segments, traces = translate_with_rules(segments, glossary, config)
    Path(trace_path).write_text(json.dumps([asdict(t) for t in traces], ensure_ascii=False, indent=2), encoding="utf-8")
    return zh_segments, traces, "rule_based_local_fallback"


def should_bypass_low_capacity_llm(model: str | None, segment_count: int, config: dict) -> bool:
    if not model:
        return False
    low_capacity_models = tuple(config.get("llm", {}).get("low_capacity_models", ["0.5b", "1b"]))
    threshold = int(config.get("llm", {}).get("low_capacity_bypass_segment_threshold", 120))
    return segment_count > threshold and any(marker.lower() in model.lower() for marker in low_capacity_models)


def translate_with_llm(
    segments: list[Segment],
    glossary: dict[str, GlossaryTerm],
    config: dict,
    trace_path: str | Path,
    client: LocalLLMClient,
    logger: logging.Logger | None = None,
) -> tuple[list[Segment], list[TranslationTrace]]:
    zh_segments: list[Segment] = []
    traces: list[TranslationTrace] = []
    prompt_dir = Path(config["project_root"]) / "prompts"
    literal_prompt = (prompt_dir / "translate_literal.md").read_text(encoding="utf-8")
    rewrite_prompt = (prompt_dir / "lecture_rewrite.md").read_text(encoding="utf-8")
    style_prompt = read_optional_prompt(prompt_dir / "style_guide.md")
    coherence_prompt = read_optional_prompt(prompt_dir / "coherence_pass.md")
    glossary_text = "\n".join(
        f"{t.source_term}\t{t.zh_term}\t{t.type}"
        for t in sorted(glossary.values(), key=lambda x: (-x.confidence, x.source_term.lower()))[:240]
    )
    style_guide = default_style_guide(config)
    if use_best_quality(config):
        style_guide = build_style_guide(segments, glossary_text, config, client, style_prompt, logger)
        style_path = Path(trace_path).with_name(Path(trace_path).stem.replace("_translation_trace", "") + "_style_guide.md")
        style_path.write_text(style_guide, encoding="utf-8")
    paragraphs = build_translation_paragraphs(segments, config) if use_best_quality(config) else []
    paragraph_by_segment = paragraph_lookup(paragraphs)
    chunk_size = int(config.get("llm", {}).get("translation_chunk_size", 8))
    for chunk_start in range(0, len(segments), max(1, chunk_size)):
        chunk = segments[chunk_start : chunk_start + max(1, chunk_size)]
        if logger:
            logger.info(
                "Translating segments %d-%d / %d",
                chunk[0].id,
                chunk[-1].id,
                len(segments),
            )
        chunk_results = translate_chunk_with_retries(
            segments,
            chunk_start,
            chunk,
            glossary,
            glossary_text,
            config,
            client,
            literal_prompt,
            rewrite_prompt,
            style_guide,
            coherence_prompt if use_best_quality(config) else "",
            paragraph_by_segment,
            logger,
        )
        for seg, lit, zh, flags, limit in chunk_results:
            paragraph = paragraph_by_segment.get(seg.id)
            zh_segments.append(Segment(seg.id, seg.start, seg.end, zh))
            traces.append(
                TranslationTrace(
                    seg.id,
                    seg.text,
                    lit,
                    zh,
                    seg.duration,
                    limit,
                    flags,
                    paragraph_id=paragraph.id if paragraph else None,
                    paragraph_segment_ids=list(paragraph.segment_ids) if paragraph else None,
                    paragraph_text=paragraph.text if paragraph else "",
                )
            )
        Path(trace_path).write_text(json.dumps([asdict(t) for t in traces], ensure_ascii=False, indent=2), encoding="utf-8")
    Path(trace_path).write_text(json.dumps([asdict(t) for t in traces], ensure_ascii=False, indent=2), encoding="utf-8")
    return zh_segments, traces


def translate_chunk_with_retries(
    all_segments: list[Segment],
    chunk_start: int,
    chunk: list[Segment],
    glossary: dict[str, GlossaryTerm],
    glossary_text: str,
    config: dict,
    client: LocalLLMClient,
    literal_prompt: str,
    rewrite_prompt: str,
    style_guide: str,
    coherence_prompt: str,
    paragraph_by_segment: dict[int, TranslationParagraph] | None = None,
    logger: logging.Logger | None = None,
) -> list[tuple[Segment, str, str, list[str], int]]:
    try:
        return request_llm_chunk(
            all_segments,
            chunk_start,
            chunk,
            glossary_text,
            config,
            client,
            literal_prompt,
            rewrite_prompt,
            style_guide,
            coherence_prompt,
            paragraph_by_segment or {},
            logger,
            glossary,
        )
    except Exception as exc:
        if logger:
            logger.warning("LLM chunk failed at segment %s (%d rows): %s", chunk[0].id if chunk else "?", len(chunk), exc)
        if len(chunk) > 1:
            results: list[tuple[Segment, str, str, list[str], int]] = []
            for offset, seg in enumerate(chunk):
                results.extend(
                    translate_chunk_with_retries(
                        all_segments,
                        chunk_start + offset,
                        [seg],
                        glossary,
                        glossary_text,
                        config,
                        client,
                        literal_prompt,
                        rewrite_prompt,
                        style_guide,
                        coherence_prompt,
                        paragraph_by_segment or {},
                        logger,
                    )
                )
            return results
        seg = chunk[0]
        safe_phrase = safe_short_phrase_translation(seg.text)
        if safe_phrase:
            limit = target_limit(seg, config)
            return [(seg, safe_phrase, safe_phrase, ["LOCAL_SAFE_PHRASE_TRANSLATION"], limit)]
        rescue = rescue_translate_single_segment(
            all_segments,
            chunk_start,
            seg,
            glossary_text,
            config,
            client,
            logger,
        )
        if rescue:
            return [rescue]
        literal, flags = fallback_translate_text(seg.text, glossary)
        limit = target_limit(seg, config)
        lecture = normalize_zh(literal)
        flags.extend(["LLM_CHUNK_FAILED_FALLBACK", "LOCAL_RULE_FALLBACK_REVIEW_REQUIRED"])
        flags.extend(assess_translation_quality(seg.text, lecture, literal, config))
        return [(seg, literal, lecture, flags, limit)]


def rescue_translate_single_segment(
    all_segments: list[Segment],
    absolute_index: int,
    seg: Segment,
    glossary_text: str,
    config: dict,
    client: LocalLLMClient,
    logger: logging.Logger | None = None,
) -> tuple[Segment, str, str, list[str], int] | None:
    protected = protect_text(seg.text)
    payload = {
        "glossary": glossary_text,
        "segment": {
            "id": seg.id,
            "text": protected.text,
            "duration": round(seg.duration, 3),
            "target_char_limit": target_limit(seg, config),
            "previous_original": all_segments[absolute_index - 1].text if absolute_index > 0 else "",
            "next_original": all_segments[absolute_index + 1].text if absolute_index + 1 < len(all_segments) else "",
        },
    }
    schema = '{"id":1,"zh_literal":"faithful target-language translation","zh_lecture":"natural target-language lecture subtitle","flags":[]}'
    for attempt in range(1, 4):
        try:
            data = client.json_chat(SINGLE_SEGMENT_RESCUE_PROMPT, json.dumps(payload, ensure_ascii=False), schema)
            lit = restore_and_repair_protected_terms(str(data.get("zh_literal", "")), protected.mapping, seg.text)
            zh = restore_and_repair_protected_terms(str(data.get("zh_lecture", "")), protected.mapping, seg.text)
            if (
                not is_usable_translation(lit, config)
                or is_forbidden_non_translation(lit)
                or short_source_translation_overexpanded(seg.text, lit, config)
            ):
                continue
            if (
                not is_usable_translation(zh, config)
                or is_forbidden_non_translation(zh)
                or short_source_translation_overexpanded(seg.text, zh, config)
            ):
                continue
            flags = sanitize_flags(data.get("flags", [])) + ["LOCAL_LLM_SINGLE_RESCUE"]
            missing = numbers_missing(seg.text, zh)
            if missing:
                flags.append("MISSING_NUMBER:" + ",".join(missing[:6]))
            limit = target_limit(seg, config)
            zh = normalize_translation(zh, config)
            if len(zh) > limit:
                flags.append("ZH_OVER_TARGET_LENGTH")
                if config.get("translation", {}).get("hard_truncate_over_limit", False):
                    zh = compress_to_limit(zh, limit, config)
            flags.extend(protected_term_flags(seg.text, zh))
            flags.extend(assess_translation_quality(seg.text, zh, lit, config))
            return (seg, normalize_translation(lit, config), zh, flags, limit)
        except Exception as exc:
            if logger:
                logger.warning("LLM single-segment rescue failed for segment %s attempt %d: %s", seg.id, attempt, exc)
    return None


def request_llm_chunk(
    all_segments: list[Segment],
    chunk_start: int,
    chunk: list[Segment],
    glossary_text: str,
    config: dict,
    client: LocalLLMClient,
    literal_prompt: str,
    rewrite_prompt: str,
    style_guide: str,
    coherence_prompt: str,
    paragraph_by_segment: dict[int, TranslationParagraph],
    logger: logging.Logger | None = None,
    glossary: dict[str, GlossaryTerm] | None = None,
) -> list[tuple[Segment, str, str, list[str], int]]:
    protected = []
    maps = []
    for i, seg in enumerate(chunk):
        result = protect_text(seg.text)
        absolute = chunk_start + i
        paragraph = paragraph_by_segment.get(seg.id)
        protected.append(
            {
                "id": seg.id,
                "text": result.text,
                "duration": round(seg.duration, 3),
                "target_char_limit": target_limit(seg, config),
                "previous_original": all_segments[absolute - 1].text if absolute > 0 else "",
                "next_original": all_segments[absolute + 1].text if absolute + 1 < len(all_segments) else "",
                "context_before": context_window(all_segments, absolute, -int(config.get("translation", {}).get("context_window_segments", 3))),
                "context_after": context_window(all_segments, absolute, int(config.get("translation", {}).get("context_window_segments", 3))),
                "paragraph_id": paragraph.id if paragraph else None,
                "paragraph_segment_ids": list(paragraph.segment_ids) if paragraph else [],
                "paragraph_text": protect_text(paragraph.text).text if paragraph else "",
            }
        )
        maps.append(result.mapping)
    literal_user = json.dumps({"glossary": glossary_text, "style_guide": style_guide, "segments": protected}, ensure_ascii=False)
    literal = client.json_chat(literal_prompt, literal_user, '{"segments":[{"id":1,"zh_literal":"忠实翻译原文","flags":[]}]}')
    literal_by_id = {int(x["id"]): x for x in literal.get("segments", [])}
    rewrite_payload = []
    for item in protected:
        row = literal_by_id.get(int(item["id"]), {})
        rewrite_payload.append({**item, "zh_literal": row.get("zh_literal", "")})
    rewrite_user = json.dumps(
        {
            "glossary": glossary_text,
            "style_guide": style_guide,
            "quality_requirements": quality_requirements(config),
            "segments": rewrite_payload,
        },
        ensure_ascii=False,
    )
    rewrite = client.json_chat(rewrite_prompt, rewrite_user, '{"segments":[{"id":1,"zh_lecture":"natural target-language spoken subtitle","flags":[]}]}')
    rewrite_by_id = {int(x["id"]): x for x in rewrite.get("segments", [])}
    if coherence_prompt:
        rewrite_by_id = run_coherence_pass(
            client,
            coherence_prompt,
            protected,
            literal_by_id,
            rewrite_by_id,
            glossary_text,
            style_guide,
            config,
            logger,
        )

    results: list[tuple[Segment, str, str, list[str], int]] = []
    low_capacity = any(marker in (client.model or "").lower() for marker in ("0.5b", "1b"))
    for idx, (seg, mapping) in enumerate(zip(chunk, maps)):
        flags: list[str] = []
        literal_row = literal_by_id.get(seg.id, {})
        rewrite_row = rewrite_by_id.get(seg.id, {})
        lit = restore_and_repair_protected_terms(str(literal_row.get("zh_literal", "")), mapping, seg.text)
        zh = restore_and_repair_protected_terms(str(rewrite_row.get("zh_lecture", "")), mapping, seg.text)
        neighbor_literal_zh = chunk_neighbor_literal_zh(chunk, literal_by_id, idx)
        flags.extend(sanitize_flags(literal_row.get("flags", [])) + sanitize_flags(rewrite_row.get("flags", [])))
        if low_capacity:
            flags.append("LOW_CAPACITY_LLM_REVIEW_REQUIRED")
        if (
            not is_usable_translation(lit, config)
            or is_forbidden_non_translation(lit)
            or short_source_translation_overexpanded(seg.text, lit, config)
        ):
            if logger:
                logger.warning("LLM row fallback for segment %s: unusable literal translation: %s", seg.id, lit[:80])
            results.append(
                fallback_or_rescue_segment(
                    all_segments,
                    chunk_start + idx,
                    seg,
                    glossary_text,
                    glossary or {},
                    config,
                    client,
                    logger,
                    "LLM_ROW_UNUSABLE_LITERAL_FALLBACK",
                )
            )
            continue
        if (
            not is_usable_translation(zh, config)
            or is_forbidden_non_translation(zh)
            or short_source_translation_overexpanded(seg.text, zh, config)
            or coherence_contains_neighbor_literal(zh, lit, neighbor_literal_zh)
        ):
            if logger:
                logger.warning("LLM row fallback for segment %s: unusable lecture translation: %s", seg.id, zh[:80])
            results.append(
                fallback_or_rescue_segment(
                    all_segments,
                    chunk_start + idx,
                    seg,
                    glossary_text,
                    glossary or {},
                    config,
                    client,
                    logger,
                    "LLM_ROW_UNUSABLE_LECTURE_FALLBACK",
                )
            )
            continue
        missing_numbers = numbers_missing(seg.text, zh)
        if missing_numbers:
            flags.append("MISSING_NUMBER:" + ",".join(missing_numbers[:6]))
        limit = target_limit(seg, config)
        zh = normalize_translation(zh, config)
        if len(zh) > limit:
            flags.append("ZH_OVER_TARGET_LENGTH")
            if config.get("translation", {}).get("hard_truncate_over_limit", False):
                zh = compress_to_limit(zh, limit, config)
        flags.extend(protected_term_flags(seg.text, zh))
        flags.extend(assess_translation_quality(seg.text, zh, lit, config))
        results.append((seg, normalize_translation(lit, config), zh, flags, limit))
    return results


def chunk_neighbor_literal_zh(
    chunk: list[Segment],
    literal_by_id: dict[int, dict[str, Any]],
    idx: int,
) -> list[str]:
    values: list[str] = []
    for neighbor_idx in (idx - 1, idx + 1):
        if 0 <= neighbor_idx < len(chunk):
            values.append(str(literal_by_id.get(chunk[neighbor_idx].id, {}).get("zh_literal", "")))
    return values


def fallback_or_rescue_segment(
    all_segments: list[Segment],
    absolute_index: int,
    seg: Segment,
    glossary_text: str,
    glossary: dict[str, GlossaryTerm],
    config: dict,
    client: LocalLLMClient,
    logger: logging.Logger | None,
    reason_flag: str,
) -> tuple[Segment, str, str, list[str], int]:
    limit = target_limit(seg, config)
    safe_phrase = safe_short_phrase_translation(seg.text)
    if safe_phrase:
        return (seg, safe_phrase, safe_phrase, ["LOCAL_SAFE_PHRASE_TRANSLATION", reason_flag], limit)
    rescue = rescue_translate_single_segment(all_segments, absolute_index, seg, glossary_text, config, client, logger)
    if rescue:
        rescue_seg, lit, zh, flags, rescue_limit = rescue
        return (rescue_seg, lit, zh, flags + [reason_flag, "LOCAL_LLM_ROW_RESCUE"], rescue_limit)
    literal, flags = fallback_translate_text(seg.text, glossary)
    lecture = normalize_translation(normalize_zh(literal), config)
    flags.extend([reason_flag, "LLM_ROW_FAILED_FALLBACK", "LOCAL_RULE_FALLBACK_REVIEW_REQUIRED"])
    flags.extend(protected_term_flags(seg.text, lecture))
    flags.extend(assess_translation_quality(seg.text, lecture, literal, config))
    return (seg, normalize_translation(literal, config), lecture, flags, limit)


def short_source_translation_overexpanded(source_text: str, candidate_zh: str, config: dict) -> bool:
    if not target_uses_compact_script(config):
        return False
    units = re.findall(r"[A-Za-z0-9%.-]+", source_text or "")
    if not units or len(units) > 5:
        return False
    candidate_len = target_meaningful_len(candidate_zh)
    allowed = max(14, len(units) * 6)
    return candidate_len > allowed


def read_optional_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def use_best_quality(config: dict) -> bool:
    mode = str(config.get("translation", {}).get("quality_mode", "best_quality")).lower()
    return mode in {"best", "best_quality", "quality", "high"}


def quality_requirements(config: dict) -> list[str]:
    target_language = translation_target_language(config) or "zh-CN"
    fluency_rule = (
        "Prefer fluent Chinese lecture wording over English word order."
        if target_uses_chinese(config)
        else "Prefer fluent target-language lecture wording over English word order."
    )
    return [
        f"Target language: {target_language}",
        "Do not omit technical information, numbers, names, formulas, variables, URLs, file paths, or protected placeholders.",
        fluency_rule,
        "Make adjacent subtitle fragments read as one coherent explanation.",
        "Keep the subtitle concise enough for TTS, but never compress away key facts.",
    ]


def default_style_guide(config: dict) -> str:
    style = config.get("translation", {}).get("style", "natural_chinese_lecture")
    target_language = translation_target_language(config) or "zh-CN"
    if not target_uses_chinese(config):
        return (
            f"Style: {style}\n"
            f"- Write in the requested target language ({target_language}) as clear lecture narration, not word-for-word English.\n"
            "- Use the course glossary consistently; keep technical acronyms in their standard form when appropriate.\n"
            "- Preserve every number, unit, formula, variable, code token, file path, URL, person name, and paper title.\n"
            "- Make adjacent subtitle fragments read coherently with natural transitions in the target language.\n"
            "- Do not add conclusions, evaluations, or explanations that are not present in the source."
        )
    return (
        f"Style: {style}\n"
        "- 中文表达要像清晰的授课口播，不要像逐词翻译。\n"
        "- 术语使用课程术语表；专业名词宁可保留英文缩写，也不要乱译。\n"
        "- 保留全部数字、单位、公式、变量、代码、路径、URL、人名、论文名。\n"
        "- 句子之间要有承接关系，可使用“这里”“也就是说”“接下来”“注意”等自然衔接词。\n"
        "- 不要添加原文没有的结论、评价或解释。"
    )


def build_style_guide(
    segments: list[Segment],
    glossary_text: str,
    config: dict,
    client: LocalLLMClient,
    prompt: str,
    logger: logging.Logger | None = None,
) -> str:
    if not prompt:
        return default_style_guide(config)
    sample_count = int(config.get("translation", {}).get("style_sample_segments", 80))
    samples = [
        {"id": seg.id, "text": seg.text, "duration": round(seg.duration, 3)}
        for seg in segments[: max(1, sample_count)]
    ]
    payload = {
        "target_language": config.get("translation", {}).get("target_language", "zh-CN"),
        "style": config.get("translation", {}).get("style", "natural_chinese_lecture"),
        "glossary": glossary_text,
        "samples": samples,
    }
    schema = '{"style_guide":"target-language lecture style guide","tone_rules":["rule"],"term_notes":["note"],"risk_notes":["note"]}'
    try:
        data = client.json_chat(prompt, json.dumps(payload, ensure_ascii=False), schema)
        pieces = [str(data.get("style_guide", "")).strip()]
        for key, title in [("tone_rules", "Tone rules"), ("term_notes", "Term notes"), ("risk_notes", "Risk notes")]:
            values = data.get(key, [])
            if isinstance(values, list) and values:
                pieces.append(title + ":\n" + "\n".join(f"- {str(v).strip()}" for v in values if str(v).strip()))
        guide = "\n\n".join(p for p in pieces if p)
        return guide or default_style_guide(config)
    except Exception as exc:
        if logger:
            logger.warning("Style guide generation failed; using default guide: %s", exc)
        return default_style_guide(config)


def build_translation_paragraphs(segments: list[Segment], config: dict) -> list[TranslationParagraph]:
    """Reconstruct spoken discourse blocks while keeping original subtitle ids.

    The blocks are context for translation quality only; output subtitles still
    use the original segment timestamps and ids.
    """
    if not segments:
        return []
    translation_cfg = config.get("translation", {})
    max_gap = float(translation_cfg.get("paragraph_max_gap_seconds", 1.2))
    max_chars = int(translation_cfg.get("paragraph_max_source_chars", 900))
    max_duration = float(translation_cfg.get("paragraph_max_duration_seconds", 45.0))
    min_segments_before_sentence_break = int(translation_cfg.get("paragraph_min_segments_before_sentence_break", 2))

    paragraphs: list[TranslationParagraph] = []
    current: list[Segment] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        paragraphs.append(
            TranslationParagraph(
                id=len(paragraphs) + 1,
                segment_ids=[seg.id for seg in current],
                start=current[0].start,
                end=current[-1].end,
                text=join_paragraph_text(current),
            )
        )
        current = []
        current_chars = 0

    previous: Segment | None = None
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        gap = seg.start - previous.end if previous else 0.0
        projected_chars = current_chars + len(text) + (1 if current else 0)
        projected_duration = seg.end - current[0].start if current else seg.duration
        if current and (gap > max_gap or projected_chars > max_chars or projected_duration > max_duration):
            flush()
        current.append(seg)
        current_chars += len(text) + (1 if len(current) > 1 else 0)
        if len(current) >= min_segments_before_sentence_break and ends_discourse_sentence(text):
            flush()
        previous = seg
    flush()
    return paragraphs


def paragraph_lookup(paragraphs: list[TranslationParagraph]) -> dict[int, TranslationParagraph]:
    lookup: dict[int, TranslationParagraph] = {}
    for paragraph in paragraphs:
        for segment_id in paragraph.segment_ids:
            lookup[segment_id] = paragraph
    return lookup


def join_paragraph_text(segments: list[Segment]) -> str:
    text = " ".join((seg.text or "").strip() for seg in segments if (seg.text or "").strip())
    return re.sub(r"\s+", " ", text).strip()


def ends_discourse_sentence(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return bool(re.search(r'[.!?。？！]["\')\]}”’]*$', cleaned))


def context_window(all_segments: list[Segment], absolute_index: int, count: int) -> list[dict[str, object]]:
    if count == 0:
        return []
    if count < 0:
        start = max(0, absolute_index + count)
        rows = all_segments[start:absolute_index]
    else:
        rows = all_segments[absolute_index + 1 : absolute_index + 1 + count]
    return [{"id": seg.id, "text": seg.text} for seg in rows]


def run_coherence_pass(
    client: LocalLLMClient,
    prompt: str,
    protected: list[dict[str, Any]],
    literal_by_id: dict[int, dict[str, Any]],
    rewrite_by_id: dict[int, dict[str, Any]],
    glossary_text: str,
    style_guide: str,
    config: dict,
    logger: logging.Logger | None = None,
) -> dict[int, dict[str, Any]]:
    payload = {
        "glossary": glossary_text,
        "style_guide": style_guide,
        "quality_requirements": quality_requirements(config),
        "segments": [
            {
                **item,
                "zh_literal": literal_by_id.get(int(item["id"]), {}).get("zh_literal", ""),
                "current_zh": rewrite_by_id.get(int(item["id"]), {}).get("zh_lecture", ""),
            }
            for item in protected
        ],
    }
    schema = '{"segments":[{"id":1,"zh_lecture":"more coherent and natural target-language lecture subtitle","flags":["COHERENCE_REWRITE"],"notes":""}]}'
    try:
        data = client.json_chat(prompt, json.dumps(payload, ensure_ascii=False), schema)
        rows = data.get("segments", [])
        by_id = {int(row.get("id")): row for row in rows if str(row.get("id", "")).isdigit()}
        protected_ids = [int(item["id"]) for item in protected if str(item.get("id", "")).isdigit()]
        protected_index = {sid: idx for idx, sid in enumerate(protected_ids)}
        for sid, row in by_id.items():
            source_item = next((item for item in protected if int(item.get("id", -1)) == sid), {})
            zh = str(row.get("zh_lecture", "")).strip()
            if not is_usable_translation(zh, config) or is_forbidden_non_translation(zh):
                continue
            current = rewrite_by_id.get(sid, {})
            idx = protected_index.get(sid)
            neighbor_literal_zh: list[str] = []
            if idx is not None:
                for neighbor_idx in (idx - 1, idx + 1):
                    if 0 <= neighbor_idx < len(protected_ids):
                        neighbor_literal_zh.append(str(literal_by_id.get(protected_ids[neighbor_idx], {}).get("zh_literal", "")))
            rejection_flags = coherence_rejection_flags(
                str(source_item.get("text", "")),
                str(current.get("zh_lecture", "")),
                zh,
                config,
                literal_zh=str(literal_by_id.get(sid, {}).get("zh_literal", "")),
                neighbor_literal_zh=neighbor_literal_zh,
            )
            if rejection_flags:
                kept = dict(current)
                kept["id"] = sid
                kept["flags"] = sanitize_flags(kept.get("flags", [])) + rejection_flags
                rewrite_by_id[sid] = kept
                continue
            merged = dict(rewrite_by_id.get(sid, {}))
            merged["id"] = sid
            merged["zh_lecture"] = zh
            merged["flags"] = sanitize_flags(merged.get("flags", [])) + sanitize_flags(row.get("flags", [])) + ["COHERENCE_PASS"]
            rewrite_by_id[sid] = merged
        return rewrite_by_id
    except Exception as exc:
        if logger:
            logger.warning("Coherence pass failed; keeping first rewrite: %s", exc)
        return rewrite_by_id


def coherence_rejection_flags(
    source_text: str,
    previous_zh: str,
    candidate_zh: str,
    config: dict,
    *,
    literal_zh: str = "",
    neighbor_literal_zh: list[str] | None = None,
) -> list[str]:
    flags: list[str] = []
    source_placeholders = placeholder_tokens(source_text)
    if source_placeholders:
        previous_placeholders = placeholder_tokens(previous_zh)
        candidate_placeholders = placeholder_tokens(candidate_zh)
        dropped = sorted(source_placeholders - candidate_placeholders)
        if dropped and source_placeholders.issubset(previous_placeholders):
            flags.append("COHERENCE_REJECTED_FIDELITY_GUARD")
            flags.append("COHERENCE_DROPPED_PROTECTED_PLACEHOLDER:" + ",".join(dropped[:6]))
    previous_quality = set(assess_translation_quality(source_text, previous_zh, previous_zh, config))
    candidate_quality = set(assess_translation_quality(source_text, candidate_zh, previous_zh, config))
    high_candidate = candidate_quality & {"SUMMARY_STYLE_TRANSLATION", "REVIEW_COMMENTARY_LEAK"}
    if high_candidate and not (previous_quality & high_candidate):
        flags.append("COHERENCE_REJECTED_QUALITY_GUARD")
        flags.extend(sorted("COHERENCE_INTRODUCED_" + flag for flag in high_candidate))
    if coherence_short_source_overexpanded(source_text, previous_zh, candidate_zh, config):
        flags.append("COHERENCE_REJECTED_SHORT_SOURCE_GUARD")
        flags.append("COHERENCE_SHORT_SOURCE_OVEREXPANDED")
    if coherence_contains_neighbor_literal(candidate_zh, literal_zh, neighbor_literal_zh or []):
        flags.append("COHERENCE_REJECTED_NEIGHBOR_LEAK")
        flags.append("COHERENCE_INCLUDED_NEIGHBOR_LITERAL")
    return flags


def placeholder_tokens(text: str) -> set[str]:
    return set(re.findall(r"<KEEP_\d{3}>", text or ""))


def coherence_short_source_overexpanded(source_text: str, previous_zh: str, candidate_zh: str, config: dict) -> bool:
    if not target_uses_compact_script(config):
        return False
    if not is_short_discourse_source(source_text):
        return False
    previous_len = target_meaningful_len(previous_zh)
    candidate_len = target_meaningful_len(candidate_zh)
    if previous_len == 0 or candidate_len == 0:
        return False
    allowed = max(10, previous_len + 8, int(previous_len * 2.5))
    return candidate_len > allowed


def is_short_discourse_source(source_text: str) -> bool:
    tokens = [token.lower() for token in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", source_text or "")]
    if not tokens or len(tokens) > 4:
        return False
    return all(token in SHORT_DISCOURSE_SOURCE_TOKENS for token in tokens)


def target_meaningful_len(text: str) -> int:
    return len(re.sub(r"[\s\u3000，。！？、；：,.!?;:'\"“”‘’()（）\[\]【】]+", "", text or ""))


def coherence_contains_neighbor_literal(candidate_zh: str, literal_zh: str, neighbor_literal_zh: list[str]) -> bool:
    candidate_norm = normalize_coherence_text(candidate_zh)
    literal_norm = normalize_coherence_text(literal_zh)
    if not candidate_norm:
        return False
    if literal_norm and candidate_norm == literal_norm:
        return False
    for neighbor in neighbor_literal_zh:
        neighbor_norm = normalize_coherence_text(neighbor)
        if len(neighbor_norm) >= 6 and neighbor_norm in candidate_norm:
            return True
    return False


def normalize_coherence_text(text: str) -> str:
    return re.sub(r"[\s\u3000，。！？、；：,.!?;:'\"“”‘’()（）\[\]【】]+", "", text or "")


def restore_and_repair_protected_terms(text: str, mapping: dict[str, str], source: str) -> str:
    restored = restore_text(text, mapping)
    protected_acronyms = [value for value in mapping.values() if is_acronym(value)]
    original_acronyms = set(extract_acronyms(source))
    for wanted in protected_acronyms:
        if wanted in restored:
            continue
        extras = [
            match
            for match in re.finditer(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Za-z0-9])", restored)
            if match.group(0) not in original_acronyms
        ]
        if extras:
            match = extras[0]
            restored = restored[: match.start()] + wanted + restored[match.end() :]
        else:
            restored = append_before_sentence_punctuation(restored, f"（{wanted}）")
    for wanted in [value for value in mapping.values() if is_numeric_token(value)]:
        compact_wanted = re.sub(r"\s+", "", wanted)
        compact_restored = re.sub(r"\s+", "", restored)
        if compact_wanted not in compact_restored:
            restored = append_before_sentence_punctuation(restored, f"（{wanted}）")
    return restored


def sanitize_flags(flags: object) -> list[str]:
    if not isinstance(flags, list):
        return []
    cleaned: list[str] = []
    for flag in flags:
        text = str(flag).strip()
        if not text or text.startswith("<KEEP_"):
            continue
        cleaned.append(text)
    return cleaned


def protected_term_flags(source: str, zh: str) -> list[str]:
    missing = protected_terms_missing(source, zh)
    if not missing:
        return []
    return ["MISSING_PROTECTED_TERM:" + ",".join(missing[:6])]


def extract_acronyms(text: str) -> list[str]:
    return re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Za-z0-9])", text or "")


def is_acronym(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*", text or ""))


def is_numeric_token(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\d+(?:,\d{3})*(?:\.\d+)?(?:st|nd|rd|th)?(?:\s?(?:V|kHz|MHz|GHz|Hz|nm|um|µm|mm|cm|mA|A|W|eV|%))?|\d+\^-?\d+",
            text or "",
        )
    )


def append_before_sentence_punctuation(text: str, suffix: str) -> str:
    if not text:
        return suffix
    if text[-1] in "。！？!?":
        return text[:-1] + suffix + text[-1]
    return text + suffix


def translate_with_rules(
    segments: list[Segment],
    glossary: dict[str, GlossaryTerm],
    config: dict,
) -> tuple[list[Segment], list[TranslationTrace]]:
    zh_segments: list[Segment] = []
    traces: list[TranslationTrace] = []
    for seg in segments:
        literal, flags = fallback_translate_text(seg.text, glossary)
        lecture = lecture_rewrite(literal, seg.text)
        if config.get("dialect", {}).get("enabled"):
            lecture = apply_light_dialect(lecture, config.get("dialect", {}).get("target", "mandarin"))
        limit = target_limit(seg, config)
        lecture = compress_to_limit(lecture, limit, config)
        flags.extend(protected_term_flags(seg.text, lecture))
        flags.extend(assess_translation_quality(seg.text, lecture, literal, config))
        zh_segments.append(Segment(seg.id, seg.start, seg.end, lecture))
        traces.append(
            TranslationTrace(
                seg.id,
                seg.text,
                literal,
                lecture,
                seg.duration,
                limit,
                flags + ["LOCAL_RULE_FALLBACK_REVIEW_REQUIRED"],
            )
        )
    return zh_segments, traces


def fallback_translate_text(text: str, glossary: dict[str, GlossaryTerm]) -> tuple[str, list[str]]:
    protected = protect_text(text)
    work = protected.text
    flags = ["LLM_UNAVAILABLE"]
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    for term in sorted(glossary.values(), key=lambda t: len(t.source_term), reverse=True):
        if len(term.source_term) < 3:
            continue
        if term.type == "acronym" and term.zh_term == term.source_term and term.confidence < 0.9:
            continue
        work = re.sub(re.escape(term.source_term), term.zh_term, work, flags=re.IGNORECASE)
    for pattern, zh in FALLBACK_PHRASES:
        work = re.sub(pattern, zh, work, flags=re.IGNORECASE)

    ascii_ratio = sum(1 for ch in work if ord(ch) < 128 and ch.isalpha()) / max(1, len(work))
    zh = rule_literal_cleanup(work)
    if numbers and not all(n in zh for n in numbers):
        zh = zh.rstrip("。") + "，其中提到 " + "、".join(numbers[:4]) + "。"
    if ascii_ratio > 0.35:
        flags.append("HIGH_ASCII_RATIO_RULE_TRANSLATION")
    return restore_text(zh, protected.mapping), flags


def rule_literal_cleanup(text: str) -> str:
    work = text.strip()
    work = re.sub(r"\bover\s+(\d+)\s+years?\b", r"超过\1年", work, flags=re.IGNORECASE)
    work = re.sub(r"\b(\d+)\s+years?\b", r"\1年", work, flags=re.IGNORECASE)
    replacements = [
        (r"\bso\b", "所以"),
        (r"\bokay\b|\bok\b", "好的"),
        (r"\byeah\b|\byes\b", "对"),
        (r"\bno\b", "不是"),
        (r"\band\b", "和"),
        (r"\bor\b", "或者"),
        (r"\bbut\b", "但是"),
        (r"\bbecause\b", "因为"),
        (r"\bthank you\b|\bthanks\b", "谢谢"),
        (r"\babout\b", "大约"),
        (r"\broughly\b", "大约"),
        (r"\bcurrent\b", "当前"),
        (r"\bglobal\b", "全球"),
        (r"\bindustry\b", "产业"),
        (r"\bsales\b", "销售额"),
        (r"\bchip\b", "芯片"),
        (r"\bchips\b", "芯片"),
        (r"\bwafer\b", "晶圆"),
        (r"\bnode\b", "节点"),
        (r"\bnodes\b", "节点"),
        (r"\blogic\b", "逻辑"),
        (r"\bmemory\b", "存储"),
        (r"\bresearch\b", "研究"),
        (r"\bdevelopment\b", "开发"),
    ]
    for pattern, repl in replacements:
        work = re.sub(pattern, repl, work, flags=re.IGNORECASE)
    work = re.sub(r"\s+", " ", work).strip()
    work = work.replace(" ,", "，").replace(" .", "。").replace(",", "，").replace(".", "。")
    if not re.search(r"[\u4e00-\u9fff]", work):
        work = "【待人工复核】" + work
    if work and work[-1] not in "。！？!?":
        work += "。"
    return work


def safe_short_phrase_translation(text: str) -> str | None:
    normalized = re.sub(r"[\s.?!,;:，。？！；：]+", " ", text or "").strip().lower()
    if not normalized:
        return None
    return SAFE_SHORT_PHRASES.get(normalized)


def is_usable_zh(text: str) -> bool:
    return is_usable_translation(text, {"translation": {"target_language": "zh-CN"}})


VALID_SHORT_TRANSLATIONS = {"对", "是", "好", "嗯", "行", "不", "否"}
SCRIPTLESS_UNIT_TOKENS = {
    "v",
    "mv",
    "kv",
    "hz",
    "khz",
    "mhz",
    "ghz",
    "a",
    "ma",
    "w",
    "kw",
    "nm",
    "um",
    "mm",
    "cm",
    "m",
    "s",
    "ms",
    "us",
    "ns",
    "db",
}


def is_usable_translation(text: str, config: dict, source_text: str = "") -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped in {"...", "…", "N/A", "null", "None"}:
        return False
    non_punct = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    if non_punct in VALID_SHORT_TRANSLATIONS:
        return True
    if source_allows_scriptless_translation(source_text) and re.search(r"\d", stripped):
        return len(non_punct) >= 1
    script_pattern = target_script_pattern(config)
    if script_pattern and not re.search(script_pattern, stripped):
        return False
    return len(non_punct) >= 2


def source_allows_scriptless_translation(source_text: str) -> bool:
    source = source_text or ""
    if not re.search(r"\d", source):
        return False
    alpha_tokens = re.findall(r"[A-Za-z]+", source)
    return all(token.lower() in SCRIPTLESS_UNIT_TOKENS for token in alpha_tokens)


def target_script_pattern(config: dict) -> str:
    target = translation_target_language(config)
    if target_uses_chinese(config):
        return r"[\u4e00-\u9fff]"
    if target.startswith(("ja", "jp")) or "japanese" in target:
        return r"[\u3040-\u30ff\u3400-\u9fff]"
    if target.startswith("ko") or "korean" in target:
        return r"[\uac00-\ud7af]"
    return ""


def target_uses_chinese(config: dict) -> bool:
    target = translation_target_language(config)
    return target.startswith(("zh", "yue", "cmn", "wuu")) or any(
        marker in target for marker in ["chinese", "cantonese", "mandarin"]
    )


def is_forbidden_non_translation(text: str) -> bool:
    stripped = normalize_zh(text)
    forbidden = [
        "这一段是在",
        "这一段主要围绕",
        "请结合英文字幕复核",
        "本段主要",
        "该片段主要",
    ]
    return any(token in stripped for token in forbidden)


def numbers_missing(source: str, translated: str) -> list[str]:
    original_nums = re.findall(r"\d+(?:\.\d+)?", source)
    translated_nums = re.findall(r"\d+(?:\.\d+)?", translated)
    return [n for n in original_nums if n not in translated_nums]


def normalize_zh(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    text = text.replace("，。", "。").replace("。。", "。")
    return text


def normalize_translation(text: str, config: dict) -> str:
    if target_uses_compact_script(config):
        return normalize_zh(text)
    work = re.sub(r"\s+", " ", text or "").strip()
    work = re.sub(r"\s+([,.;:!?])", r"\1", work)
    return work


def lecture_rewrite(literal: str, original: str) -> str:
    if literal.endswith("。"):
        return literal
    return literal + "。"


def target_limit(seg: Segment, config: dict) -> int:
    cps = float(config.get("translation", {}).get("max_zh_chars_per_second", 5.5))
    line = int(config.get("translation", {}).get("max_zh_chars_per_subtitle_line", 22))
    return max(8, min(line * 2, int(seg.duration * cps)))


def compress_to_limit(text: str, limit: int, config: dict | None = None) -> str:
    config = config or {"translation": {"target_language": "zh-CN"}}
    text = normalize_translation(text, config)
    if len(text) <= limit:
        return text
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    # Preserve final punctuation while keeping speech compact.
    compact_target = target_uses_compact_script(config)
    separator = "，" if compact_target else ", "
    period = "。" if compact_target else "."
    shortened = text[: max(1, limit - 1)].rstrip("，、；：,;: ")
    missing = [n for n in nums if n not in shortened]
    if missing:
        suffix = separator + ("、" if compact_target else ", ").join(missing[:4]) + period
        shortened = shortened[: max(1, limit - len(suffix))].rstrip("，、；：。,;: ") + suffix
    else:
        shortened += period
    return shortened


def apply_light_dialect(text: str, target: str) -> str:
    if target == "sichuan":
        return text.replace("这里", "这儿").replace("一下", "一哈")
    if target == "dongbei":
        return text.replace("这里", "这块")
    if target == "taiwan":
        return text.replace("视频", "影片")
    return text
