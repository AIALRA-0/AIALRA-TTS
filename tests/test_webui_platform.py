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

from ecse_localizer.webui import create_app


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

    response = client.post(
        "/api/jobs",
        json={
            "type": "audit",
            "project_id": project["id"],
            "folder_id": folder["id"],
            "source_language": "auto",
            "target_subtitle_language": "zh-CN",
            "target_tts_language": "zh-CN",
        },
    )
    assert response.status_code == 200
    assert response.json()["job"]["metadata"]["folder_id"] == folder["id"]

    response = client.get("/api/quota")
    assert response.status_code == 200
    assert response.json()["local_quota_bytes"] > 0
