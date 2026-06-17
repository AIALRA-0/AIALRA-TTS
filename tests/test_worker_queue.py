import json
import os
import time
from types import SimpleNamespace
from pathlib import Path

import yaml

from ecse_localizer.webui import (
    WebState,
    active_job_counts,
    browser_upload_policy,
    build_job_command,
    claim_worker_job,
    command_with_config,
    create_job_record,
    enforce_active_job_limits,
    file_display_name,
    infer_job_type,
    list_jobs,
    require_worker_token,
    read_job,
    requeue_stale_worker_jobs,
    retry_job_record,
    save_worker_artifact_cache_upload,
    save_worker_preview_upload,
    soft_delete_job,
    update_job,
    upload_fits_quota,
    worker_args_from_command,
    worker_status_payload,
    worker_status_changes,
)
from ecse_localizer.worker_client import (
    canonical_json,
    collect_worker_media_refs,
    extract_progress_from_text,
    redacted_command,
    resolve_worker_media_args,
    summarize_result,
    worker_args,
    worker_headers,
    worker_signature,
)
from ecse_localizer.worker_client import find_registered_artifact, preview_source_path, register_worker_artifacts, upload_worker_artifact_cache, upload_worker_preview


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


def test_file_display_name_handles_windows_and_posix_paths():
    assert file_display_name(r"C:\worker-local\outputs\lecture_report.json") == "lecture_report.json"
    assert file_display_name("/srv/aialra/previews/lecture_report.json") == "lecture_report.json"


def test_repair_fidelity_worker_args_are_portable(tmp_path):
    state = WebState(write_config(tmp_path))
    command, title = build_job_command(
        "repair_fidelity",
        {
            "report": r"C:\worker-local\outputs\lecture_report.json",
            "fidelity_report": r"C:\worker-local\outputs\lecture_fidelity_report.json",
            "max_score": 2,
            "skip_high": True,
        },
        state,
        validate_paths=False,
    )

    assert title == "Fidelity repair: lecture_report.json"
    assert worker_args_from_command(command) == [
        "repair-fidelity",
        "--report",
        r"C:\worker-local\outputs\lecture_report.json",
        "--fidelity-report",
        r"C:\worker-local\outputs\lecture_fidelity_report.json",
        "--max-score",
        "2",
        "--skip-high",
    ]


def test_infer_job_type_recognizes_repair_fidelity_command():
    assert infer_job_type({"command": ["python", "-m", "ecse_localizer", "repair-fidelity", "--report", "x"]}) == "repair_fidelity"


def test_worker_status_payload_marks_missing_worker_unavailable(tmp_path):
    state = WebState(write_config(tmp_path))
    payload = worker_status_payload(state)
    assert payload["execution_mode"] == "worker_queue"
    assert payload["worker_required"] is True
    assert payload["heartbeat_online"] is False
    assert payload["available"] is False
    assert payload["status"] == "local"
    assert "queued jobs will wait" in payload["message"]


def test_worker_hmac_signature_is_accepted_without_static_token(tmp_path):
    state = WebState(write_config(tmp_path))
    state.webui["worker_auth_mode"] = "hmac"
    body_text = canonical_json({"worker_id": "worker-1"})
    body = body_text.encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {
        "x-worker-timestamp": timestamp,
        "x-worker-signature": worker_signature(
            "worker-token",
            timestamp=timestamp,
            method="POST",
            path="/api/worker/heartbeat",
            body=body,
        ),
    }

    require_worker_token(fake_worker_request(state, headers, path="/api/worker/heartbeat"), state, body)


def test_signed_worker_headers_do_not_send_plaintext_token():
    headers = worker_headers("worker-token", path="/api/worker/jobs/claim", body=b"{}")
    assert headers["X-Worker-Auth"] == "hmac-sha256"
    assert "X-Worker-Signature" in headers
    assert "X-Worker-Timestamp" in headers
    assert "X-Worker-Token" not in headers


def test_worker_hmac_mode_rejects_legacy_static_token(tmp_path):
    state = WebState(write_config(tmp_path))
    state.webui["worker_auth_mode"] = "hmac"
    try:
        require_worker_token(fake_worker_request(state, {"x-worker-token": "worker-token"}), state, b"{}")
    except Exception as exc:
        assert "HMAC signature is required" in str(exc)
    else:
        raise AssertionError("expected HMAC mode to reject legacy static token")


