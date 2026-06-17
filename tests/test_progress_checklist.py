import json
from pathlib import Path

import yaml

from ecse_localizer.cli import main
from ecse_localizer.progress_checklist import (
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_VALIDATION,
    build_progress_checklist,
    write_progress_checklist,
)
from ecse_localizer.utils import read_json


def config(tmp_path: Path) -> dict:
    return {
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
    }


def write_platform_report(tmp_path: Path, *, passed: bool = True) -> Path:
    path = tmp_path / "runs" / "platform_check" / "platform_check_report.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "pass": passed,
                "summary": {"checked_gates": 6, "failed_gates": [] if passed else ["webui_api_smoke"]},
                "gates": {
                    "release_check": {"pass": True},
                    "translation_sample": {"pass": True},
                    "remote_smoke": {"pass": True},
                    "webui_api_smoke": {"pass": passed},
                    "deploy_template_guard": {"pass": True},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_smoke_report(tmp_path: Path, *, passed: bool = True) -> Path:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "en_srt": output / "sample_smoke_90s_en.srt",
        "zh_srt": output / "sample_smoke_90s_zh.srt",
        "bilingual_srt": output / "sample_smoke_90s_bilingual.srt",
        "bilingual_ass": output / "sample_smoke_90s_bilingual.ass",
        "zh_dub_wav": output / "sample_smoke_90s_zh_dub.wav",
        "zh_dub_mp4": output / "sample_smoke_90s_zh_dub.mp4",
    }
    for path in paths.values():
        path.write_text("ok", encoding="utf-8")
    path = output / "sample_smoke_90s_report.json"
    path.write_text(
        json.dumps(
            {
                "name": "sample_smoke_90s",
                "mode": "smoke",
                "source_video": str(tmp_path / "input" / "sample.mp4"),
                "asr_backend": "existing_subtitle",
                "translation_backend": "local_llm",
                "tts": {"backend": "cosyvoice_sft", "segment_count": 12},
                "outputs": {key: str(value) for key, value in paths.items()},
                "qa": {"pass": passed, "issues": []},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_progress_checklist_tracks_objective_and_latest_platform_gate(tmp_path):
    write_platform_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_platform_check"]["pass"] is True
    assert checklist["summary"]["total"] >= 20
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["translation.best_quality"]["status"] == STATUS_DONE
    assert rows["worker.lifecycle"]["status"] == STATUS_DONE
    assert rows["storage.preview_cache"]["status"] == STATUS_DONE
    assert rows["deploy.real_contabo"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.smoke_90s"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.first_full_lecture"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.batch_all"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.real_video"]["status"] == STATUS_NEEDS_VALIDATION


def test_progress_checklist_marks_real_smoke_done_but_full_validation_in_progress(tmp_path):
    write_platform_report(tmp_path, passed=True)
    smoke_path = write_smoke_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_real_video_smoke"]["pass"] is True
    assert checklist["latest_real_video_smoke"]["path"] == str(smoke_path)
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["validation.smoke_90s"]["status"] == STATUS_DONE
    assert rows["validation.real_video"]["status"] == STATUS_IN_PROGRESS
    assert rows["validation.first_full_lecture"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.batch_all"]["status"] == STATUS_NEEDS_VALIDATION


def test_write_progress_checklist_creates_markdown_and_json(tmp_path):
    write_platform_report(tmp_path, passed=True)
    out = tmp_path / "checklist"

    result = write_progress_checklist(out, config(tmp_path))

    json_path = Path(result["json"])
    md_path = Path(result["markdown"])
    assert json_path.exists()
    assert md_path.exists()
    assert read_json(json_path)["mode"] == "aialra_progress_checklist"
    text = md_path.read_text(encoding="utf-8")
    assert "AIALRA Local Video Localizer Progress Checklist" in text
    assert "deploy.real_contabo" in text
    assert "validation.smoke_90s" in text
    assert "validation.real_video" in text


def test_progress_checklist_cli_writes_reports(tmp_path, capsys):
    cfg = config(tmp_path)
    Path(cfg["input_dir"]).mkdir()
    Path(cfg["output_dir"]).mkdir()
    Path(cfg["work_dir"]).mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"

    rc = main(["--config", str(config_path), "progress-checklist", "--output", str(out), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["mode"] == "aialra_progress_checklist"
    assert Path(payload["json"]).exists()
    assert Path(payload["markdown"]).exists()
