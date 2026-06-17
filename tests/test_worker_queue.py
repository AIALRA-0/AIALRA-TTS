from pathlib import Path

import yaml

from ecse_localizer.webui import (
    WebState,
    build_job_command,
    claim_worker_job,
    command_with_config,
    create_job_record,
    list_jobs,
    retry_job_record,
    soft_delete_job,
    update_job,
    worker_args_from_command,
    worker_status_payload,
    worker_status_changes,
)
from ecse_localizer.worker_client import redacted_command, summarize_result, worker_args


def write_config(tmp_path: Path) -> Path:
    config = {
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "webui": {
            "username": "admin",
            "password": "local-password",
            "session_secret": "unit-test-secret",
            "platform_dir": str(tmp_path / "platform"),
            "upload_dir": str(tmp_path / "uploads"),
            "job_dir": str(tmp_path / "jobs"),
            "worker_token": "worker-token",
            "execution_mode": "worker_queue",
        },
    }
    Path(config["input_dir"]).mkdir()
    Path(config["output_dir"]).mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


def test_worker_args_are_portable(tmp_path):
    state = WebState(write_config(tmp_path))
    command, _ = build_job_command(
        "process_one",
        {"video": r"C:\worker-local\lecture.mp4"},
        state,
        validate_paths=False,
    )
    args = worker_args_from_command(command)
    assert args == ["process-one", "--video", r"C:\worker-local\lecture.mp4"]


def test_worker_status_payload_marks_missing_worker_unavailable(tmp_path):
    state = WebState(write_config(tmp_path))
    payload = worker_status_payload(state)
    assert payload["execution_mode"] == "worker_queue"
    assert payload["worker_required"] is True
    assert payload["heartbeat_online"] is False
    assert payload["available"] is False
    assert payload["status"] == "local"
    assert "queued jobs will wait" in payload["message"]


def test_command_with_config_replaces_only_config_path(tmp_path):
    state = WebState(write_config(tmp_path))
    command, _ = build_job_command("audit", {}, state)
    updated = command_with_config(command, tmp_path / "job.yaml")
    assert updated[updated.index("--config") + 1] == str(tmp_path / "job.yaml")
    assert worker_args_from_command(updated) == ["audit", "--input", str(tmp_path / "input")]


def test_claim_worker_job_marks_job_claimed(tmp_path):
    state = WebState(write_config(tmp_path))
    record = create_job_record(
        state,
        "audit",
        "Audit input directory",
        ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "x"],
        user="admin",
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    claimed = claim_worker_job(state, "worker-1")
    assert claimed
    assert claimed["id"] == record["id"]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "worker-1"
    assert claim_worker_job(state, "worker-1") is None


def test_worker_status_changes_extracts_result_paths_as_fields():
    changes = worker_status_changes(
        {
            "status": "passed",
            "returncode": 0,
            "result": {"pass": True, "report": "demo_report.json", "video": "demo.mp4"},
        }
    )
    assert changes["status"] == "done"
    assert changes["result_report"] == "demo_report.json"
    assert changes["result_video"] == "demo.mp4"


def test_soft_delete_job_hides_record_from_default_history(tmp_path):
    state = WebState(write_config(tmp_path))
    record = create_job_record(
        state,
        "audit",
        "Audit input directory",
        ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "x"],
        user="admin",
        metadata={},
    )
    deleted = soft_delete_job(state, record["id"], deleted_by="admin")
    assert deleted["status"] == "deleted"
    assert list_jobs(state, "admin") == []
    assert list_jobs(state, "admin", include_deleted=True)[0]["id"] == record["id"]


def test_retry_worker_job_requeues_failed_record(tmp_path):
    state = WebState(write_config(tmp_path))
    record = create_job_record(
        state,
        "audit",
        "Audit input directory",
        ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "x"],
        user="admin",
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "failed", "returncode": 1})
    retried = retry_job_record(state, record["id"])
    assert retried["status"] == "retrying"
    assert retried["retry_count"] == 1
    claimed = claim_worker_job(state, "worker-1")
    assert claimed["id"] == record["id"]
    assert claimed["status"] == "claimed"


def test_worker_client_helpers_redact_local_config():
    job = {"metadata": {"worker_args": ["audit", "--input", "x"]}}
    assert worker_args(job) == ["audit", "--input", "x"]
    assert redacted_command(["python", "-m", "ecse_localizer", "--config", r"C:\secret\config.yaml", "audit"]) == [
        "python",
        "-m",
        "ecse_localizer",
        "--config",
        "<local-config>",
        "audit",
    ]
    assert summarize_result({"pass": True, "report": r"C:\secret\demo_report.json"}) == {"pass": True, "report": "demo_report.json"}