def test_upload_quota_counts_reserved_bytes_without_double_counting_current_file():
    assert upload_fits_quota(base_used_bytes=4, reserved_bytes=0, current_file_bytes=6, quota_bytes=10)
    assert upload_fits_quota(base_used_bytes=4, reserved_bytes=3, current_file_bytes=3, quota_bytes=10)
    assert not upload_fits_quota(base_used_bytes=4, reserved_bytes=3, current_file_bytes=4, quota_bytes=10)


def test_browser_upload_policy_disables_remote_worker_queue_by_default(tmp_path):
    config_path = write_config(tmp_path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["webui"]["execution_mode"] = "local_subprocess"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    state = WebState(config_path)
    assert browser_upload_policy(state)["enabled"] is True

    data["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    state = WebState(config_path)
    policy = browser_upload_policy(state)
    assert policy["enabled"] is False
    assert policy["mode"] == "disabled"
    assert "Windows worker" in policy["message"]

    state.webui["allow_remote_media_uploads"] = True
    assert browser_upload_policy(state)["enabled"] is True


def fake_worker_request(state: WebState, headers: dict[str, str], *, path: str = "/api/worker/jobs/claim"):
    return SimpleNamespace(
        headers=headers,
        method="POST",
        url=SimpleNamespace(path=path),
        app=SimpleNamespace(state=SimpleNamespace(web=state)),
    )


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


def test_stale_claimed_worker_job_is_requeued_and_claimable(tmp_path):
    state = WebState(write_config(tmp_path))
    state.webui["worker_job_heartbeat_timeout_seconds"] = 30
    record = create_job_record(
        state,
        "audit",
        "Audit input directory",
        ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "x"],
        user="admin",
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-dead"})
    make_job_file_stale(state, record["id"], seconds=120)

    changed = requeue_stale_worker_jobs(state)

    assert changed[0]["id"] == record["id"]
    assert changed[0]["status"] == "retrying"
    assert changed[0]["retry_count"] == 1
    assert changed[0]["last_claimed_by"] == "worker-dead"
    assert changed[0]["claimed_by"] is None
    claimed = claim_worker_job(state, "worker-new")
    assert claimed["id"] == record["id"]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "worker-new"


def test_fresh_running_worker_job_is_not_requeued(tmp_path):
    state = WebState(write_config(tmp_path))
    state.webui["worker_job_heartbeat_timeout_seconds"] = 300
    record = create_job_record(
        state,
        "audit",
        "Audit input directory",
        ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "x"],
        user="admin",
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "running", "claimed_by": "worker-1", "retry_count": 0})

    assert requeue_stale_worker_jobs(state) == []
    assert read_job(state, record["id"])["status"] == "running"


def test_stale_worker_job_fails_after_max_auto_retries(tmp_path):
    state = WebState(write_config(tmp_path))
    state.webui["worker_job_heartbeat_timeout_seconds"] = 30
    state.webui["worker_job_max_auto_retries"] = 1
    record = create_job_record(
        state,
        "audit",
        "Audit input directory",
        ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "x"],
        user="admin",
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "running", "claimed_by": "worker-1", "retry_count": 1})
    make_job_file_stale(state, record["id"], seconds=120)

    changed = requeue_stale_worker_jobs(state)

    assert changed[0]["status"] == "failed"
    assert changed[0]["returncode"] == -2
    assert "max auto retries" in changed[0]["error"]
    assert claim_worker_job(state, "worker-new") is None


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


def make_job_file_stale(state: WebState, job_id: str, *, seconds: int) -> None:
    path = state.job_dir / f"{job_id}.json"
    old_epoch = time.time() - seconds
    os.utime(path, (old_epoch, old_epoch))


def test_preview_source_prefers_video_output(tmp_path):
    source = tmp_path / "demo_zh_dub.mp4"
    assert preview_source_path({"video": str(source), "report": "demo_report.json"}) == source
    assert preview_source_path({"hard_sub": str(tmp_path / "demo_hardsub.mp4")}).name == "demo_hardsub.mp4"
    assert preview_source_path({"video": str(tmp_path / "demo_report.json")}) is None


