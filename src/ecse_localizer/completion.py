from __future__ import annotations

import json
from pathlib import Path

from .utils import slugify


def completed_report_for(video: Path, output_dir: Path) -> Path | None:
    canonical = output_dir / f"{slugify(video.name)}_report.json"
    candidates = sorted(
        {canonical, *output_dir.glob("*_report.json")},
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    seen: set[Path] = set()
    for report in candidates:
        if report in seen or not report.exists():
            continue
        seen.add(report)
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except Exception:
            continue
        mode = str(data.get("mode") or "").lower()
        if mode == "smoke" or "_smoke" in report.stem.lower():
            continue
        source_raw = str(data.get("source_video", ""))
        if source_raw:
            if Path(source_raw).resolve() != video.resolve():
                continue
        elif report != canonical:
            continue
        if not data.get("qa", {}).get("pass"):
            continue
        outputs = data.get("outputs", {})
        required = ["en_srt", "zh_srt", "bilingual_srt", "bilingual_ass", "zh_dub_wav", "zh_dub_mp4"]
        if not all(outputs.get(k) and Path(outputs[k]).exists() for k in required):
            continue
        return report
    return None
