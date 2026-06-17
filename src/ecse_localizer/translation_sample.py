from __future__ import annotations

from pathlib import Path
from typing import Any

from .fidelity import heuristic_fidelity_issues
from .repair import select_repair_ids
from .subtitle_io import Segment
from .translate import build_translation_paragraphs, default_style_guide, numbers_missing
from .translation_quality import assess_translation_quality, quality_flag_severity
from .utils import ensure_dir, write_json


def build_translation_quality_sample(config: dict[str, Any]) -> dict[str, Any]:
    segments = [
        Segment(
            1,
            0.0,
            7.0,
            "Today we derive Vout = Vin * R2 / (R1 + R2), using R1 = 10 kOhm and R2 = 5 kOhm, then connect it to sensor_readout.py.",
        ),
        Segment(
            2,
            7.2,
            13.0,
            "The key point is that the calibration value must be updated before the next measurement.",
        ),
    ]
    paragraphs = build_translation_paragraphs(
        segments,
        {
            "translation": {
                "paragraph_max_gap_seconds": 1.0,
                "paragraph_max_source_chars": 600,
                "paragraph_max_duration_seconds": 30,
                "paragraph_min_segments_before_sentence_break": 2,
            }
        },
    )
    rows = [
        sample_row(
            segments[0],
            literal="今天我们推导 Vout = Vin * R2 / (R1 + R2)，使用 R1 = 10 kOhm 和 R2 = 5 kOhm，然后把它连接到 sensor_readout.py。",
            lecture="今天我们先推导 Vout = Vin * R2 / (R1 + R2)，其中 R1 = 10 kOhm、R2 = 5 kOhm，再把结果接到 sensor_readout.py。",
            coherence="今天我们先推导 Vout = Vin * R2 / (R1 + R2)。在 R1 = 10 kOhm、R2 = 5 kOhm 的条件下，再把这个结果用于 sensor_readout.py 的传感器读出。",
            bad_for_repair="这一段主要围绕分压器展开。",
            repair="今天我们先推导 Vout = Vin * R2 / (R1 + R2)。这里 R1 = 10 kOhm、R2 = 5 kOhm，接着把结果用于 sensor_readout.py 的传感器读出。",
            protected_terms=["Vout", "Vin", "R1", "R2", "10", "5", "sensor_readout.py"],
            config=config,
        ),
        sample_row(
            segments[1],
            literal="关键点是校准值必须在下一次测量之前被更新。",
            lecture="关键点是：下一次测量之前，必须先更新校准值。",
            coherence="也就是说，进入下一次测量前，校准值要先更新好，这样读数才有意义。",
            bad_for_repair="这里主要是在讲校准。",
            repair="也就是说，进入下一次测量前，必须先更新校准值，这样后面的读数才可靠。",
            protected_terms=["校准值", "测量"],
            config=config,
        ),
    ]
    return {
        "pass": all(row["pass"] for row in rows),
        "mode": "best_quality_translation_sample",
        "style_guide": default_style_guide(config),
        "paragraphs": [
            {"id": paragraph.id, "segment_ids": paragraph.segment_ids, "text": paragraph.text}
            for paragraph in paragraphs
        ],
        "rows": rows,
        "checks": {
            "has_literal_lecture_coherence_repair": all(
                all(row.get(key) for key in ["zh_literal", "zh_lecture", "zh_coherence", "zh_repair"])
                for row in rows
            ),
            "all_repairs_selected": all(row["repair_selected"] for row in rows),
            "all_repairs_preserve_numbers": all(not row["missing_numbers_after_repair"] for row in rows),
            "all_repairs_preserve_terms": all(not row["missing_terms_after_repair"] for row in rows),
        },
    }


def sample_row(
    segment: Segment,
    *,
    literal: str,
    lecture: str,
    coherence: str,
    bad_for_repair: str,
    repair: str,
    protected_terms: list[str],
    config: dict[str, Any],
) -> dict[str, Any]:
    bad_issues = heuristic_fidelity_issues([{"id": segment.id, "text": segment.text}], [{"id": segment.id, "text": bad_for_repair}])
    fidelity = {
        "reviews": [{"id": segment.id, "score": 2, "faithful": False, "summary_like": True}],
        "issues": bad_issues,
    }
    repair_ids = select_repair_ids(fidelity, max_score=3, include_high=True)
    stage_flags = {
        "literal": assess_translation_quality(segment.text, literal, literal, config),
        "lecture": assess_translation_quality(segment.text, lecture, literal, config),
        "coherence": assess_translation_quality(segment.text, coherence, literal, config),
        "repair_input": assess_translation_quality(segment.text, bad_for_repair, literal, config),
        "repair": assess_translation_quality(segment.text, repair, literal, config),
    }
    missing_terms = [term for term in protected_terms if is_source_token(term) and term not in repair]
    missing_numbers = numbers_missing(segment.text, repair)
    repair_selected = segment.id in repair_ids
    high_flags_after_repair = quality_flag_severity(stage_flags["repair"]) == "high"
    return {
        "id": segment.id,
        "original_text": segment.text,
        "duration": round(segment.duration, 3),
        "zh_literal": literal,
        "zh_lecture": lecture,
        "zh_coherence": coherence,
        "bad_translation_for_repair": bad_for_repair,
        "zh_repair": repair,
        "protected_terms": protected_terms,
        "stage_flags": stage_flags,
        "fidelity_issues_for_bad_translation": bad_issues,
        "repair_selected": repair_selected,
        "missing_terms_after_repair": missing_terms,
        "missing_numbers_after_repair": missing_numbers,
        "pass": repair_selected and not missing_terms and not missing_numbers and not high_flags_after_repair,
    }


def is_source_token(term: str) -> bool:
    return any(ch.isascii() and (ch.isalpha() or ch.isdigit()) for ch in term)


def write_translation_quality_sample(output_dir: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    out = ensure_dir(output_dir)
    sample = build_translation_quality_sample(config)
    json_path = out / "translation_quality_sample.json"
    md_path = out / "translation_quality_sample.md"
    write_json(json_path, sample)
    md_path.write_text(render_markdown(sample), encoding="utf-8")
    return {"pass": sample["pass"], "json": str(json_path), "markdown": str(md_path), "sample": sample}


def render_markdown(sample: dict[str, Any]) -> str:
    lines = [
        "# Translation Quality Sample",
        "",
        f"Status: {'PASS' if sample.get('pass') else 'WARN'}",
        "",
        "## Style Guide",
        "",
        str(sample.get("style_guide", "")).strip(),
        "",
        "## Stage Comparison",
        "",
    ]
    for row in sample.get("rows", []):
        lines.extend(
            [
                f"### Segment {row['id']}",
                "",
                f"- Original: {row['original_text']}",
                f"- Literal: {row['zh_literal']}",
                f"- Lecture: {row['zh_lecture']}",
                f"- Coherence: {row['zh_coherence']}",
                f"- Bad repair input: {row['bad_translation_for_repair']}",
                f"- Repair: {row['zh_repair']}",
                f"- Repair selected: {row['repair_selected']}",
                f"- Missing terms after repair: {', '.join(row['missing_terms_after_repair']) or 'none'}",
                f"- Missing numbers after repair: {', '.join(row['missing_numbers_after_repair']) or 'none'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