def test_upload_worker_preview_sends_signed_binary_without_plaintext_token(monkeypatch, tmp_path):
    preview = tmp_path / "demo_preview.mp4"
    preview.write_bytes(b"small preview mp4")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "preview": {"id": "job_1_zh_dub_mp4"}}

    def fake_post(url, data, headers, timeout):
        captured.update({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return Response()

    monkeypatch.setattr("ecse_localizer.worker_client.requests.post", fake_post)
    result = upload_worker_preview(
        "https://remote.example",
        "worker-token",
        "job_1",
        preview,
        variant="preview",
        preview_id="job_1_zh_dub_mp4",
        display_name="demo_zh_dub.mp4",
        source_output_key="zh_dub_mp4",
        worker_id="worker-1",
    )

    assert result["ok"] is True
    assert captured["url"] == "https://remote.example/api/worker/jobs/job_1/preview"
    assert captured["data"] == b"small preview mp4"
    assert captured["headers"]["Content-Type"] == "video/mp4"
    assert captured["headers"]["X-Worker-Preview-Variant"] == "preview"
    assert captured["headers"]["X-Worker-Id"] == "worker-1"
    assert "X-Worker-Signature" in captured["headers"]
    assert "X-Worker-Token" not in captured["headers"]


def test_register_worker_artifacts_writes_local_registry_without_leaking_path(monkeypatch, tmp_path):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr("ecse_localizer.worker_client.artifact_registry_path", lambda: registry_path)
    output = tmp_path / "lecture_zh_dub.mp4"
    output.write_bytes(b"mp4")
    report_json = tmp_path / "lecture_report.json"
    report_json.write_text(json.dumps({"outputs": {"zh_dub_mp4": str(output)}}), encoding="utf-8")
    report_md = tmp_path / "lecture_report.md"
    report_md.write_text("report", encoding="utf-8")

    summaries = register_worker_artifacts({"report": str(report_md)}, {"id": "job-1"})

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["name"] == "lecture_zh_dub.mp4"
    assert summary["source_output_key"] == "zh_dub_mp4"
    assert "path" not in summary
    registered = find_registered_artifact(summary["ref_id"])
    assert registered["path"] == str(output)
    assert registry_path.exists()


def test_worker_media_refs_register_and_resolve_without_leaking_path(monkeypatch, tmp_path):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr("ecse_localizer.worker_client.artifact_registry_path", lambda: registry_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    video = media_root / "lecture.mp4"
    video.write_bytes(b"mp4")

    summaries = collect_worker_media_refs({"input_dir": str(media_root), "worker": {"max_media_refs": 10}})

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["name"] == "lecture.mp4"
    assert "path" not in summary
    resolved = resolve_worker_media_args(["process-one", "--video", f"worker-ref:{summary['ref_id']}"])
    assert resolved == ["process-one", "--video", str(video.resolve())]


def test_worker_media_refs_skip_generated_output_dirs(monkeypatch, tmp_path):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr("ecse_localizer.worker_client.artifact_registry_path", lambda: registry_path)
    source = tmp_path / "lecture.mp4"
    source.write_bytes(b"source")
    output_dir = tmp_path / "_localizer_output"
    output_dir.mkdir()
    generated = output_dir / "lecture_zh_dub.mp4"
    generated.write_bytes(b"generated")

    summaries = collect_worker_media_refs({"input_dir": str(tmp_path), "worker": {"max_media_refs": 10}})

    assert [row["name"] for row in summaries] == ["lecture.mp4"]


def test_upload_worker_artifact_cache_sends_signed_binary_without_plaintext_token(monkeypatch, tmp_path):
    artifact = tmp_path / "lecture_zh_dub.mp4"
    artifact.write_bytes(b"full mp4")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "artifact": {"id": "worker_artifact_ref1"}}

    def fake_post(url, data, headers, timeout):
        captured.update({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return Response()

    monkeypatch.setattr("ecse_localizer.worker_client.requests.post", fake_post)
    result = upload_worker_artifact_cache(
        "https://remote.example",
        "worker-token",
        "cache-job-1",
        artifact,
        artifact_id="worker_artifact_ref1",
        artifact_ref_id="ref1",
        display_name="lecture_zh_dub.mp4",
        source_output_key="zh_dub_mp4",
        worker_id="worker-1",
    )

    assert result["ok"] is True
    assert captured["url"] == "https://remote.example/api/worker/jobs/cache-job-1/artifact-cache"
    assert captured["data"] == b"full mp4"
    assert captured["headers"]["Content-Type"] == "video/mp4"
    assert captured["headers"]["X-Worker-Artifact-Id"] == "worker_artifact_ref1"
    assert captured["headers"]["X-Worker-Artifact-Ref"] == "ref1"
    assert "X-Worker-Signature" in captured["headers"]
    assert "X-Worker-Token" not in captured["headers"]


def test_save_worker_preview_upload_writes_manifest_without_source_path(tmp_path):
    state = WebState(write_config(tmp_path))
    record = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={"project_id": "course", "folder_id": "week_1"},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})
    record = read_job(state, record["id"])
    body = b"small preview mp4"

    row = save_worker_preview_upload(
        state,
        record,
        fake_worker_request(
            state,
            {
                "x-worker-id": "worker-1",
                "x-worker-preview-variant": "preview",
                "x-worker-preview-id": "preview-1",
                "x-worker-preview-name": "lecture_zh_dub.mp4",
                "x-worker-preview-file-name": "lecture_preview.mp4",
                "x-worker-preview-source-key": "zh_dub_mp4",
            },
        ),
        body,
    )

    assert row["id"] == "preview-1"
    assert row["owner"] == "admin"
    assert row["project_id"] == "course"
    assert row["folder_id"] == "week_1"
    assert row["preview_path"].endswith("lecture_preview.mp4")
    assert Path(row["preview_path"]).read_bytes() == body
    manifest = json.loads((Path(state.config["output_dir"]) / "previews" / "preview_manifest.json").read_text(encoding="utf-8"))
    assert manifest["previews"][0]["display_path"] == "preview cache: lecture_preview.mp4"
    assert "source_path" not in manifest["previews"][0]
    assert "private" not in json.dumps(manifest, ensure_ascii=False)
    assert state.store.quota_status("admin")["remote_used_bytes"] >= len(body)


def test_save_worker_artifact_cache_upload_writes_downloadable_manifest(tmp_path):
    state = WebState(write_config(tmp_path))
    record = create_job_record(
        state,
        "cache_artifact",
        "Cache artifact",
        ["worker-action", "upload-artifact-cache", "ref1"],
        user="admin",
        metadata={
            "worker_action": "upload_artifact_cache",
            "artifact_id": "worker_artifact_ref1",
            "artifact_ref_id": "ref1",
            "artifact_name": "lecture_zh_dub.mp4",
            "source_job_id": "source-job",
            "source_output_key": "zh_dub_mp4",
            "project_id": "course",
            "folder_id": "week_1",
        },
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})
    record = read_job(state, record["id"])

    row = save_worker_artifact_cache_upload(
        state,
        record,
        fake_worker_request(
            state,
            {
                "x-worker-id": "worker-1",
                "x-worker-artifact-id": "worker_artifact_ref1",
                "x-worker-artifact-ref": "ref1",
                "x-worker-artifact-name": "lecture_zh_dub.mp4",
                "x-worker-artifact-file-name": "lecture_zh_dub.mp4",
                "x-worker-artifact-source-key": "zh_dub_mp4",
            },
            path=f"/api/worker/jobs/{record['id']}/artifact-cache",
        ),
        b"full mp4",
    )

    assert row["id"] == "worker_artifact_ref1"
    assert row["remote_cache"] is True
    assert row["preview_path"].endswith("lecture_zh_dub.mp4")
    assert Path(row["preview_path"]).read_bytes() == b"full mp4"
    manifest = json.loads((Path(state.config["output_dir"]) / "previews" / "preview_manifest.json").read_text(encoding="utf-8"))
    assert manifest["previews"][0]["display_path"] == "remote cache: lecture_zh_dub.mp4"
    assert "source_path" not in manifest["previews"][0]


def test_save_worker_preview_upload_enforces_quota_without_testclient(tmp_path):
    config_path = write_config(tmp_path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["webui"]["default_remote_quota_gb"] = 0.00000001
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    state = WebState(config_path)
    record = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )

    try:
        save_worker_preview_upload(
            state,
            record,
            fake_worker_request(
                state,
                {
                    "x-worker-id": "worker-1",
                    "x-worker-preview-variant": "preview",
                    "x-worker-preview-file-name": "too_big_preview.mp4",
                },
            ),
            b"x" * 100,
        )
    except Exception as exc:
        assert "Remote quota exceeded" in str(exc)
    else:
        raise AssertionError("expected worker preview upload to enforce remote quota")
