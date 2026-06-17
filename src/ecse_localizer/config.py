from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from typing import Any

import yaml

from .utils import PROJECT_ROOT


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else PROJECT_ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = expand_env_vars(data)
    data["project_root"] = str(PROJECT_ROOT)
    return data


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    Path(path).write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")


def privacy_guard(config: dict[str, Any]) -> None:
    privacy = config.get("privacy", {})
    if privacy.get("allow_cloud_api") is not False:
        raise RuntimeError("Privacy guard requires allow_cloud_api: false")
    if privacy.get("allow_upload_media") is not False:
        raise RuntimeError("Privacy guard requires allow_upload_media: false")
    tts = config.get("tts", {})
    if tts.get("allow_voice_clone") and not privacy.get("allow_voice_clone_without_consent", False):
        raise RuntimeError("Voice cloning is disabled unless explicit consent files are present")


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    if isinstance(value, str):
        return os.path.expandvars(value)
    return value
