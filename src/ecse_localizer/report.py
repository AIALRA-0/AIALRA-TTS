from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import write_json


def write_audit_report(output_dir: str | Path, audit: dict[str, Any]) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "audit_report.json", audit)
    lines = [
        "# Audit Report",
        "",
        f"- Input: `{audit.get('input_dir')}`",
        f"- Videos: {audit.get('video_count')}",
        f"- Subtitles: {audit.get('subtitle_count')}",
        "",
        "| # | Duration | Resolution | Audio | Existing subtitles | Needs ASR | File |",
        "|---:|---:|---|---:|---:|---|---|",
    ]
    for i, video in enumerate(audit.get("videos", []), start=1):
        subs = len(video.get("subtitles", []))
        lines.append(
            f"| {i} | {float(video.get('duration') or 0):.1f}s | {video.get('resolution','')} | "
            f"{video.get('audio_tracks',0)} | {subs} | {video.get('needs_asr')} | {Path(video.get('path','')).name} |"
        )
    (out / "audit_report.md").write_text("\n".join(lines), encoding="utf-8")


def write_video_report(path_md: str | Path, path_json: str | Path, data: dict[str, Any]) -> None:
    write_json(path_json, data)
    lines = [
        f"# Localizer QA Report: {data.get('name')}",
        "",
        f"- Source video: `{data.get('source_video')}`",
        f"- Mode: {data.get('mode')}",
        f"- Subtitle source: {data.get('subtitle_source')}",
        f"- ASR backend: {data.get('asr_backend')}",
        f"- ASR language: requested={data.get('asr', {}).get('requested_language', 'unknown')}, backend={data.get('asr', {}).get('backend_language', 'unknown')}, detected={data.get('asr', {}).get('detected_language', 'unknown')}, probability={data.get('asr', {}).get('language_probability')}",
        f"- LLM/translation backend: {data.get('translation_backend')}",
        f"- TTS backend: {data.get('tts', {}).get('backend')}",
        f"- Audio enhancement: {data.get('audio_enhancement')}",
        f"- QA pass: {data.get('qa', {}).get('pass')}",
        "",
        "## Outputs",
        "",
    ]
    for key, value in data.get("outputs", {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Issues", ""])
    issues = data.get("qa", {}).get("issues", [])
    if not issues:
        lines.append("- None")
    else:
        for issue in issues:
            lines.append(f"- [{issue.get('severity')}] {issue.get('type')}: `{issue}`")

    trace_flags = data.get("qa", {}).get("trace_flags", {})
    actionable_flags = data.get("qa", {}).get("actionable_trace_flags", {})
    lines.extend(["", "## Translation Quality Flags", ""])
    if not trace_flags:
        lines.append("- None")
    else:
        lines.extend(["| Flag | Count | Actionable |", "|---|---:|---|"])
        for flag, count in sorted(trace_flags.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"| `{flag}` | {count} | {'yes' if flag in actionable_flags else 'no'} |")
    samples = data.get("qa", {}).get("translation_flag_samples", [])
    if samples:
        lines.extend(["", "### Flagged Segment Samples", ""])
        for row in samples[:10]:
            flags = ", ".join(f"`{flag}`" for flag in row.get("flags", []))
            paragraph = row.get("paragraph_id")
            paragraph_note = f", paragraph {paragraph}" if paragraph else ""
            lines.append(f"- Segment {row.get('segment_id')}{paragraph_note}: {flags}")
            if row.get("original_text"):
                lines.append(f"  - Source: {markdown_inline(row.get('original_text', ''))}")
            if row.get("zh_literal"):
                lines.append(f"  - Literal: {markdown_inline(row.get('zh_literal', ''))}")
            if row.get("zh_lecture"):
                lines.append(f"  - Lecture: {markdown_inline(row.get('zh_lecture', ''))}")
    lines.extend(["", "## First 10 Subtitles", ""])
    for row in data.get("qa", {}).get("first_10_subtitles", []):
        lines.append(f"{row['id']}. {row['start']:.2f}-{row['end']:.2f}")
        lines.append(f"   - ZH: {row['zh']}")
        lines.append(f"   - EN: {row['en']}")
    lines.extend(["", "## Glossary Sample", ""])
    for term in data.get("qa", {}).get("glossary_sample", []):
        lines.append(f"- {term['source_term']} -> {term['zh_term']} ({term['type']}, {term['confidence']})")
    Path(path_md).write_text("\n".join(lines), encoding="utf-8")


def markdown_inline(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "..."
    return "`" + text.replace("`", "'") + "`"


def write_index_report(output_dir: str | Path) -> Path:
    out = Path(output_dir)
    reports = sorted(out.glob("*_report.md"))
    lines = ["# Localizer Output Index", ""]
    for report in reports:
        lines.append(f"- [{report.name}]({report.name})")
    index = out / "index.md"
    index.write_text("\n".join(lines), encoding="utf-8")
    return index
