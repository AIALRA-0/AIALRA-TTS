from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .utils import PROJECT_ROOT, ensure_dir


def apply_job_overrides(config: dict[str, Any], metadata: dict[str, Any] | None) -> dict[str, Any]:
    out = deepcopy(config)
    meta = metadata or {}
    source_language = clean_value(meta.get("source_language"))
    target_subtitle_language = clean_value(meta.get("target_subtitle_language"))
    target_tts_language = clean_value(meta.get("target_tts_language"))
    quality_mode = clean_value(meta.get("quality_mode"))
    style = clean_value(meta.get("style"))

    if source_language:
        out.setdefault("asr", {})["language"] = None if source_language.lower() == "auto" else source_language
        out.setdefault("translation", {})["source_language"] = source_language
    if target_subtitle_language:
        out.setdefault("translation", {})["target_language"] = target_subtitle_language
    if target_tts_language:
        out.setdefault("tts", {})["language"] = target_tts_language
    if quality_mode:
        out.setdefault("translation", {})["quality_mode"] = quality_mode
    if style:
        out.setdefault("translation", {})["style"] = style

    out.setdefault("job", {})["metadata"] = {
        key: value
        for key, value in meta.items()
        if key
        in {
            "project_id",
            "folder_id",
            "source_language",
            "target_subtitle_language",
            "target_tts_language",
            "quality_mode",
            "style",
        }
    }
    return out


def write_job_config(
    base_config: dict[str, Any],
    metadata: dict[str, Any] | None,
    *,
    job_id: str,
    root: str | Path | None = None,
) -> Path:
    root_path = ensure_dir(root or PROJECT_ROOT / "runs" / "job_configs")
    config = apply_job_overrides(base_config, metadata)
    data = json.loads(json.dumps(config, ensure_ascii=False))
    data.pop("project_root", None)
    path = root_path / f"{safe_filename(job_id)}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def clean_value(value: Any) -> str:
    text = str(value or "").strip()
    return text


def safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return cleaned[:120] or "job"
