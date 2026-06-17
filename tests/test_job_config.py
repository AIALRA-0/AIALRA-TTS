from pathlib import Path

import yaml

from ecse_localizer.asr import asr_language
from ecse_localizer.job_config import apply_job_overrides, write_job_config


def test_job_overrides_apply_language_style_and_quality():
    config = {
        "project_root": "private-runtime-path",
        "asr": {"language": "en"},
        "translation": {"target_language": "zh-CN", "style": "natural_chinese_lecture"},
        "tts": {"language": "zh-CN"},
    }
    out = apply_job_overrides(
        config,
        {
            "project_id": "p1",
            "folder_id": "root",
            "source_language": "auto",
            "target_subtitle_language": "ja",
            "target_tts_language": "ko",
            "quality_mode": "best_quality",
            "style": "clear technical lecture",
            "worker_args": ["process-all"],
        },
    )
    assert out["asr"]["language"] is None
    assert out["translation"]["source_language"] == "auto"
    assert out["translation"]["target_language"] == "ja"
    assert out["translation"]["quality_mode"] == "best_quality"
    assert out["translation"]["style"] == "clear technical lecture"
    assert out["tts"]["language"] == "ko"
    assert "worker_args" not in out["job"]["metadata"]


def test_write_job_config_omits_runtime_project_root(tmp_path: Path):
    path = write_job_config({"project_root": "private", "asr": {"language": "en"}}, {"source_language": "auto"}, job_id="job:1", root=tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "project_root" not in data
    assert data["asr"]["language"] is None
    assert path.name == "job_1.yaml"


def test_asr_language_auto_and_null_enable_detection():
    assert asr_language({"asr": {"language": "auto"}}) is None
    assert asr_language({"asr": {"language": None}}) is None
    assert asr_language({"asr": {"language": "en"}}) == "en"
