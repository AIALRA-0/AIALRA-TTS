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
    apply_optional_overrides(out, meta)

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
            "template_id",
            "tts_speed",
            "tts_emotion",
            "tts_end_gap_seconds",
            "tts_min_audio_gap_seconds",
            "tts_speaker_gender",
            "mux_keep_original_audio",
            "mux_original_audio_volume",
            "mux_hard_subtitle",
            "mux_soft_subtitle",
            "max_subtitle_line_chars",
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
    strip_job_config_secrets(data)
    path = root_path / f"{safe_filename(job_id)}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def clean_value(value: Any) -> str:
    text = str(value or "").strip()
    return text


def apply_optional_overrides(config: dict[str, Any], meta: dict[str, Any]) -> None:
    set_float(config, ["tts", "speed"], meta.get("tts_speed"))
    set_text(config, ["tts", "emotion"], meta.get("tts_emotion"))
    set_float(config, ["tts", "end_gap_seconds"], meta.get("tts_end_gap_seconds"))
    set_float(config, ["tts", "min_audio_gap_seconds"], meta.get("tts_min_audio_gap_seconds"))
    set_text(config, ["tts", "speaker_gender"], meta.get("tts_speaker_gender"))
    set_bool(config, ["mux", "keep_original_audio"], meta.get("mux_keep_original_audio"))
    set_float(config, ["mux", "original_audio_volume"], meta.get("mux_original_audio_volume"))
    set_bool(config, ["mux", "hard_subtitle"], meta.get("mux_hard_subtitle"))
    set_bool(config, ["mux", "soft_subtitle"], meta.get("mux_soft_subtitle"))
    line_chars = meta.get("max_subtitle_line_chars")
    if line_chars not in {None, ""}:
        config.setdefault("translation", {})["max_zh_chars_per_subtitle_line"] = int(max(12, min(42, float(line_chars))))


def set_text(config: dict[str, Any], path: list[str], value: Any) -> None:
    text = clean_value(value)
    if text:
        set_nested(config, path, text)


def set_float(config: dict[str, Any], path: list[str], value: Any) -> None:
    if value in {None, ""}:
        return
    set_nested(config, path, float(value))


def set_bool(config: dict[str, Any], path: list[str], value: Any) -> None:
    if value in {None, ""}:
        return
    if isinstance(value, bool):
        parsed = value
    else:
        parsed = str(value).lower() in {"1", "true", "yes", "on"}
    set_nested(config, path, parsed)


def set_nested(config: dict[str, Any], path: list[str], value: Any) -> None:
    cur = config
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def strip_job_config_secrets(config: dict[str, Any]) -> None:
    webui = config.get("webui")
    if not isinstance(webui, dict):
        return
    for key in [
        "username",
        "password",
        "session_secret",
        "download_secret",
        "worker_token",
    ]:
        webui.pop(key, None)


def safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return cleaned[:120] or "job"
