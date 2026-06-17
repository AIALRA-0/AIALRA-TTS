from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import load_config
from .llm_local import LocalLLMClient
from .utils import PROJECT_ROOT, ensure_dir, write_json


def run_fidelity_audit(
    report_json: str | Path,
    config: dict | None = None,
    *,
    output_json: str | Path | None = None,
    output_md: str | Path | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    config = config or load_config()
    report_path = Path(report_json)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    en_segments = report.get("segments", {}).get("en", [])
    zh_segments = report.get("segments", {}).get("zh", [])
    if len(en_segments) != len(zh_segments):
        raise RuntimeError(f"Segment count mismatch: en={len(en_segments)} zh={len(zh_segments)}")

    client = LocalLLMClient(config)
    status = client.status()
    if not status.available:
        raise RuntimeError(status.message)
    if status.model and "14b" not in status.model.lower():
        raise RuntimeError(f"Fidelity audit requires the configured 14B model; active model is {status.model}")

    output_json = Path(output_json) if output_json else report_path.with_name(report_path.stem.replace("_report", "") + "_fidelity_report.json")
    output_md = Path(output_md) if output_md else output_json.with_suffix(".md")
    prompt = (PROJECT_ROOT / "prompts" / "fidelity_check.md").read_text(encoding="utf-8")
    chunk_size = int(config.get("llm", {}).get("fidelity_chunk_size", 6))

    reviews: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    pairs = list(zip(en_segments, zh_segments))
    for start in range(0, len(pairs), max(1, chunk_size)):
        chunk = pairs[start : start + max(1, chunk_size)]
        if logger:
            logger.info("Fidelity audit segments %d-%d / %d", chunk[0][0]["id"], chunk[-1][0]["id"], len(pairs))
        reviews.extend(review_chunk(client, prompt, chunk, logger))
        write_json(output_json, build_audit(report, status.model, reviews, issues=[], partial=True))

    issues.extend(heuristic_fidelity_issues(en_segments, zh_segments))
    issues.extend(review_issues(reviews, en_segments))
    audit = build_audit(report, status.model, reviews, issues=issues, partial=False)
    write_json(output_json, audit)
    write_fidelity_markdown(output_md, audit)
    return audit


def review_chunk(
    client: LocalLLMClient,
    prompt: str,
    chunk: list[tuple[dict[str, Any], dict[str, Any]]],
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    payload = {
        "segments": [
            {
                "id": en["id"],
                "english": en["text"],
                "chinese": zh["text"],
                "duration": round(float(en.get("end", 0)) - float(en.get("start", 0)), 3),
            }
            for en, zh in chunk
        ]
    }
    schema = (
        '{"segments":[{"id":1,"faithful":true,"summary_like":false,'
        '"missing_key_info":[],"added_info":[],"number_or_name_issue":[],"score":5,"notes":""}]}'
    )
    for attempt in range(1, 4):
        try:
            data = client.json_chat(prompt, json.dumps(payload, ensure_ascii=False), schema)
            rows = data.get("segments", [])
            by_id = {int(row.get("id")): normalize_review(row) for row in rows if str(row.get("id", "")).isdigit()}
            missing = [en["id"] for en, _ in chunk if int(en["id"]) not in by_id]
            if missing:
                raise RuntimeError(f"missing review ids: {missing[:8]}")
            return [by_id[int(en["id"])] for en, _ in chunk]
        except Exception as exc:
            if logger:
                logger.warning("Fidelity chunk attempt %d failed at segment %s: %s", attempt, chunk[0][0]["id"], exc)
    return [
        {
            "id": int(en["id"]),
            "faithful": False,
            "summary_like": False,
            "missing_key_info": [],
            "added_info": [],
            "number_or_name_issue": [],
            "score": 1,
            "notes": "LLM fidelity review failed",
        }
        for en, _ in chunk
    ]


def normalize_review(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row.get("id", 0)),
        "faithful": bool(row.get("faithful", False)),
        "summary_like": bool(row.get("summary_like", False)),
        "missing_key_info": as_str_list(row.get("missing_key_info", [])),
        "added_info": as_str_list(row.get("added_info", [])),
        "number_or_name_issue": as_str_list(row.get("number_or_name_issue", [])),
        "score": max(1, min(5, int(row.get("score", 1) or 1))),
        "notes": str(row.get("notes", ""))[:500],
    }


def as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v)[:200] for v in value if str(v).strip()]


