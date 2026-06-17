from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ffmpeg_utils import media_duration
from .glossary import GlossaryTerm
from .llm_local import LocalLLMClient
from .mux import hardsub_video, mux_video
from .qa import looks_like_non_translation_narration, run_qa
from .report import write_video_report
from .subtitle_io import (
    Segment,
    bilingual_segments,
    to_dicts,
    write_bilingual_ass,
    write_srt,
    write_vtt,
)
from .translate import TranslationTrace, is_usable_translation, numbers_missing, target_limit
from .translation_quality import translation_target_language
from .tts import build_aligned_dub
from .utils import PROJECT_ROOT, ensure_dir, now_id, slugify, write_json


@dataclass
class RepairResult:
    segment_id: int
    old_text: str
    new_text: str
    score: int
    notes: str
    flags: list[str]


def repair_from_fidelity(
    report_json: str | Path,
    fidelity_json: str | Path | None,
    config: dict[str, Any],
    *,
    max_score: int = 3,
    include_high: bool = True,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    report_path = Path(report_json)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if fidelity_json is None:
        fidelity_path = report_path.with_name(report_path.stem.replace("_report", "") + "_fidelity_report.json")
    else:
        fidelity_path = Path(fidelity_json)
    fidelity = json.loads(fidelity_path.read_text(encoding="utf-8"))

    en_segments = [Segment(int(x["id"]), float(x["start"]), float(x["end"]), str(x["text"])) for x in report["segments"]["en"]]
    zh_segments = [Segment(int(x["id"]), float(x["start"]), float(x["end"]), str(x["text"])) for x in report["segments"]["zh"]]
    if len(en_segments) != len(zh_segments):
        raise RuntimeError(f"Segment count mismatch: en={len(en_segments)} zh={len(zh_segments)}")

    repair_ids = select_repair_ids(fidelity, max_score=max_score, include_high=include_high)
    if logger:
        logger.info("Repairing %d segments from fidelity report %s", len(repair_ids), fidelity_path)

    client = LocalLLMClient(config)
    status = client.status()
    if not status.available:
        raise RuntimeError(status.message)
    if status.model and "14b" not in status.model.lower():
        raise RuntimeError(f"Repair requires configured 14B model; active model is {status.model}")

    prompt = (PROJECT_ROOT / "prompts" / "fidelity_repair.md").read_text(encoding="utf-8")
    glossary = load_glossary(Path(config["output_dir"]) / "glossary.json")
    glossary_text = glossary_prompt_text(glossary)
    review_by_id = {int(row["id"]): row for row in fidelity.get("reviews", []) if str(row.get("id", "")).isdigit()}

    repair_trace_path = Path(config["work_dir"]) / now_id("fidelity_repair_trace") / "repair_trace.json"
    ensure_dir(repair_trace_path.parent)
    repairs: list[RepairResult] = []
    by_id = {seg.id: seg for seg in zh_segments}
    chunk_size = int(config.get("llm", {}).get("repair_chunk_size", 6))
    ids = sorted(repair_ids)
    for start in range(0, len(ids), max(1, chunk_size)):
        chunk_ids = ids[start : start + max(1, chunk_size)]
        if logger:
            logger.info("Repairing segment ids %s", chunk_ids)
        repaired = repair_chunk_with_fallback(
            client,
            prompt,
            en_segments,
            zh_segments,
            chunk_ids,
            review_by_id,
            glossary_text,
            config,
            logger,
        )
        for item in repaired:
            seg = by_id.get(item.segment_id)
            if seg and item.new_text.strip():
                seg.text = item.new_text.strip()
            repairs.append(item)
        write_json(repair_trace_path, {"repairs": [r.__dict__ for r in repairs]})

    return write_repaired_outputs(report, report_path, en_segments, zh_segments, repairs, glossary, config, logger)


def select_repair_ids(fidelity: dict[str, Any], *, max_score: int, include_high: bool) -> set[int]:
    ids: set[int] = set()
    for row in fidelity.get("reviews", []):
        if not str(row.get("id", "")).isdigit():
            continue
        sid = int(row["id"])
        score = int(row.get("score", 5) or 5)
        if score <= max_score or row.get("summary_like") or not row.get("faithful", True):
            ids.add(sid)
    if include_high:
        for issue in fidelity.get("issues", []):
            if issue.get("severity") == "high" and str(issue.get("segment_id", "")).isdigit():
                ids.add(int(issue["segment_id"]))
    for issue in fidelity.get("issues", []):
        if issue.get("type") in {
            "number_mismatch",
            "acronym_or_name_mismatch",
            "possibly_overcompressed_translation",
            "translation_quality_heuristic",
        }:
            if str(issue.get("segment_id", "")).isdigit():
                ids.add(int(issue["segment_id"]))
    return ids


def repair_chunk_with_fallback(
    client: LocalLLMClient,
    prompt: str,
    en_segments: list[Segment],
    zh_segments: list[Segment],
    chunk_ids: list[int],
    review_by_id: dict[int, dict[str, Any]],
    glossary_text: str,
    config: dict[str, Any],
    logger: logging.Logger | None,
) -> list[RepairResult]:
    try:
        return repair_chunk(client, prompt, en_segments, zh_segments, chunk_ids, review_by_id, glossary_text, config)
    except Exception as exc:
        if logger:
            logger.warning("Repair chunk failed for %s: %s", chunk_ids, exc)
    results: list[RepairResult] = []
    for sid in chunk_ids:
        try:
            results.extend(repair_chunk(client, prompt, en_segments, zh_segments, [sid], review_by_id, glossary_text, config))
        except Exception as exc:
            if logger:
                logger.warning("Single repair failed for segment %s: %s", sid, exc)
            old = zh_segments[sid - 1].text
            results.append(RepairResult(sid, old, old, int(review_by_id.get(sid, {}).get("score", 1) or 1), str(exc)[:300], ["FIDELITY_REPAIR_FAILED"]))
    return results


def repair_chunk(
    client: LocalLLMClient,
    prompt: str,
    en_segments: list[Segment],
    zh_segments: list[Segment],
    chunk_ids: list[int],
    review_by_id: dict[int, dict[str, Any]],
    glossary_text: str,
    config: dict[str, Any],
) -> list[RepairResult]:
    payload = {
        "glossary": glossary_text,
        "target_language": translation_target_language(config),
        "segments": [
            {
                "id": sid,
                "previous_source": en_segments[sid - 2].text if sid > 1 else "",
                "source": en_segments[sid - 1].text,
                "current_translation": zh_segments[sid - 1].text,
                "next_source": en_segments[sid].text if sid < len(en_segments) else "",
                "previous_english": en_segments[sid - 2].text if sid > 1 else "",
                "english": en_segments[sid - 1].text,
                "current_chinese": zh_segments[sid - 1].text,
                "next_english": en_segments[sid].text if sid < len(en_segments) else "",
                "duration": round(en_segments[sid - 1].duration, 3),
                "target_char_limit": target_limit(en_segments[sid - 1], config),
                "review": review_by_id.get(sid, {}),
            }
            for sid in chunk_ids
        ],
    }
    schema = '{"segments":[{"id":1,"zh":"repaired target-language subtitle","flags":["FIDELITY_REPAIRED"],"notes":"why"}]}'
    data = client.json_chat(prompt, json.dumps(payload, ensure_ascii=False), schema)
    rows = data.get("segments", [])
    by_id = {int(row.get("id")): row for row in rows if str(row.get("id", "")).isdigit()}
    missing = [sid for sid in chunk_ids if sid not in by_id]
    if missing:
        raise RuntimeError(f"missing repaired ids: {missing}")

    results: list[RepairResult] = []
    for sid in chunk_ids:
        en = en_segments[sid - 1]
        old = zh_segments[sid - 1].text
        row = by_id[sid]
        new = clean_repair_text(str(row.get("zh", "")))
        if not is_usable_translation(new, config) or looks_like_non_translation_narration(new):
            raise RuntimeError(f"unusable repair for segment {sid}: {new!r}")
        flags = sanitize_flags(row.get("flags", []))
        flags.append("FIDELITY_REPAIRED")
        missing_numbers = numbers_missing(en.text, new)
        if missing_numbers:
            flags.append("REPAIR_MISSING_NUMBER:" + ",".join(missing_numbers[:6]))
        results.append(
            RepairResult(
                segment_id=sid,
                old_text=old,
                new_text=new,
                score=int(review_by_id.get(sid, {}).get("score", 0) or 0),
                notes=str(row.get("notes", ""))[:300],
                flags=sorted(set(flags)),
            )
        )
    return results


def clean_repair_text(text: str) -> str:
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    text = text.replace("<KEEP_001>", "").replace("<KEEP_002>", "").replace("<KEEP_003>", "")
    return text.strip()


def sanitize_flags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        flag = str(item).strip().upper().replace(" ", "_")
        if flag:
            out.append(flag[:80])
    return out


def write_repaired_outputs(
    report: dict[str, Any],
    report_path: Path,
    en_segments: list[Segment],
    zh_segments: list[Segment],
    repairs: list[RepairResult],
    glossary: dict[str, GlossaryTerm],
    config: dict[str, Any],
    logger: logging.Logger | None,
) -> dict[str, Any]:
    output_dir = ensure_dir(config["output_dir"])
    run_dir = ensure_dir(Path(config["work_dir"]) / now_id(slugify(str(report.get("name", "lecture")), 36) + "_repair"))
    copy_unchanged_tts_cache(report, run_dir, {r.segment_id for r in repairs if r.old_text != r.new_text}, config, logger)

    base = f"{report.get('name', report_path.stem.replace('_report', ''))}_repaired"
    work_video = Path(report.get("work_video") or report.get("source_video"))
    process_duration = media_duration(work_video)

    en_srt = output_dir / f"{base}_en.srt"
    en_vtt = output_dir / f"{base}_en.vtt"
    zh_srt = output_dir / f"{base}_zh.srt"
    zh_vtt = output_dir / f"{base}_zh.vtt"
    bilingual_srt = output_dir / f"{base}_bilingual.srt"
    bilingual_ass = output_dir / f"{base}_bilingual.ass"
    zh_wav = output_dir / f"{base}_zh_dub.wav"
    zh_mp4 = output_dir / f"{base}_zh_dub.mp4"
    hard_mp4 = output_dir / f"{base}_zh_dub_bilingual_hardsub.mp4"

    line_limit = int(config["translation"]["max_zh_chars_per_subtitle_line"])
    write_srt(en_srt, en_segments)
    write_vtt(en_vtt, en_segments)
    write_srt(zh_srt, zh_segments, cjk=True, line_limit=line_limit)
    write_vtt(zh_vtt, zh_segments, cjk=True, line_limit=line_limit)
    write_srt(bilingual_srt, bilingual_segments(en_segments, zh_segments), cjk=False)
    write_bilingual_ass(bilingual_ass, en_segments, zh_segments)

    tts_info = build_aligned_dub(zh_segments, process_duration, zh_wav, run_dir, config, logger)
    mux_video(work_video, zh_wav, zh_mp4, config, logger)
    hard_ok = hardsub_video(zh_mp4, bilingual_ass, hard_mp4, logger) if config.get("mux", {}).get("hard_subtitle", True) else False

    outputs = {
        "en_srt": str(en_srt),
        "en_vtt": str(en_vtt),
        "zh_srt": str(zh_srt),
        "zh_vtt": str(zh_vtt),
        "bilingual_srt": str(bilingual_srt),
        "bilingual_ass": str(bilingual_ass),
        "zh_dub_wav": str(zh_wav),
        "zh_dub_mp4": str(zh_mp4),
    }
    if hard_ok:
        outputs["zh_dub_bilingual_hardsub_mp4"] = str(hard_mp4)

    repair_flags = {r.segment_id: r.flags for r in repairs}
    traces = [
        TranslationTrace(
            segment_id=en.id,
            original_text=en.text,
            zh_literal=zh.text,
            zh_lecture=zh.text,
            duration=en.duration,
            target_char_limit=target_limit(en, config),
            flags=repair_flags.get(en.id, []),
        )
        for en, zh in zip(en_segments, zh_segments)
    ]
    qa = run_qa(outputs, en_segments, zh_segments, glossary, traces, tts_info, process_duration, config)
    result = {
        "name": base,
        "run_id": run_dir.name,
        "mode": "fidelity_repair",
        "source_video": report.get("source_video"),
        "work_video": str(work_video),
        "source_report": str(report_path),
        "subtitle_source": report.get("subtitle_source"),
        "asr_backend": report.get("asr_backend"),
        "translation_backend": "local_llm_fidelity_repair",
        "audio_enhancement": report.get("audio_enhancement"),
        "tts": tts_info,
        "outputs": outputs,
        "qa": qa,
        "repairs": [r.__dict__ for r in repairs],
        "report_md": str(output_dir / f"{base}_report.md"),
        "report_json": str(output_dir / f"{base}_report.json"),
        "segments": {"en": to_dicts(en_segments), "zh": to_dicts(zh_segments)},
    }
    write_video_report(result["report_md"], result["report_json"], result)
    write_json(run_dir / "result.json", result)
    return result


def copy_unchanged_tts_cache(
    report: dict[str, Any],
    run_dir: Path,
    changed_ids: set[int],
    config: dict[str, Any],
    logger: logging.Logger | None,
) -> None:
    old_run_id = report.get("run_id")
    if not old_run_id:
        return
    old_tts = Path(config["work_dir"]) / str(old_run_id) / "tts_segments"
    new_tts = ensure_dir(run_dir / "tts_segments")
    if not old_tts.exists():
        return
    copied = 0
    for src in old_tts.glob("seg_*_cosy.wav"):
        try:
            sid = int(src.stem.split("_")[1])
        except Exception:
            continue
        if sid in changed_ids:
            continue
        dst = new_tts / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            copied += 1
    if logger:
        logger.info("Copied %d unchanged CosyVoice segment clips from %s", copied, old_tts)


def load_glossary(path: Path) -> dict[str, GlossaryTerm]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    terms: dict[str, GlossaryTerm] = {}
    for row in data:
        if isinstance(row, dict) and row.get("source_term"):
            terms[str(row["source_term"])] = GlossaryTerm(
                source_term=str(row["source_term"]),
                zh_term=str(row.get("zh_term", row["source_term"])),
                type=str(row.get("type", "")),
                confidence=float(row.get("confidence", 0.0) or 0.0),
                first_seen_video=str(row.get("first_seen_video", "")),
                notes=str(row.get("notes", "")),
            )
    return terms


def glossary_prompt_text(glossary: dict[str, GlossaryTerm]) -> str:
    rows = []
    for term in sorted(glossary.values(), key=lambda t: (-t.confidence, t.source_term.lower()))[:240]:
        rows.append(f"{term.source_term}\t{term.zh_term}\t{term.type}")
    return "\n".join(rows)
