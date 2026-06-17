import json
from pathlib import Path

import yaml

from ecse_localizer.webui import (
    WebState,
    active_job_counts,
    build_job_command,
    claim_worker_job,
    command_with_config,
    create_job_record,
    enforce_active_job_limits,
    list_jobs,
    read_job,
    retry_job_record,
    soft_delete_job,
    update_job,
    upload_fits_quota,
    worker_args_from_command,
    worker_status_payload,
    worker_status_changes,
)
from ecse_localizer.worker_client import extract_progress_from_text, redacted_command, summarize_result, worker_args


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


def test_upload_quota_counts_reserved_bytes_without_double_counting_current_file():
    assert upload_fits_quota(base_used_bytes=4, reserved_bytes=0, current_file_bytes=6, quota_bytes=10)
    assert upload_fits_quota(base_used_bytes=4, reserved_bytes=3, current_file_bytes=3, quota_bytes=10)
    assert not upload_fits_quota(base_used_bytes=4, reserved_bytes=3, current_file_bytes=4, quota_bytes=10)


def test_command_with_config_replaces_only_config_path(tmp_path):
    state = WebState(write_config(tmp_path))
    command, _ = build_job_command("audit", {}, state)
    updated = command_with_config(command, tmp_path / "job.yaml")
    assert updated[updated.index("--config") + 1] == str(tmp_path / "job.yaml")
    assert worker_args_from_command(updated) == ["audit", "--input", str(tmp_path / "input")]


def test_legacy_job_record_is_normalized_on_read(tmp_path):
    state = WebState(write_config(tmp_path))
    legacy_path = state.job_dir / "legacy_passed.json"
    legacy_path.write_text(
        json.dumps(
            {
                "id": "legacy_passed",
                "status": "passed",
                "command": 'python -m ecse_localizer audit --input "C:\\Course Root"',
            }
        ),
        encoding="utf-8",
    )

    record = read_job(state, "legacy_passed")
    assert record["schema_version"] == 2
    assert record["status"] == "done"
    assert record["legacy_status"] == "passed"
    assert record["dispatch_target"] == "local"
    assert record["metadata"] == {}
    assert record["log"].endswith("legacy_passed.log")

    persisted = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert persisted["status"] == "done"


def test_legacy_worker_job_without_dispatch_can_be_claimed(tmp_path):
    state = WebState(write_config(tmp_path))
    legacy_path = state.job_dir / "legacy_worker_queue.json"
    legacy_path.write_text(
        json.dumps(
            {
                "id": "legacy_worker_queue",
                "status": "queued",
                "metadata": {"worker_args": ["audit", "--input", "x"]},
            }
        ),
        encoding="utf-8",
    )

    claimed = claim_worker_job(state, "worker-legacy")
    assert claimed["id"] == "legacy_worker_queue"
    assert claimed["schema_version"] == 2
    assert claimed["dispatch_target"] == "worker"
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "worker-legacy"


def test_active_job_limits_count_user_and_global_records(tmp_path):
    config_path = write_config(tmp_path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["webui"]["max_active_jobs_per_user"] = 1
    data["webui"]["max_active_jobs_global"] = 2
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    state = WebState(config_path)

    create_job_record(
        state,
        "audit",
        "Audit 1",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    create_job_record(
        state,
        "audit",
        "Audit 2",
        ["python", "-m", "ecse_localizer", "audit"],
        user="other",
        metadata={},
        dispatch_target="worker",
    )

    assert active_job_counts(state, "admin") == {"user": 1, "global": 2}
    try:
        enforce_active_job_limits(state, "admin")
    except Exception as exc:
        assert "Active job limit" in str(exc)
    else:
        raise AssertionError("expected user active job limit")

    state.webui["max_active_jobs_per_user"] = 5
    try:
        enforce_active_job_limits(state, "new-user")
    except Exception as exc:
        assert "Global active job limit" in str(exc)
    else:
        raise AssertionError("expected global active job limit")

    update_job(state, list_jobs(state, "admin")[0]["id"], {"status": "done"})
    enforce_active_job_limits(state, "new-user")


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


def test_worker_status_changes_preserves_running_progress_and_metrics():
    changes = worker_status_changes(
        {
            "status": "running",
            "worker_id": "worker-1",
            "pid": 123,
            "progress": 42,
            "log_tail": "processed 4/10 segments",
            "metrics": {"cpu": {"percent": 55}},
            "command": ["python", "-m", "ecse_localizer", "--config", "<local-config>", "audit"],
        }
    )
    assert changes["status"] == "running"
    assert changes["worker_id"] == "worker-1"
    assert changes["progress"] == 42
    assert changes["log_tail"].endswith("segments")
    assert changes["metrics"]["cpu"]["percent"] == 55
    assert changes["command"][-1] == "audit"


def test_worker_progress_parser_handles_percent_and_fraction():
    assert extract_progress_from_text("overall progress: 37%") == 37
    assert extract_progress_from_text("processed segment 3/12") == 25
    assert extract_progress_from_text("nothing parseable") is None


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