def heuristic_fidelity_issues(en_segments: list[dict[str, Any]], zh_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for en, zh in zip(en_segments, zh_segments):
        sid = int(en["id"])
        en_text = str(en.get("text", ""))
        zh_text = str(zh.get("text", ""))
        if re.search(r"这一段|这里主要|本段|该片段|请结合英文字幕复核", zh_text):
            issues.append({"type": "summary_or_commentary_phrase", "severity": "high", "segment_id": sid, "zh": zh_text})
        missing_numbers = [n for n in re.findall(r"\d+(?:\.\d+)?", en_text) if n not in re.findall(r"\d+(?:\.\d+)?", zh_text)]
        if missing_numbers:
            issues.append({"type": "number_mismatch", "severity": "medium", "segment_id": sid, "missing": missing_numbers[:8]})
        missing_names = [
            token
            for token in sorted(set(re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9-]{1,}(?![A-Za-z0-9])", en_text)))
            if token not in zh_text
        ]
        if missing_names:
            issues.append({"type": "acronym_or_name_mismatch", "severity": "medium", "segment_id": sid, "missing": missing_names[:8]})
        en_words = len(re.findall(r"[A-Za-z0-9]+", en_text))
        zh_cjk = len(re.findall(r"[\u4e00-\u9fff]", zh_text))
        if en_words >= 16 and zh_cjk < max(8, int(en_words * 0.45)):
            issues.append(
                {
                    "type": "possibly_overcompressed_translation",
                    "severity": "medium",
                    "segment_id": sid,
                    "en_words": en_words,
                    "zh_cjk": zh_cjk,
                }
            )
    return issues


def review_issues(reviews: list[dict[str, Any]], en_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    en_by_id = {int(seg["id"]): str(seg.get("text", "")) for seg in en_segments}
    for row in reviews:
        source_text = en_by_id.get(int(row["id"]), "")
        low_info_fragment = is_low_information_fragment(source_text)
        severity = "medium"
        if row["summary_like"]:
            severity = "high"
        elif row["score"] <= 2 and not low_info_fragment:
            severity = "high"
        if severity == "high" or row["score"] == 3 or row["missing_key_info"] or row["added_info"] or row["number_or_name_issue"]:
            issues.append(
                {
                    "type": "llm_fidelity_review",
                    "severity": severity,
                    "segment_id": row["id"],
                    "faithful": row["faithful"],
                    "summary_like": row["summary_like"],
                    "score": row["score"],
                    "missing_key_info": row["missing_key_info"],
                    "added_info": row["added_info"],
                    "number_or_name_issue": row["number_or_name_issue"],
                    "notes": row["notes"],
                }
            )
    return issues


def is_low_information_fragment(text: str) -> bool:
    words = re.findall(r"[A-Za-z0-9$%.-]+", text or "")
    if len(words) <= 4:
        return True
    lower = " ".join(w.lower() for w in words)
    fragment_markers = [
        "so that s",
        "and i ll",
        "and in the",
        "so it s a",
        "for all the",
        "that s the level of",
        "because i want",
    ]
    return len(words) <= 8 and any(marker in lower for marker in fragment_markers)


def build_audit(
    report: dict[str, Any],
    model: str | None,
    reviews: list[dict[str, Any]],
    *,
    issues: list[dict[str, Any]],
    partial: bool,
) -> dict[str, Any]:
    high = [issue for issue in issues if issue.get("severity") == "high"]
    scores = [int(row.get("score", 0)) for row in reviews if row.get("score")]
    return {
        "source_report": report.get("report_json"),
        "source_video": report.get("source_video"),
        "model": model,
        "segment_count": len(report.get("segments", {}).get("en", [])),
        "reviewed_count": len(reviews),
        "partial": partial,
        "pass": (not partial) and not high and len(reviews) == len(report.get("segments", {}).get("en", [])),
        "average_score": round(sum(scores) / len(scores), 3) if scores else 0,
        "score_counts": {str(i): scores.count(i) for i in range(1, 6)},
        "issues": issues,
        "reviews": reviews,
    }


def write_fidelity_markdown(path: str | Path, audit: dict[str, Any]) -> None:
    lines = [
        "# Translation Fidelity Audit",
        "",
        f"- Source video: `{audit.get('source_video')}`",
        f"- Local LLM judge: `{audit.get('model')}`",
        f"- Segments reviewed: {audit.get('reviewed_count')} / {audit.get('segment_count')}",
        f"- Average score: {audit.get('average_score')}",
        f"- PASS: {audit.get('pass')}",
        "",
        "## Issues",
        "",
    ]
    if not audit.get("issues"):
        lines.append("- None")
    else:
        for issue in audit["issues"][:200]:
            lines.append(f"- [{issue.get('severity')}] segment {issue.get('segment_id')}: {issue.get('type')} `{issue}`")
    ensure_dir(Path(path).parent)
    Path(path).write_text("\n".join(lines), encoding="utf-8")
