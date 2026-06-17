from pathlib import Path

import yaml
import pytest

try:
    from fastapi.testclient import TestClient
except RuntimeError as exc:  # Starlette may require optional httpx2.
    TestClient = None
    TESTCLIENT_IMPORT_ERROR = exc
else:
    TESTCLIENT_IMPORT_ERROR = None

from ecse_localizer.webui import create_app, create_job_record, update_job


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
        "translation": {"max_zh_chars_per_subtitle_line": 22},
        "webui": {
            "username": "admin",
            "password": "local-password",
            "session_secret": "unit-test-secret",
            "platform_dir": str(tmp_path / "platform"),
            "upload_dir": str(tmp_path / "uploads"),
            "job_dir": str(tmp_path / "jobs"),
            "worker_token": "worker-token",
            "max_upload_mb": 1,
            "default_local_quota_gb": 1,
            "default_remote_quota_gb": 1,
            "default_project_quota_gb": 2,
        },
    }
    Path(config["input_dir"]).mkdir()
    Path(config["output_dir"]).mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


def test_webui_login_project_and_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.get("/api/projects")
    assert response.status_code == 200
    assert response.json()["projects"]

    response = client.post("/api/projects", json={"name": "Course Project", "description": "test", "quota_project_gb": 4})
    assert response.status_code == 200
    project = response.json()["project"]
    assert project["name"] == "Course Project"
    assert project["quota_project_bytes"] == 4 * 1024 * 1024 * 1024

    response = client.post(f"/api/projects/{project['id']}/folders", json={"name": "Week 1"})
    assert response.status_code == 200
    folder = response.json()["folder"]
    assert folder["name"] == "Week 1"

    response = client.get("/api/templates")
    assert response.status_code == 200
    assert response.json()["templates"]

    response = client.post(
        "/api/templates",
        json={
            "name": "Japanese Template",
            "params": {
                "source_language": "auto",
                "target_subtitle_language": "ja",
                "target_tts_language": "ja",
                "quality_mode": "balanced",
                "tts_speed": 1.1,
                "mux_hard_subtitle": False,
                "max_subtitle_line_chars": 18,
            },
        },
    )
    assert response.status_code == 200
    template = response.json()["template"]

    response = client.post(
        "/api/jobs",
        json={
            "type": "audit",
            "project_id": project["id"],
            "folder_id": folder["id"],
            "template_id": template["id"],
        },
    )
    assert response.status_code == 200
    metadata = response.json()["job"]["metadata"]
    assert metadata["folder_id"] == folder["id"]
    assert metadata["template_id"] == template["id"]
    assert metadata["target_subtitle_language"] == "ja"
    assert metadata["tts_speed"] == 1.1
    assert metadata["mux_hard_subtitle"] is False
    assert metadata["max_subtitle_line_chars"] == 18

    response = client.get("/api/quota")
    assert response.status_code == 200
    assert response.json()["local_quota_bytes"] > 0

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    caps = response.json()["capabilities"]
    assert "asr" in caps
    assert "translation" in caps
    assert "tts" in caps


def test_webui_admin_can_update_user_quota_and_disable(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    admin = TestClient(app)

    response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = admin.post(
        "/api/users",
        json={"username": "student.one", "password": "long-enough-password", "quota_local_gb": 2, "quota_remote_gb": 3},
    )
    assert response.status_code == 200

    response = admin.patch(
        "/api/users/student.one",
        json={"role": "user", "disabled": True, "quota_local_gb": 4, "quota_remote_gb": 5},
    )
    assert response.status_code == 200
    user = response.json()["user"]
    assert user["disabled"] is True
    assert user["quota_local_bytes"] == 4 * 1024 * 1024 * 1024
    assert user["quota_remote_bytes"] == 5 * 1024 * 1024 * 1024

    student = TestClient(app)
    response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 401

    response = admin.patch("/api/users/admin", json={"disabled": True})
    assert response.status_code == 400

    response = admin.patch("/api/users/admin", json={"role": "user"})
    assert response.status_code == 400


def test_worker_queue_submit_reports_waiting_worker(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["worker_token"] = "worker-token"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    worker = response.json()["worker"]
    assert worker["execution_mode"] == "worker_queue"
    assert worker["available"] is False

    project = client.get("/api/projects").json()["projects"][0]
    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "folder_id": "root"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["dispatch"]["target"] == "worker"
    assert payload["dispatch"]["worker"]["available"] is False
    assert payload["job"]["queued_for_worker"] is True
    assert payload["job"]["metadata"]["worker_status_at_submit"]["available"] is False
    assert payload["job"]["metadata"]["worker_args"][0] == "audit"


def test_worker_log_endpoint_returns_remote_log_tail_when_file_missing(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    record = create_job_record(
        state,
        "audit",
        "Remote audit",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    update_job(
        state,
        record["id"],
        {
            "log": str(tmp_path / "missing-worker.log"),
            "log_tail": "line one\nprocessed segment 3/12\nline three",
            "progress": 25,
        },
    )

    response = client.get(f"/api/jobs/{record['id']}/log")
    assert response.status_code == 200
    assert "processed segment 3/12" in response.text


def test_worker_status_update_refreshes_heartbeat_and_job_progress(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    record = create_job_record(
        state,
        "audit",
        "Remote audit",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    response = client.post(
        f"/api/worker/jobs/{record['id']}/status",
        headers={"x-worker-token": "worker-token"},
        json={
            "status": "running",
            "worker_id": "worker-1",
            "progress": 33,
            "log_tail": "overall progress: 33%",
            "metrics": {"gpu": [{"available": True, "util_percent": 50}]},
        },
    )
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "running"
    assert job["progress"] == 33
    assert job["log_tail"] == "overall progress: 33%"

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    worker = response.json()["worker"]
    assert worker["status"] == "online"
    assert worker["worker_id"] == "worker-1"
    assert worker["heartbeat_online"] is True
