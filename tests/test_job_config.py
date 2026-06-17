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


def test_job_overrides_apply_template_runtime_params():
    out = apply_job_overrides(
        {"translation": {}, "tts": {}, "mux": {}},
        {
            "template_id": "tpl_1",
            "tts_speed": 1.1,
            "tts_emotion": "calm",
            "tts_end_gap_seconds": 0.35,
            "tts_min_audio_gap_seconds": 0.12,
            "tts_speaker_gender": "female",
            "mux_keep_original_audio": False,
            "mux_original_audio_volume": 0.05,
            "mux_hard_subtitle": False,
            "mux_soft_subtitle": True,
            "max_subtitle_line_chars": 24,
        },
    )
    assert out["tts"]["speed"] == 1.1
    assert out["tts"]["emotion"] == "calm"
    assert out["tts"]["end_gap_seconds"] == 0.35
    assert out["tts"]["min_audio_gap_seconds"] == 0.12
    assert out["tts"]["speaker_gender"] == "female"
    assert out["mux"]["keep_original_audio"] is False
    assert out["mux"]["original_audio_volume"] == 0.05
    assert out["mux"]["hard_subtitle"] is False
    assert out["mux"]["soft_subtitle"] is True
    assert out["translation"]["max_zh_chars_per_subtitle_line"] == 24
    assert out["job"]["metadata"]["template_id"] == "tpl_1"


def test_write_job_config_omits_runtime_project_root(tmp_path: Path):
    path = write_job_config({"project_root": "private", "asr": {"language": "en"}}, {"source_language": "auto"}, job_id="job:1", root=tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "project_root" not in data
    assert data["asr"]["language"] is None
    assert path.name == "job_1.yaml"


def test_write_job_config_strips_webui_secrets(tmp_path: Path):
    path = write_job_config(
        {
            "project_root": "private",
            "input_dir": "input",
            "output_dir": "output",
            "work_dir": "work",
            "webui": {
                "username": "admin",
                "password": "local-password",
                "session_secret": "session-secret",
                "download_secret": "download-secret",
                "worker_token": "worker-token",
                "preview_dir": "preview-cache",
                "worker_preview_max_upload_mb": 256,
            },
        },
        {"source_language": "auto"},
        job_id="job-secret-test",
        root=tmp_path,
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["webui"]["preview_dir"] == "preview-cache"
    assert data["webui"]["worker_preview_max_upload_mb"] == 256
    for key in ["username", "password", "session_secret", "download_secret", "worker_token"]:
        assert key not in data["webui"]
    rendered = path.read_text(encoding="utf-8")
    assert "local-password" not in rendered
    assert "session-secret" not in rendered
    assert "download-secret" not in rendered
    assert "worker-token" not in rendered


def test_asr_language_auto_and_null_enable_detection():
    assert asr_language({"asr": {"language": "auto"}}) is None
    assert asr_language({"asr": {"language": None}}) is None
    assert asr_language({"asr": {"language": "en"}}) == "en"
