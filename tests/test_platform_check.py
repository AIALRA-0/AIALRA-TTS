import json
from pathlib import Path

import yaml

from ecse_localizer.cli import main
from ecse_localizer.platform_check import run_platform_check, webui_api_smoke_gate
from ecse_localizer.utils import PROJECT_ROOT


def config(tmp_path: Path) -> dict:
    return {
        "project_root": str(PROJECT_ROOT),
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "translation": {
            "quality_mode": "best_quality",
            "supported_target_languages": ["zh-CN"],
            "allow_unlisted_targets": True,
        },
        "asr": {"supported_languages": ["auto", "en"]},
        "tts": {"supported_languages": ["zh-CN"]},
        "llm": {"endpoint": "http://127.0.0.1:11434/v1"},
        "webui": {
            "platform_dir": str(tmp_path / "runs" / "platform"),
            "job_dir": str(tmp_path / "runs" / "jobs"),
            "upload_dir": str(tmp_path / "output" / "uploads"),
            "preview_dir": str(tmp_path / "output" / "previews"),
            "preview_manifest": str(tmp_path / "output" / "previews" / "preview_manifest.json"),
        },
    }


def healthy_payload() -> dict:
    return {
        "worker_id": "worker-1",
        "version": "test",
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "metrics": {
            "cpu": {"load_percent": 20},
            "memory": {"used_percent": 30},
            "gpu": [{"available": True, "util_percent": 40, "memory_used_percent": 25}],
            "local_storage": {"managed_bytes": 100, "total_reported_bytes": 100, "roots": []},
        },
        "capabilities": {
            "asr": {"available": True, "supported_languages": ["auto", "en"]},
            "translation": {"available": True, "supported_target_languages": ["zh-CN"]},
            "tts": {"available": True, "supported_languages": ["zh-CN"]},
        },
    }


def test_platform_check_aggregates_release_worker_remote_and_template_gates(tmp_path):
    out = tmp_path / "platform"

    result = run_platform_check(config(tmp_path), output_dir=out, worker_payload=healthy_payload())

    assert result["pass"] is True
    assert result["summary"]["failed_gates"] == []
    assert set(result["gates"]) == {
        "release_check",
        "translation_sample",
        "remote_smoke",
        "webui_api_smoke",
        "worker_health_local",
        "deploy_template_guard",
    }
    assert result["gates"]["webui_api_smoke"]["pass"] is True
    assert result["gates"]["deploy_template_guard"]["placeholder_errors"] > 0
    assert Path(result["json"]).exists()
    assert Path(result["markdown"]).exists()


def test_platform_check_cli_writes_reports(tmp_path, capsys):
    cfg = config(tmp_path)
    Path(cfg["input_dir"]).mkdir()
    Path(cfg["output_dir"]).mkdir()
    Path(cfg["work_dir"]).mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"

    rc = main(["--config", str(config_path), "platform-check", "--output", str(out), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["pass"] is True
    assert payload["summary"]["failed_gates"] == []
    assert Path(payload["json"]).exists()
    assert Path(payload["markdown"]).exists()


def test_webui_api_smoke_gate_uses_isolated_state_and_checks_core_apis(tmp_path):
    out = tmp_path / "platform"

    result = webui_api_smoke_gate(config(tmp_path), out)

    assert result["pass"] is True
    assert Path(result["config"]).exists()
    assert Path(result["config"]).is_relative_to(out)
    steps = {step["name"]: step for step in result["steps"]}
    assert steps["auth_required"]["pass"] is True
    assert steps["login"]["pass"] is True
    assert steps["dashboard"]["pass"] is True
    assert steps["quota"]["pass"] is True
    assert steps["capabilities"]["pass"] is True
    assert steps["create_user"]["pass"] is True
    assert steps["create_project"]["pass"] is True
    assert steps["create_folder"]["pass"] is True
    assert steps["create_template"]["pass"] is True
    assert steps["signed_worker_heartbeat"]["pass"] is True
    assert steps["worker_media_refs"]["pass"] is True
    assert steps["queue_worker_ref_job"]["pass"] is True
    assert steps["job_history_filter"]["pass"] is True
    assert steps["worker_claims_job"]["pass"] is True
    assert steps["cancel_claimed_worker_job"]["pass"] is True
    assert steps["worker_control_poll_cancel"]["pass"] is True
    assert steps["worker_reports_cancelled"]["pass"] is True

    repeated = webui_api_smoke_gate(config(tmp_path), out)
    assert repeated["pass"] is True
