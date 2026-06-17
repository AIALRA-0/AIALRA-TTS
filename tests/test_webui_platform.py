import json
from io import BytesIO
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

from ecse_localizer.artifacts import artifact_catalog
from ecse_localizer.platform_store import safe_worker_id
from ecse_localizer.utils import read_json
from ecse_localizer.webui import create_app, create_job_record, fields_from_config, list_all_video_records, read_job, resolve_video_reference, update_job
from ecse_localizer.worker_client import worker_headers


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


def test_tuning_fields_include_tts_slot_trim_controls():
    fields = {field["path"]: field for field in fields_from_config({"tts": {"trim_overlong_audio_to_slot": True}})}

    assert fields["tts.trim_overlong_audio_to_slot"]["type"] == "bool"
    assert fields["tts.trim_overlong_audio_to_slot"]["value"] is True
    assert fields["tts.shrink_delayed_slots_to_original_timeline"]["type"] == "bool"
    assert fields["tts.compact_distributed_max_gap_seconds"]["type"] == "float"
    assert fields["tts.slot_trim_tolerance_seconds"]["type"] == "float"
    assert fields["tts.slot_trim_fade_seconds"]["type"] == "float"


def test_static_ui_does_not_expose_raw_worker_path_placeholder():
    html = (Path(__file__).parents[1] / "src" / "ecse_localizer" / "static" / "index.html").read_text(encoding="utf-8")

    assert r"D:\worker-media" not in html
    assert "jobWorkerVideoPath" in html


def test_static_settings_ui_is_admin_only():
    html = (Path(__file__).parents[1] / "src" / "ecse_localizer" / "static" / "index.html").read_text(encoding="utf-8")

    assert '<button class="tab" data-tab="settings" data-admin-only>' in html
    assert '<section id="settingsTab" class="tab-panel" data-admin-only>' in html


def test_static_template_controls_have_save_update_and_delete_actions():
    static_root = Path(__file__).parents[1] / "src" / "ecse_localizer" / "static"
    html = (static_root / "index.html").read_text(encoding="utf-8")
    js = (static_root / "app.js").read_text(encoding="utf-8")

    for button_id in ["saveTemplateBtn", "updateTemplateBtn", "deleteTemplateBtn"]:
        assert f'id="{button_id}"' in html
        assert '$("' + button_id + '").addEventListener("click"' in js
    assert 'id="jobShrinkDelayedSlots"' in html
    assert 'id="jobCompactMaxGap"' in html
    assert "tts_shrink_delayed_slots_to_original_timeline" in js
    assert "tts_compact_max_gap_seconds" in js
    assert "async function deleteCurrentTemplate()" in js


def test_static_project_controls_have_update_actions():
    static_root = Path(__file__).parents[1] / "src" / "ecse_localizer" / "static"
    html = (static_root / "index.html").read_text(encoding="utf-8")
    js = (static_root / "app.js").read_text(encoding="utf-8")

    assert 'id="showArchivedProjects"' in html
    assert "保存项目" in js
    assert "保存文件夹" in js
    assert "恢复项目" in js
    assert "恢复文件夹" in js
    assert "async function saveProjectSettings(projectId)" in js
    assert "async function saveFolderSettings(projectId, folderId)" in js
    assert "async function restoreProject(project)" in js
    assert "async function restoreFolder(project, folder)" in js
    assert 'method: "PATCH"' in js


def test_static_job_history_has_deleted_filter_and_restore_action():
    static_root = Path(__file__).parents[1] / "src" / "ecse_localizer" / "static"
    html = (static_root / "index.html").read_text(encoding="utf-8")
    js = (static_root / "app.js").read_text(encoding="utf-8")

    assert '<option value="deleted">deleted</option>' in html
    assert 'params.set("include_deleted", "true")' in js
    assert "查看产物" in js
    assert "恢复记录" in js
    assert "function viewJobArtifacts(job)" in js
    assert "async function restoreJob(jobId)" in js
    assert '`/api/jobs/${encodeURIComponent(jobId)}/restore`' in js
    assert "delete_files=true" in js
    assert "同时删除生成文件和远端缓存" in js


def test_static_artifact_history_has_scope_filters():
    static_root = Path(__file__).parents[1] / "src" / "ecse_localizer" / "static"
    html = (static_root / "index.html").read_text(encoding="utf-8")
    js = (static_root / "app.js").read_text(encoding="utf-8")

    for element_id in ["artifactFilterProject", "artifactFilterFolder", "artifactFilterJob", "artifactFilterKind"]:
        assert f'id="{element_id}"' in html
    assert "function artifactFilterQuery()" in js
    for field in ["project_id", "folder_id", "job_id", "kind"]:
        assert f'params.set("{field}"' in js
    assert "renderArtifactFilterOptions()" in js


def test_webui_login_project_and_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    assert "; secure" not in response.headers["set-cookie"].lower()

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

    response = client.patch(
        f"/api/projects/{project['id']}",
        json={"name": "Course Project Updated", "description": "edited", "quota_project_gb": 3},
    )
    assert response.status_code == 200
    updated = response.json()["project"]
    assert updated["name"] == "Course Project Updated"
    assert updated["description"] == "edited"
    assert updated["quota_project_bytes"] == 3 * 1024 * 1024 * 1024

    response = client.patch(f"/api/projects/{project['id']}/folders/{folder['id']}", json={"name": "Week One"})
    assert response.status_code == 200
    assert response.json()["folder"]["name"] == "Week One"
    refreshed_project = next(item for item in response.json()["projects"] if item["id"] == project["id"])
    assert any(item["id"] == folder["id"] and item["name"] == "Week One" for item in refreshed_project["folders"])

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


def test_template_update_api_applies_to_worker_job_metadata(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    response = client.post(
        "/api/templates",
        json={"name": "Reusable", "params": {"quality_mode": "fast", "tts_speed": 0.95}},
    )
    assert response.status_code == 200
    template = response.json()["template"]

    response = client.patch(
        f"/api/templates/{template['id']}",
        json={
            "name": "Reusable tuned",
            "params": {
                "quality_mode": "best_quality",
                "tts_speed": "1.15",
                "tts_compact_max_gap_seconds": "1.5",
                "tts_shrink_delayed_slots_to_original_timeline": "false",
                "unknown_secret": "drop",
            },
        },
    )

    assert response.status_code == 200
    updated = response.json()["template"]
    assert updated["name"] == "Reusable tuned"
    assert updated["params"]["quality_mode"] == "best_quality"
    assert updated["params"]["tts_speed"] == 1.15
    assert updated["params"]["tts_compact_max_gap_seconds"] == 1.5
    assert updated["params"]["tts_shrink_delayed_slots_to_original_timeline"] is False
    assert "unknown_secret" not in updated["params"]

    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "template_id": template["id"]})

    assert response.status_code == 200
    metadata = response.json()["job"]["metadata"]
    assert metadata["template_id"] == template["id"]
    assert metadata["quality_mode"] == "best_quality"
    assert metadata["tts_speed"] == 1.15
    assert metadata["tts_compact_max_gap_seconds"] == 1.5
    assert metadata["tts_shrink_delayed_slots_to_original_timeline"] is False


def test_template_delete_api_removes_template_from_job_options(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    response = client.post("/api/templates", json={"name": "Delete Me", "params": {"quality_mode": "fast"}})
    assert response.status_code == 200
    template = response.json()["template"]

    response = client.delete(f"/api/templates/{template['id']}")

    assert response.status_code == 200
    assert all(item["id"] != template["id"] for item in response.json()["templates"])
    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "template_id": template["id"]})
    assert response.status_code == 400
    assert "Template not found" in response.text


def test_project_and_folder_archive_api_hides_targets(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    default_project = client.get("/api/projects").json()["projects"][0]

    response = client.post("/api/projects", json={"name": "Archive Me", "quota_project_gb": 4})
    assert response.status_code == 200
    project = response.json()["project"]
    response = client.post(f"/api/projects/{project['id']}/folders", json={"name": "Week 1"})
    assert response.status_code == 200
    folder = response.json()["folder"]

    response = client.delete(f"/api/projects/{project['id']}/folders/{folder['id']}")

    assert response.status_code == 200
    active_project = next(item for item in response.json()["projects"] if item["id"] == project["id"])
    assert all(item["id"] != folder["id"] for item in active_project["folders"])
    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "folder_id": folder["id"]})
    assert response.status_code == 400
    assert "Folder not found" in response.text

    response = client.delete(f"/api/projects/{project['id']}")

    assert response.status_code == 200
    assert all(item["id"] != project["id"] for item in response.json()["projects"])
    response = client.get("/api/projects?include_archived=true")
    assert response.status_code == 200
    archived_project = next(item for item in response.json()["projects"] if item["id"] == project["id"])
    assert archived_project["archived_at"]
    assert any(item["id"] == folder["id"] and item["archived_at"] for item in archived_project["folders"])
    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "folder_id": "root"})
    assert response.status_code == 400
    assert "Project not found" in response.text

    response = client.delete(f"/api/projects/{default_project['id']}")
    assert response.status_code == 400
    assert "active project" in response.text

    response = client.post(f"/api/projects/{project['id']}/restore", json={})
    assert response.status_code == 200
    assert any(item["id"] == project["id"] and not item.get("archived_at") for item in response.json()["projects"])
    response = client.post(f"/api/projects/{project['id']}/folders/{folder['id']}/restore", json={})
    assert response.status_code == 200
    restored_project = next(item for item in response.json()["projects"] if item["id"] == project["id"])
    assert any(item["id"] == folder["id"] and not item.get("archived_at") for item in restored_project["folders"])
    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "folder_id": folder["id"]})
    assert response.status_code == 200


def test_webui_secure_session_cookie_when_configured(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["cookie_secure"] = True
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "; secure" in cookie


def test_webui_csrf_origin_check_blocks_cross_site_mutations(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["csrf_origin_check"] = True
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app, base_url="https://localizer.example.com")

    response = client.post(
        "/api/login",
        json={"username": "admin", "password": "local-password"},
        headers={"origin": "https://localizer.example.com"},
    )
    assert response.status_code == 200

    response = client.post("/api/projects", json={"name": "No Origin"})
    assert response.status_code == 403

    response = client.post("/api/projects", json={"name": "Cross Site"}, headers={"origin": "https://evil.example"})
    assert response.status_code == 403

    response = client.post("/api/projects", json={"name": "Same Site"}, headers={"origin": "https://localizer.example.com"})
    assert response.status_code == 200


def test_webui_csrf_origin_check_exempts_signed_worker_api(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["csrf_origin_check"] = True
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/worker/heartbeat", headers={"x-worker-token": "worker-token"}, json={"worker_id": "worker-1"})

    assert response.status_code == 200
    assert response.json()["worker"]["worker_id"] == "worker-1"


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


def test_cleanup_endpoint_is_admin_only(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    app = create_app(write_config(tmp_path))
    admin = TestClient(app)

    response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = admin.post(
        "/api/users",
        json={"username": "student.one", "password": "long-enough-password", "quota_local_gb": 1, "quota_remote_gb": 1},
    )
    assert response.status_code == 200

    student = TestClient(app)
    response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 200

    response = student.post("/api/cleanup", json={"dry_run": False, "older_than_days": 7})
    assert response.status_code == 403

    response = admin.post("/api/cleanup", json={"dry_run": True, "older_than_days": 7})
    assert response.status_code == 200
    cleanup = response.json()["cleanup"]
    assert cleanup["dry_run"] is True
    assert "items" in cleanup


def test_global_settings_and_raw_config_are_admin_only(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")

    with TestClient(app) as student:
        response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
        assert response.status_code == 200
        response = student.get("/api/tuning")
        assert response.status_code == 403
        response = student.post("/api/tuning", json={"values": {"tts.cosyvoice_gain": 2.0}})
        assert response.status_code == 403
        response = student.get("/api/config/raw")
        assert response.status_code == 403
        assert "worker-token" not in response.text
        response = student.post("/api/config/raw", json={"yaml": "privacy:\n  allow_cloud_api: true\n"})
        assert response.status_code == 403

    with TestClient(app) as admin:
        response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
        assert response.status_code == 200
        response = admin.get("/api/tuning")
        assert response.status_code == 200
        assert response.json()["fields"]
        response = admin.get("/api/config/raw")
        assert response.status_code == 200
        assert "worker-token" in response.text


def test_upload_enforces_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["default_remote_quota_gb"] = 0.000001
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.post("/api/upload", files=[("files", ("small.mp4", BytesIO(b"x" * 900), "video/mp4"))])
    assert response.status_code == 200
    assert response.json()["quota"]["remote_used_bytes"] == 900

    response = client.post("/api/upload", files=[("files", ("too_big.mp4", BytesIO(b"x" * 300), "video/mp4"))])
    assert response.status_code == 413
    assert "Remote quota exceeded" in response.text


def test_upload_failure_rolls_back_files_saved_earlier_in_batch(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["default_remote_quota_gb"] = 0.000001
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.post(
        "/api/upload",
        files=[
            ("files", ("first.mp4", BytesIO(b"x" * 600), "video/mp4")),
            ("files", ("second.mp4", BytesIO(b"x" * 600), "video/mp4")),
        ],
    )

    assert response.status_code == 413
    assert "Remote quota exceeded" in response.text
    assert not list(state.store.user_upload_dir("admin").glob("*"))
    assert client.get("/api/quota").json()["remote_used_bytes"] == 0


def test_upload_enforces_global_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["default_remote_quota_gb"] = 1
    config["webui"]["global_remote_quota_gb"] = 0.000001
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.post("/api/upload", files=[("files", ("small.mp4", BytesIO(b"x" * 900), "video/mp4"))])
    assert response.status_code == 200
    quota = response.json()["quota"]
    assert quota["remote_global_used_bytes"] == 900
    assert quota["remote_global_quota_bytes"] > 900

    response = client.post("/api/upload", files=[("files", ("too_big.mp4", BytesIO(b"x" * 300), "video/mp4"))])
    assert response.status_code == 413
    assert "Global remote quota exceeded" in response.text


def test_upload_response_uses_video_refs_for_non_admin(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    raw_upload_dir = str(state.store.user_upload_dir("student.one"))

    client = TestClient(app)
    response = client.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 200

    response = client.post("/api/upload", files=[("files", ("private.mp4", BytesIO(b"mp4"), "video/mp4"))])
    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert raw_upload_dir not in rendered
    saved = payload["saved"][0]
    assert saved["path"].startswith("video-ref:")
    assert saved["local_video_ref"] is True
    assert saved["display_path"] == "uploaded media: private.mp4"
    assert Path(resolve_video_reference(state, "student.one", saved["path"])).exists()

    project = client.get("/api/projects").json()["projects"][0]
    response = client.post(
        "/api/jobs",
        json={"type": "process_one", "video": saved["path"], "project_id": project["id"], "folder_id": "root"},
    )
    assert response.status_code == 200
    assert raw_upload_dir not in json.dumps(response.json(), ensure_ascii=False)


def test_upload_disabled_by_default_for_worker_queue(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    policy = response.json()["upload_policy"]
    assert policy["enabled"] is False
    assert policy["execution_mode"] == "worker_queue"
    assert policy["allow_worker_path_submission"] is False
    assert policy["worker_ref_required"] is True
    assert "worker-ref" in policy["worker_path_message"]

    response = client.post("/api/upload", files=[("files", ("lecture.mp4", BytesIO(b"x"), "video/mp4"))])
    assert response.status_code == 403
    assert "Windows worker" in response.text


def test_worker_path_submission_policy_is_opt_in(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["allow_worker_path_submission"] = True
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    policy = response.json()["upload_policy"]
    assert policy["execution_mode"] == "worker_queue"
    assert policy["allow_worker_path_submission"] is True
    assert policy["worker_ref_required"] is False
    assert "private trusted deployment" in policy["worker_path_message"]


def test_jobs_endpoint_filters_by_project_folder_and_status(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200

    project = state.store.create_project("admin", "Course")
    folder = state.store.create_folder("admin", project["id"], "Week 1")
    other_project = state.store.create_project("admin", "Other")
    first = create_job_record(
        state,
        "audit",
        "Audit course",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={"project_id": project["id"], "folder_id": folder["id"]},
        dispatch_target="worker",
    )
    second = create_job_record(
        state,
        "audit",
        "Audit other",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={"project_id": other_project["id"], "folder_id": "root"},
        dispatch_target="worker",
    )
    update_job(state, second["id"], {"status": "running"})

    response = client.get(f"/api/jobs?project_id={project['id']}&folder_id={folder['id']}")
    assert response.status_code == 200
    assert [job["id"] for job in response.json()["jobs"]] == [first["id"]]

    response = client.get("/api/jobs?status=running")
    assert response.status_code == 200
    assert [job["id"] for job in response.json()["jobs"]] == [second["id"]]


def test_deleted_job_can_be_filtered_and_restored(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    record = create_job_record(
        state,
        "audit",
        "Audit course",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "done", "returncode": 0})

    response = client.delete(f"/api/jobs/{record['id']}")
    assert response.status_code == 200
    assert response.json()["job"]["status"] == "deleted"

    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert record["id"] not in {job["id"] for job in response.json()["jobs"]}

    response = client.get("/api/jobs?status=deleted")
    assert response.status_code == 200
    assert [job["id"] for job in response.json()["jobs"]] == [record["id"]]

    response = client.post(f"/api/jobs/{record['id']}/restore", json={})
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "done"
    assert job["restored_by"] == "admin"

    response = client.get("/api/jobs")
    assert response.status_code == 200
    assert record["id"] in {job["id"] for job in response.json()["jobs"]}


def test_deleted_job_artifacts_hide_from_normal_page_but_remain_job_scoped(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    output = Path(state.config["output_dir"])
    video = output / "lecture_zh_dub.mp4"
    video.write_bytes(b"mp4")
    report = output / "lecture_report.json"
    report.write_text(json.dumps({"name": "lecture", "outputs": {"zh_dub_mp4": str(video)}}), encoding="utf-8")
    record = create_job_record(
        state,
        "process_one",
        "Lecture",
        ["python", "-m", "ecse_localizer", "process-one"],
        user="admin",
        metadata={"project_id": "course", "folder_id": "root"},
        dispatch_target="worker",
    )
    update_job(
        state,
        record["id"],
        {
            "status": "done",
            "returncode": 0,
            "result_report": str(report),
            "result_video": str(video),
            "worker_artifacts": [{"ref_id": "ref_deleted", "source_output_key": "zh_dub_mp4", "name": "lecture_zh_dub.mp4", "size": 100}],
        },
    )

    response = client.get("/api/artifacts")
    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert "lecture_zh_dub.mp4" in {item["name"] for item in artifacts}
    download_url = next(item["download_url"] for item in artifacts if item["name"] == "lecture_zh_dub.mp4")

    response = client.delete(f"/api/jobs/{record['id']}")
    assert response.status_code == 200
    assert video.exists()

    response = client.get("/api/artifacts")
    assert response.status_code == 200
    assert "lecture_zh_dub.mp4" not in {item["name"] for item in response.json()["artifacts"]}
    response = client.get(download_url)
    assert response.status_code == 404

    response = client.get(f"/api/jobs/{record['id']}/artifacts")
    assert response.status_code == 200
    deleted_artifacts = response.json()["artifacts"]
    deleted_video = next(item for item in deleted_artifacts if item["name"] == "lecture_zh_dub.mp4")
    assert "download_url" not in deleted_video
    assert deleted_video["download_disabled_reason"] == "source_job_deleted"
    deleted_worker_ref = next(item for item in deleted_artifacts if item["id"] == "worker_artifact_ref_deleted")
    assert "request_cache_url" not in deleted_worker_ref
    response = client.post("/api/artifacts/worker_artifact_ref_deleted/request-cache", json={})
    assert response.status_code == 404
    assert not any(job["type"] == "cache_artifact" for job in client.get("/api/jobs?include_deleted=true").json()["jobs"])


def test_delete_job_can_delete_files_and_release_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    output = Path(state.config["output_dir"])
    video = output / "lecture_zh_dub.mp4"
    video.write_bytes(b"mp4")
    report = output / "lecture_report.json"
    report_md = output / "lecture_report.md"
    report.write_text(json.dumps({"name": "lecture", "outputs": {"zh_dub_mp4": str(video)}}), encoding="utf-8")
    report_md.write_text("report", encoding="utf-8")
    preview_dir = output / "previews"
    preview_dir.mkdir()
    preview = preview_dir / "lecture_preview.mp4"
    thumbnail = preview_dir / "lecture_thumb.jpg"
    preview.write_bytes(b"preview")
    thumbnail.write_bytes(b"thumb")
    manifest = preview_dir / "preview_manifest.json"
    record = create_job_record(
        state,
        "process_one",
        "Lecture",
        ["python", "-m", "ecse_localizer", "process-one"],
        user="admin",
        metadata={"project_id": "course", "folder_id": "root"},
        dispatch_target="worker",
    )
    manifest.write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "id": "preview-1",
                        "owner": "admin",
                        "job_id": record["id"],
                        "preview_path": str(preview),
                        "thumbnail_path": str(thumbnail),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    update_job(state, record["id"], {"status": "done", "returncode": 0, "result_report": str(report), "result_video": str(video)})

    assert client.get("/api/quota").json()["remote_used_bytes"] == len(b"previewthumb")
    response = client.delete(f"/api/jobs/{record['id']}?delete_files=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["status"] == "deleted"
    assert payload["job"]["files_deleted_bytes"] >= len(b"mp4previewthumb")
    assert payload["deleted_files"]["bytes"] >= len(b"mp4previewthumb")
    assert payload["quota"]["remote_used_bytes"] == 0
    assert not video.exists()
    assert not report.exists()
    assert not report_md.exists()
    assert not preview.exists()
    assert not thumbnail.exists()
    assert read_json(manifest)["previews"] == []
    saved_job = read_job(state, record["id"])
    assert saved_job and saved_job["files_deleted_by"] == "admin"


def test_ownerless_legacy_jobs_are_admin_only(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    log_path = state.job_dir / "legacy_ownerless.log"
    log_path.write_text("legacy private log", encoding="utf-8")
    (state.job_dir / "legacy_ownerless.json").write_text(
        json.dumps(
            {
                "id": "legacy_ownerless",
                "type": "process_one",
                "title": "Legacy Ownerless Job",
                "status": "done",
                "log": str(log_path),
                "command": ["python", "-m", "ecse_localizer", "process-one"],
            }
        ),
        encoding="utf-8",
    )

    with TestClient(app) as student:
        response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
        assert response.status_code == 200
        response = student.get("/api/jobs")
        assert response.status_code == 200
        assert "legacy_ownerless" not in {job["id"] for job in response.json()["jobs"]}
        assert student.get("/api/jobs/legacy_ownerless").status_code == 404
        assert student.get("/api/jobs/legacy_ownerless/log").status_code == 404

    with TestClient(app) as admin:
        response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
        assert response.status_code == 200
        response = admin.get("/api/jobs")
        assert response.status_code == 200
        assert "legacy_ownerless" in {job["id"] for job in response.json()["jobs"]}
        response = admin.get("/api/jobs/legacy_ownerless/log")
        assert response.status_code == 200
        assert "legacy private log" in response.text


def test_non_admin_job_log_redacts_local_paths_and_secrets(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    win_root = "C:" + "\\Users\\Alice\\Desktop\\ECSE 4961"
    log_path = state.job_dir / "student_job.log"
    log_path.write_text(
        "\n".join(
            [
                f"processing {win_root}\\lecture.mp4",
                "worker_token=supersecret",
                "Authorization: " + "Bearer " + "secretbearertoken",
            ]
        ),
        encoding="utf-8",
    )
    record = create_job_record(
        state,
        "process_one",
        "Student job",
        ["python", "-m", "ecse_localizer", "process-one"],
        user="student.one",
        metadata={},
        dispatch_target="local",
    )
    update_job(state, record["id"], {"status": "running", "log": str(log_path)})

    with TestClient(app) as student:
        response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
        assert response.status_code == 200
        response = student.get(f"/api/jobs/{record['id']}/log")
        assert response.status_code == 200
        assert "<local-path>" in response.text
        assert "supersecret" not in response.text
        assert "secretbearertoken" not in response.text
        assert "Alice" not in response.text

    with TestClient(app) as admin:
        response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
        assert response.status_code == 200
        response = admin.get(f"/api/jobs/{record['id']}/log")
        assert response.status_code == 200
        assert f"{win_root}\\lecture.mp4" in response.text
        assert "supersecret" in response.text


def test_reports_and_dashboard_are_user_scoped(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    state.store.create_user("student.two", "another-long-password")
    output = Path(state.config["output_dir"])
    one_video = output / "one_source.mp4"
    one_dub = output / "one_zh_dub.mp4"
    one_video.write_bytes(b"source")
    one_dub.write_bytes(b"dub")
    one_report = output / "one_report.json"
    (output / "one_report.json").write_text(
        json.dumps(
            {
                "name": "one",
                "user": "student.one",
                "source_video": str(one_video),
                "outputs": {"zh_dub_mp4": str(one_dub)},
                "qa": {"pass": True, "issues": []},
            }
        ),
        encoding="utf-8",
    )
    (output / "two_report.json").write_text(
        json.dumps({"name": "two", "user": "student.two", "qa": {"pass": True, "issues": []}}),
        encoding="utf-8",
    )
    (output / "legacy_report.json").write_text(
        json.dumps({"name": "legacy", "qa": {"pass": False, "issues": ["ownerless"]}}),
        encoding="utf-8",
    )

    with TestClient(app) as student_one:
        response = student_one.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
        assert response.status_code == 200
        response = student_one.get("/api/reports")
        assert response.status_code == 200
        reports = response.json()["reports"]
        assert {report["name"] for report in reports} == {"one"}
        report = reports[0]
        rendered_reports = json.dumps(reports, ensure_ascii=False)
        assert report["path"].startswith("report-ref:")
        assert report["report_ref"] is True
        assert report["display_path"] == "report: one_report.json"
        assert report["video"] == "one_source.mp4"
        assert report["zh_dub_mp4"] == "one_zh_dub.mp4"
        assert str(one_report) not in rendered_reports
        assert str(one_video) not in rendered_reports
        assert str(one_dub) not in rendered_reports
        response = student_one.get("/api/dashboard")
        assert response.status_code == 200
        payload = response.json()
        assert payload["report_count"] == 1
        assert {report["name"] for report in payload["latest_reports"]} == {"one"}
        rendered_dashboard = json.dumps(payload["latest_reports"], ensure_ascii=False)
        assert str(one_report) not in rendered_dashboard
        assert str(one_video) not in rendered_dashboard
        assert str(one_dub) not in rendered_dashboard

    with TestClient(app) as admin:
        response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
        assert response.status_code == 200
        response = admin.get("/api/reports")
        assert response.status_code == 200
        admin_reports = response.json()["reports"]
        assert {report["name"] for report in admin_reports} == {"one", "two", "legacy"}
        assert str(one_report) in {report["path"] for report in admin_reports}


def test_report_refs_allow_non_admin_report_jobs_without_path_leak(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    project = state.store.create_project("student.one", "Course")
    report_path = Path(state.config["output_dir"]) / "student_report.json"
    report_path.write_text(
        json.dumps({"name": "student-report", "user": "student.one", "qa": {"pass": True, "issues": []}}),
        encoding="utf-8",
    )

    client = TestClient(app)
    response = client.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 200
    report_ref = client.get("/api/reports").json()["reports"][0]["path"]
    assert report_ref.startswith("report-ref:")

    response = client.post(
        "/api/jobs",
        json={"type": "fidelity_audit", "report": report_ref, "project_id": project["id"], "folder_id": "root"},
    )
    assert response.status_code == 200
    rendered = json.dumps(response.json(), ensure_ascii=False)
    assert str(report_path) not in rendered
    assert response.json()["job"]["queued_for_worker"] is True

    response = client.post(
        "/api/jobs",
        json={"type": "fidelity_audit", "report": str(report_path), "project_id": project["id"], "folder_id": "root"},
    )
    assert response.status_code == 400
    assert "Report reference is required" in response.text


def test_dashboard_redacts_storage_paths_for_non_admin(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    raw_input = str(state.config["input_dir"])
    raw_output = str(state.config["output_dir"])
    raw_upload = str(state.store.user_upload_dir("student.one"))

    with TestClient(app) as student:
        response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
        assert response.status_code == 200
        response = student.get("/api/dashboard")
        assert response.status_code == 200
        payload = response.json()
        rendered = json.dumps(payload, ensure_ascii=False)
        assert payload["storage_summary"]["redacted"] is True
        assert raw_input not in rendered
        assert raw_output not in rendered
        assert raw_upload not in rendered
        assert payload["input_dir"] == "managed course media"
        assert payload["output_dir"] == "managed output storage"
        assert payload["upload_dir"] == "your upload storage"

    with TestClient(app) as admin:
        response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
        assert response.status_code == 200
        response = admin.get("/api/dashboard")
        assert response.status_code == 200
        payload = response.json()
        assert payload["storage_summary"]["redacted"] is False
        assert payload["input_dir"] == raw_input
        assert payload["output_dir"] == raw_output


def test_video_records_use_refs_for_non_admin_paths(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    course_video = Path(state.config["input_dir"]) / "lecture.mp4"
    course_video.write_bytes(b"course mp4")
    upload_video = state.store.user_upload_dir("student.one") / "upload.mp4"
    upload_video.write_bytes(b"upload mp4")

    records = list_all_video_records(state, "student.one")
    paths = {row["path"] for row in records}
    assert str(course_video) not in paths
    assert str(upload_video) not in paths
    assert {row["display_path"] for row in records} == {"course media: lecture.mp4", "uploaded media: upload.mp4"}
    refs = {row["name"]: row["path"] for row in records}
    assert refs["lecture.mp4"].startswith("video-ref:")
    assert refs["upload.mp4"].startswith("video-ref:")
    assert resolve_video_reference(state, "student.one", refs["lecture.mp4"]) == str(course_video)
    assert resolve_video_reference(state, "student.one", refs["upload.mp4"]) == str(upload_video)

    admin_records = list_all_video_records(state, "admin")
    assert any(row["path"] == str(course_video) for row in admin_records)


def test_videos_api_redacts_paths_for_non_admin(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    course_video = Path(state.config["input_dir"]) / "lecture.mp4"
    course_video.write_bytes(b"course mp4")

    with TestClient(app) as student:
        response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
        assert response.status_code == 200
        response = student.get("/api/videos")
        assert response.status_code == 200
        payload = response.json()
        assert all(row["path"] != str(course_video) for row in payload["videos"])
        assert all(row["path"].startswith("video-ref:") for row in payload["videos"])

    with TestClient(app) as admin:
        response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
        assert response.status_code == 200
        response = admin.get("/api/videos")
        assert response.status_code == 200
        assert any(row["path"] == str(course_video) for row in response.json()["videos"])


def test_artifact_download_urls_are_user_scoped(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    state.store.create_user("student.two", "another-long-password")

    output = Path(state.config["output_dir"])
    one_video = output / "one_zh_dub.mp4"
    one_report = output / "one_report.json"
    two_video = output / "two_zh_dub.mp4"
    two_report = output / "two_report.json"
    one_video.write_bytes(b"student one mp4")
    two_video.write_bytes(b"student two mp4")
    one_report.write_text(
        json.dumps({"name": "one", "user": "student.one", "outputs": {"zh_dub_mp4": str(one_video)}}),
        encoding="utf-8",
    )
    two_report.write_text(
        json.dumps({"name": "two", "user": "student.two", "outputs": {"zh_dub_mp4": str(two_video)}}),
        encoding="utf-8",
    )

    student_one = TestClient(app)
    response = student_one.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 200
    artifacts = student_one.get("/api/artifacts").json()["artifacts"]
    names = {item["name"] for item in artifacts}
    assert "one_zh_dub.mp4" in names
    assert "two_zh_dub.mp4" not in names
    download_url = next(item["download_url"] for item in artifacts if item["name"] == "one_zh_dub.mp4")

    anonymous = TestClient(app)
    response = anonymous.get(download_url)
    assert response.status_code == 200
    assert response.content == b"student one mp4"

    student_two = TestClient(app)
    response = student_two.post("/api/login", json={"username": "student.two", "password": "another-long-password"})
    assert response.status_code == 200
    response = student_two.get(download_url)
    assert response.status_code == 401
    assert "Invalid signed URL" in response.text

    admin = TestClient(app)
    response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = admin.patch("/api/users/student.one", json={"disabled": True})
    assert response.status_code == 200

    response = anonymous.get(download_url)
    assert response.status_code == 401
    assert "Login or signed token required" in response.text


def test_artifact_api_redacts_paths_for_non_admin_but_keeps_actions(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")

    output = Path(state.config["output_dir"])
    video = output / "one_zh_dub.mp4"
    report = output / "one_report.json"
    video.write_bytes(b"student one mp4")
    report.write_text(
        json.dumps({"name": "one", "user": "student.one", "outputs": {"zh_dub_mp4": str(video)}}),
        encoding="utf-8",
    )

    client = TestClient(app)
    response = client.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 200
    response = client.get("/api/artifacts")
    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert str(video) not in rendered
    assert str(report) not in rendered

    artifact = next(item for item in payload["artifacts"] if item["name"] == "one_zh_dub.mp4")
    assert "path" not in artifact
    assert "report" not in artifact
    assert artifact["display_path"] == "generated output: one_zh_dub.mp4"
    assert artifact["deletable"] is True
    assert artifact["download_url"]

    response = client.delete(f"/api/artifacts/{artifact['id']}")
    assert response.status_code == 200
    rendered_delete = json.dumps(response.json(), ensure_ascii=False)
    assert str(video) not in rendered_delete
    assert str(report) not in rendered_delete
    assert "path" not in response.json()["artifact"]
    assert not video.exists()


def test_artifacts_endpoint_filters_by_project_folder_job_and_kind(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    course = state.store.create_project("admin", "Course")
    week_1 = state.store.create_folder("admin", course["id"], "Week 1")
    other = state.store.create_project("admin", "Other")
    output = Path(state.config["output_dir"])
    course_video = output / "course_zh_dub.mp4"
    course_report = output / "course_report.json"
    other_video = output / "other_zh_dub.mp4"
    other_report = output / "other_report.json"
    course_video.write_bytes(b"course mp4")
    other_video.write_bytes(b"other mp4")
    course_report.write_text(
        json.dumps({"name": "course", "outputs": {"zh_dub_mp4": str(course_video)}}),
        encoding="utf-8",
    )
    other_report.write_text(
        json.dumps({"name": "other", "outputs": {"zh_dub_mp4": str(other_video)}}),
        encoding="utf-8",
    )
    course_job = create_job_record(
        state,
        "process_one",
        "Course job",
        ["python", "-m", "ecse_localizer", "process-one"],
        user="admin",
        metadata={"project_id": course["id"], "folder_id": week_1["id"]},
        dispatch_target="worker",
    )
    other_job = create_job_record(
        state,
        "process_one",
        "Other job",
        ["python", "-m", "ecse_localizer", "process-one"],
        user="admin",
        metadata={"project_id": other["id"], "folder_id": "root"},
        dispatch_target="worker",
    )
    update_job(state, course_job["id"], {"status": "done", "result_report": str(course_report)})
    update_job(state, other_job["id"], {"status": "done", "result_report": str(other_report)})

    response = client.get(f"/api/artifacts?project_id={course['id']}&folder_id={week_1['id']}&kind=zh_dub_mp4")
    assert response.status_code == 200
    names = [row["name"] for row in response.json()["artifacts"]]
    assert names == ["course_zh_dub.mp4"]

    response = client.get(f"/api/artifacts?job_id={other_job['id']}&kind=zh_dub_mp4")
    assert response.status_code == 200
    names = [row["name"] for row in response.json()["artifacts"]]
    assert names == ["other_zh_dub.mp4"]

    response = client.get(f"/api/jobs/{course_job['id']}/artifacts")
    assert response.status_code == 200
    names = {row["name"] for row in response.json()["artifacts"]}
    assert {"course_zh_dub.mp4", "course"}.issubset(names)

    response = client.delete(f"/api/jobs/{course_job['id']}")
    assert response.status_code == 200
    response = client.get(f"/api/jobs/{course_job['id']}/artifacts")
    assert response.status_code == 200
    names = {row["name"] for row in response.json()["artifacts"]}
    assert "course_zh_dub.mp4" in names

    response = client.get(f"/api/artifacts?project_id={course['id']}&folder_id=root")
    assert response.status_code == 200
    assert response.json()["artifacts"] == []


def test_ownerless_remote_preview_is_admin_only(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    preview_dir = tmp_path / "previews"
    config["webui"]["preview_dir"] = str(preview_dir)
    config["webui"]["preview_manifest"] = str(preview_dir / "preview_manifest.json")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    state.store.create_user("student.one", "long-enough-password")
    preview_dir.mkdir()
    preview = preview_dir / "legacy_preview.mp4"
    preview.write_bytes(b"legacy preview")
    (preview_dir / "preview_manifest.json").write_text(
        json.dumps({"previews": [{"id": "legacy-preview", "name": "legacy.mp4", "preview_path": str(preview)}]}),
        encoding="utf-8",
    )

    student = TestClient(app)
    response = student.post("/api/login", json={"username": "student.one", "password": "long-enough-password"})
    assert response.status_code == 200
    response = student.get("/api/artifacts")
    assert response.status_code == 200
    assert "legacy-preview" not in {row["id"] for row in response.json()["artifacts"]}

    admin = TestClient(app)
    response = admin.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = admin.get("/api/artifacts")
    assert response.status_code == 200
    assert "legacy-preview" in {row["id"] for row in response.json()["artifacts"]}


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


def test_worker_queue_process_one_rejects_raw_worker_path_by_default(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    worker_path = r"D:\worker-media\lecture.mp4"

    response = client.post(
        "/api/jobs",
        json={"type": "process_one", "video": worker_path, "project_id": project["id"], "folder_id": "root"},
    )

    assert response.status_code == 400
    assert "worker-ref" in response.text
    assert "worker-media" not in response.text


def test_worker_queue_video_options_expose_only_safe_worker_ref_ids(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    win_root = "C:" + "\\Users\\Alice\\Desktop"
    state.store.record_worker_heartbeat(
        {
            "status": "online",
            "worker_id": "worker-1",
            "media_refs": [
                {"ref_id": "safe_ref-1", "name": "safe.mp4", "size": 10, "media_type": "video/mp4"},
                {"ref_id": "../escape", "name": f"{win_root}\\secret.mp4", "size": 10, "media_type": "video/mp4"},
            ],
        }
    )
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = client.get("/api/videos")

    assert response.status_code == 200
    payload = response.json()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "worker-ref:safe_ref-1" in serialized
    assert "worker-ref:../escape" not in serialized
    assert "Alice" not in serialized
    assert "secret.mp4" not in serialized


def test_worker_queue_process_one_accepts_worker_ref_without_paths(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]

    response = client.post(
        "/api/jobs",
        json={"type": "process_one", "video": "worker-ref:media123", "video_name": "lecture.mp4", "project_id": project["id"], "folder_id": "root"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dispatch"]["target"] == "worker"
    assert payload["job"]["metadata"]["worker_args"] == ["process-one", "--video", "worker-ref:media123"]
    assert payload["job"]["title"] == "Process one: lecture.mp4"
    assert "worker-media" not in json.dumps(payload, ensure_ascii=False)


def test_worker_queue_normalizes_unsafe_worker_id_across_claim_and_status(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    response = client.post(
        "/api/jobs",
        json={"type": "process_one", "video": "worker-ref:media123", "video_name": "lecture.mp4", "project_id": project["id"], "folder_id": "root"},
    )
    assert response.status_code == 200
    job_id = response.json()["job"]["id"]
    unsafe_worker_id = "C:" + "\\Users\\Alice\\Desktop\\token=abc123"
    expected_worker_id = safe_worker_id(unsafe_worker_id)

    response = client.post(
        "/api/worker/jobs/claim",
        headers={"x-worker-token": "worker-token"},
        json={"worker_id": unsafe_worker_id},
    )
    assert response.status_code == 200
    assert response.json()["job"]["claimed_by"] == expected_worker_id

    response = client.post(
        f"/api/worker/jobs/{job_id}/status",
        headers={"x-worker-token": "worker-token"},
        json={"status": "running", "worker_id": unsafe_worker_id, "progress": 50},
    )
    assert response.status_code == 200
    rendered = json.dumps(response.json(), ensure_ascii=False)
    assert "Alice" not in rendered
    assert "token=abc123" not in rendered
    assert response.json()["job"]["worker_id"] == expected_worker_id


def test_worker_queue_process_one_rejects_unsafe_worker_ref(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    response = client.post(
        "/api/jobs",
        json={"type": "process_one", "video": "worker-ref:../escape", "project_id": project["id"], "folder_id": "root"},
    )

    assert response.status_code == 400
    assert "worker-ref" in response.text
    assert not client.get("/api/jobs").json()["jobs"]


def test_worker_queue_raw_worker_path_opt_in_redacts_browser_responses(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["allow_worker_path_submission"] = True
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    worker_path = r"D:\worker-media\lecture.mp4"

    response = client.post(
        "/api/jobs",
        json={"type": "process_one", "video": worker_path, "project_id": project["id"], "folder_id": "root"},
    )

    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, ensure_ascii=False)
    assert r"D:\worker-media" not in rendered
    assert "worker-media" not in rendered
    assert payload["job"]["metadata"]["worker_args"] == ["process-one", "--video", "<local-path>"]

    job_id = payload["job"]["id"]
    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert r"D:\worker-media" not in json.dumps(response.json(), ensure_ascii=False)

    response = client.post(
        "/api/worker/jobs/claim",
        headers={"x-worker-token": "worker-token"},
        json={"worker_id": "worker-1"},
    )
    assert response.status_code == 200
    assert response.json()["job"]["metadata"]["worker_args"] == ["process-one", "--video", worker_path]


def test_worker_media_refs_appear_as_video_options_without_paths(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = client.post(
        "/api/worker/heartbeat",
        headers={"x-worker-token": "worker-token"},
        json={
            "worker_id": "worker-1",
            "media_refs": [
                {
                    "ref_id": "media123",
                    "name": "lecture.mp4",
                    "path": r"D:\worker-private\lecture.mp4",
                    "size": 10,
                    "media_type": "video/mp4",
                }
            ],
        },
    )
    assert response.status_code == 200
    assert "path" not in response.json()["worker"]["media_refs"][0]

    response = client.get("/api/videos")

    assert response.status_code == 200
    body = response.json()
    worker_video = next(row for row in body["videos"] if row.get("worker_ref"))
    assert worker_video["path"] == "worker-ref:media123"
    assert worker_video["display_path"] == "Windows worker: lecture.mp4"
    assert "worker-private" not in json.dumps(body)


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
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
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
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})
    response = client.post(
        f"/api/worker/jobs/{record['id']}/status",
        headers={"x-worker-token": "worker-token"},
        json={
            "status": "running",
            "worker_id": "worker-1",
            "progress": 33,
            "log_tail": "overall progress: 33%",
            "metrics": {
                "gpu": [{"available": True, "util_percent": 50, "memory_used_percent": 25}],
                "disk": {"path": r"C:\private\worker-output", "used_percent": 61},
                "local_storage": {"managed_bytes": 12345, "total_reported_bytes": 12345, "roots": []},
            },
        },
    )
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "running"
    assert job["progress"] == 33
    assert job["log_tail"] == "overall progress: 33%"
    assert "path" not in job["metrics"]["disk"]

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    worker = response.json()["worker"]
    assert worker["status"] == "online"
    assert worker["worker_id"] == "worker-1"
    assert worker["heartbeat_online"] is True
    assert "path" not in worker["metrics"]["disk"]
    assert response.json()["metrics"]["source"] == "worker_heartbeat"
    assert response.json()["metrics"]["gpu"][0]["util_percent"] == 50
    assert response.json()["quota"]["local_used_bytes"] == 12345


def test_worker_status_update_cannot_soft_delete_job(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
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
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})

    response = client.post(
        f"/api/worker/jobs/{record['id']}/status",
        headers={"x-worker-token": "worker-token"},
        json={"status": "deleted", "worker_id": "worker-1"},
    )

    assert response.status_code == 400
    assert "Unsupported worker job status: deleted" in response.text
    assert read_job(state, record["id"])["status"] == "claimed"


def test_metrics_endpoint_returns_live_worker_metrics_without_paths(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = client.post(
        "/api/worker/heartbeat",
        headers={"x-worker-token": "worker-token"},
        json={
            "worker_id": "worker-1",
            "max_concurrent_jobs": 2,
            "metrics": {
                "cpu": {"load_percent": 42},
                "memory": {"used_percent": 38},
                "gpu": [{"available": True, "util_percent": 71, "memory_used_percent": 33, "name": "RTX"}],
                "disk": {"path": r"C:\private\worker-output", "used_percent": 61},
                "local_storage": {"managed_bytes": 54321, "total_reported_bytes": 54321, "roots": []},
            },
        },
    )
    assert response.status_code == 200

    response = client.get("/api/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["worker"]["heartbeat_online"] is True
    assert body["worker"]["max_concurrent_jobs"] == 2
    assert body["queue"]["worker_max_slots"] == 2
    assert body["queue"]["worker_slots_available"] == 2
    assert body["metrics"]["source"] == "worker_heartbeat"
    assert body["metrics"]["cpu"]["load_percent"] == 42
    assert body["metrics"]["memory"]["used_percent"] == 38
    assert body["metrics"]["gpu"][0]["util_percent"] == 71
    assert body["quota"]["local_used_bytes"] == 54321
    assert "path" not in json.dumps(body).lower()
    assert "private" not in json.dumps(body).lower()


def test_capabilities_prefer_online_worker_heartbeat(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["worker_auth_mode"] = "hmac"
    config["translation"]["supported_target_languages"] = ["zh-CN"]
    config["tts"] = {"supported_languages": ["zh-CN"], "language": "zh-CN"}
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    worker_caps = {
        "asr": {"supported_languages": ["auto", "en", "ja"], "current_supported": True},
        "translation": {"supported_target_languages": ["zh-CN", "ja"], "current_supported": True},
        "tts": {"supported_languages": ["zh-CN", "ja"], "current_supported": True},
    }
    body = json.dumps({"worker_id": "worker-1", "capabilities": worker_caps}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    response = client.post(
        "/api/worker/heartbeat",
        data=body.encode("utf-8"),
        headers=worker_headers("worker-token", path="/api/worker/heartbeat", body=body),
    )
    assert response.status_code == 200

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = client.get("/api/capabilities")

    assert response.status_code == 200
    caps = response.json()
    assert caps["source"] == "worker_heartbeat"
    assert caps["worker_id"] == "worker-1"
    assert caps["tts"]["supported_languages"] == ["zh-CN", "ja"]
    assert caps["translation"]["supported_target_languages"] == ["zh-CN", "ja"]


def test_running_worker_job_cancel_is_polled_and_finalized(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
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
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "running", "claimed_by": "worker-1"})

    response = client.post(f"/api/jobs/{record['id']}/cancel", json={})

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "running"
    assert job["cancel_requested"] is True
    assert job["cancel_requested_by"] == "admin"

    response = client.post(
        f"/api/worker/jobs/{record['id']}/control",
        headers={"x-worker-token": "worker-token"},
        json={"worker_id": "worker-1"},
    )

    assert response.status_code == 200
    control = response.json()["control"]
    assert control["status"] == "running"
    assert control["cancel_requested"] is True
    assert "command" not in control
    assert "path" not in json.dumps(control).lower()

    response = client.post(
        f"/api/worker/jobs/{record['id']}/status",
        headers={"x-worker-token": "worker-token"},
        json={"status": "done", "worker_id": "worker-1", "returncode": 0, "worker_artifacts": [{"ref_id": "late"}]},
    )

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "cancelled"
    assert job["returncode"] == -9
    assert job["cancel_handled_at"]
    assert "worker_artifacts" not in job

    response = client.post(f"/api/jobs/{record['id']}/retry", json={})

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "retrying"
    assert job["cancel_requested"] is False
    assert job["cancel_requested_at"] is None


def test_stale_worker_status_cannot_override_reclaimed_job(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    record = create_job_record(
        state,
        "audit",
        "Remote audit",
        ["python", "-m", "ecse_localizer", "audit"],
        user="admin",
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "running", "claimed_by": "worker-new", "worker_id": "worker-new"})

    response = client.post(
        f"/api/worker/jobs/{record['id']}/status",
        headers={"x-worker-token": "worker-token"},
        json={
            "status": "done",
            "worker_id": "worker-old",
            "returncode": 0,
            "worker_artifacts": [{"ref_id": "stale-output"}],
        },
    )

    assert response.status_code == 409
    persisted = read_job(state, record["id"])
    assert persisted["status"] == "running"
    assert persisted["claimed_by"] == "worker-new"
    assert "worker_artifacts" not in persisted


def test_worker_queue_pause_resume_and_cancel_paused_job(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
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
        metadata={"worker_args": ["audit", "--input", "x"]},
        dispatch_target="worker",
    )

    response = client.post(f"/api/jobs/{record['id']}/pause", json={})

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "paused"
    assert job["paused_by"] == "admin"

    response = client.post(
        "/api/worker/jobs/claim",
        headers={"x-worker-token": "worker-token"},
        json={"worker_id": "worker-1"},
    )
    assert response.status_code == 200
    assert response.json()["job"] is None

    response = client.post(f"/api/jobs/{record['id']}/resume", json={})

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "queued"
    assert job["resumed_by"] == "admin"

    response = client.post(f"/api/jobs/{record['id']}/pause", json={})
    assert response.status_code == 200
    response = client.post(f"/api/jobs/{record['id']}/cancel", json={})

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "cancelled"
    assert job["returncode"] == -9
    assert not job.get("cancel_requested")


def test_job_submit_rejects_full_local_worker_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["default_local_quota_gb"] = 0.000000001
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = client.post(
        "/api/worker/heartbeat",
        headers={"x-worker-token": "worker-token"},
        json={
            "worker_id": "worker-1",
            "metrics": {"local_storage": {"managed_bytes": 2, "total_reported_bytes": 2, "roots": []}},
        },
    )
    assert response.status_code == 200

    project = client.get("/api/projects").json()["projects"][0]
    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "folder_id": "root"})

    assert response.status_code == 413
    assert "Local worker quota exceeded" in response.text


def test_job_submit_rejects_full_project_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    response = client.post("/api/projects", json={"name": "Tiny Project", "quota_project_gb": 0.000000001})
    assert response.status_code == 200
    project = response.json()["project"]

    output = Path(state.config["output_dir"])
    generated = output / "existing_zh_dub.mp4"
    generated.write_bytes(b"existing")
    report = output / "existing_report.json"
    report.write_text(
        json.dumps(
            {
                "name": "existing",
                "user": "admin",
                "project_id": project["id"],
                "outputs": {"zh_dub_mp4": str(generated)},
            }
        ),
        encoding="utf-8",
    )

    response = client.post("/api/jobs", json={"type": "audit", "project_id": project["id"], "folder_id": "root"})

    assert response.status_code == 413
    assert "Project quota exceeded" in response.text


def test_worker_preview_upload_registers_manifest_and_artifact(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["worker_auth_mode"] = "hmac"
    config["webui"]["worker_preview_max_upload_mb"] = 1
    config["webui"]["preview_dir"] = str(tmp_path / "previews")
    config["webui"]["preview_manifest"] = str(tmp_path / "previews" / "preview_manifest.json")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    project = client.get("/api/projects").json()["projects"][0]
    record = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={"project_id": project["id"], "folder_id": "root"},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})

    path = f"/api/worker/jobs/{record['id']}/preview"
    body = b"small mp4 preview"
    headers = worker_headers(
        "worker-token",
        path=path,
        body=body,
        extra_headers={
            "Content-Type": "video/mp4",
            "X-Worker-Preview-Variant": "preview",
            "X-Worker-Preview-Id": "preview-1",
            "X-Worker-Preview-Name": "lecture_zh_dub.mp4",
            "X-Worker-Preview-File-Name": "lecture_preview.mp4",
            "X-Worker-Preview-Source-Key": "zh_dub_mp4",
            "X-Worker-Id": "worker-1",
        },
    )
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["preview"]["preview_path"].endswith("lecture_preview.mp4")

    thumb_body = b"jpg"
    thumb_headers = worker_headers(
        "worker-token",
        path=path,
        body=thumb_body,
        extra_headers={
            "Content-Type": "image/jpeg",
            "X-Worker-Preview-Variant": "thumbnail",
            "X-Worker-Preview-Id": "preview-1",
            "X-Worker-Preview-Name": "lecture_zh_dub.mp4",
            "X-Worker-Preview-File-Name": "lecture_thumb.jpg",
            "X-Worker-Preview-Source-Key": "zh_dub_mp4",
            "X-Worker-Id": "worker-1",
        },
    )
    response = client.post(path, data=thumb_body, headers=thumb_headers)
    assert response.status_code == 200

    manifest = read_json(tmp_path / "previews" / "preview_manifest.json")
    row = manifest["previews"][0]
    assert row["id"] == "preview-1"
    assert row["owner"] == "admin"
    assert row["project_id"] == project["id"]
    assert row["source_output_key"] == "zh_dub_mp4"
    assert row["preview_path"].endswith("lecture_preview.mp4")
    assert row["thumbnail_path"].endswith("lecture_thumb.jpg")
    assert "source_path" not in row
    assert "private" not in str(row)

    artifact = next(item for item in artifact_catalog(state.config, [read_json(state.job_dir / f"{record['id']}.json")]) if item.get("remote_preview"))
    assert artifact["name"] == "lecture_zh_dub.mp4"
    assert artifact["thumbnail_path"].endswith("lecture_thumb.jpg")
    quota = client.get("/api/quota").json()
    assert quota["remote_used_bytes"] >= len(body) + len(thumb_body)


def test_worker_preview_upload_rejects_tampered_signed_headers(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["worker_auth_mode"] = "hmac"
    config["webui"]["preview_dir"] = str(tmp_path / "previews")
    config["webui"]["preview_manifest"] = str(tmp_path / "previews" / "preview_manifest.json")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)
    record = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})

    path = f"/api/worker/jobs/{record['id']}/preview"
    body = b"small mp4 preview"
    headers = worker_headers(
        "worker-token",
        path=path,
        body=body,
        extra_headers={
            "Content-Type": "video/mp4",
            "X-Worker-Preview-Variant": "preview",
            "X-Worker-Preview-File-Name": "lecture_preview.mp4",
            "X-Worker-Id": "worker-1",
        },
    )
    headers["X-Worker-Id"] = "worker-2"

    response = client.post(path, data=body, headers=headers)

    assert response.status_code == 401
    assert "Invalid worker HMAC signature" in response.text


def test_request_worker_artifact_cache_creates_worker_job(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    source_job = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={"project_id": "course", "folder_id": "week_1"},
        dispatch_target="worker",
    )
    update_job(
        state,
        source_job["id"],
        {
            "status": "done",
            "worker_artifacts": [
                {
                    "ref_id": "ref1",
                    "source_output_key": "zh_dub_mp4",
                    "name": "lecture_zh_dub.mp4",
                    "size": 123,
                    "media_type": "video/mp4",
                }
            ],
        },
    )

    response = client.get("/api/artifacts")
    assert response.status_code == 200
    artifact = next(item for item in response.json()["artifacts"] if item.get("remote_worker_artifact"))
    assert artifact["request_cache_url"] == "/api/artifacts/worker_artifact_ref1/request-cache"
    assert "download_url" not in artifact

    response = client.post(artifact["request_cache_url"], json={})
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["type"] == "cache_artifact"
    assert job["dispatch_target"] == "worker"
    assert job["metadata"]["worker_action"] == "upload_artifact_cache"
    assert job["metadata"]["artifact_ref_id"] == "ref1"


def test_request_worker_artifact_cache_rejects_known_size_over_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["default_remote_quota_gb"] = 0.00000001
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    source_job = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={"project_id": "course", "folder_id": "week_1"},
        dispatch_target="worker",
    )
    update_job(
        state,
        source_job["id"],
        {
            "status": "done",
            "worker_artifacts": [{"ref_id": "ref_big", "source_output_key": "zh_dub_mp4", "name": "lecture_zh_dub.mp4", "size": 100}],
        },
    )
    artifact = next(item for item in client.get("/api/artifacts").json()["artifacts"] if item["id"] == "worker_artifact_ref_big")

    response = client.post(artifact["request_cache_url"], json={})

    assert response.status_code == 413
    assert "Remote quota exceeded" in response.text
    assert not any(job["type"] == "cache_artifact" for job in client.get("/api/jobs").json()["jobs"])


def test_request_worker_artifact_cache_rejects_known_size_over_global_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["default_remote_quota_gb"] = 1
    config["webui"]["global_remote_quota_gb"] = 0.00000001
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    source_job = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={"project_id": "course", "folder_id": "week_1"},
        dispatch_target="worker",
    )
    update_job(
        state,
        source_job["id"],
        {
            "status": "done",
            "worker_artifacts": [{"ref_id": "ref_big", "source_output_key": "zh_dub_mp4", "name": "lecture_zh_dub.mp4", "size": 100}],
        },
    )
    artifact = next(item for item in client.get("/api/artifacts").json()["artifacts"] if item["id"] == "worker_artifact_ref_big")

    response = client.post(artifact["request_cache_url"], json={})

    assert response.status_code == 413
    assert "Global remote quota exceeded" in response.text
    assert not any(job["type"] == "cache_artifact" for job in client.get("/api/jobs").json()["jobs"])


def test_request_worker_artifact_cache_rejects_known_size_over_cache_limit(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["execution_mode"] = "worker_queue"
    config["webui"]["worker_artifact_cache_max_upload_mb"] = 1
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
    source_job = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    update_job(
        state,
        source_job["id"],
        {
            "status": "done",
            "worker_artifacts": [
                {
                    "ref_id": "ref_too_large",
                    "source_output_key": "zh_dub_mp4",
                    "name": "lecture_zh_dub.mp4",
                    "size": 2 * 1024 * 1024,
                }
            ],
        },
    )
    artifact = next(item for item in client.get("/api/artifacts").json()["artifacts"] if item["id"] == "worker_artifact_ref_too_large")

    response = client.post(artifact["request_cache_url"], json={})

    assert response.status_code == 413
    assert "Worker artifact cache exceeds 1 MB" in response.text
    assert not any(job["type"] == "cache_artifact" for job in client.get("/api/jobs").json()["jobs"])


def test_worker_artifact_cache_upload_registers_downloadable_artifact(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["worker_auth_mode"] = "hmac"
    config["webui"]["preview_dir"] = str(tmp_path / "previews")
    config["webui"]["preview_manifest"] = str(tmp_path / "previews" / "preview_manifest.json")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)

    response = client.post("/api/login", json={"username": "admin", "password": "local-password"})
    assert response.status_code == 200
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
            "source_output_key": "zh_dub_mp4",
            "project_id": "course",
            "folder_id": "week_1",
        },
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})

    path = f"/api/worker/jobs/{record['id']}/artifact-cache"
    body = b"full mp4"
    headers = worker_headers(
        "worker-token",
        path=path,
        body=body,
        extra_headers={
            "Content-Type": "video/mp4",
            "X-Worker-Artifact-Id": "worker_artifact_ref1",
            "X-Worker-Artifact-Ref": "ref1",
            "X-Worker-Artifact-Name": "lecture_zh_dub.mp4",
            "X-Worker-Artifact-File-Name": "lecture_zh_dub.mp4",
            "X-Worker-Artifact-Source-Key": "zh_dub_mp4",
            "X-Worker-Id": "worker-1",
        },
    )
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["artifact"]["id"] == "worker_artifact_ref1"

    artifact = next(item for item in client.get("/api/artifacts").json()["artifacts"] if item["id"] == "worker_artifact_ref1")
    assert artifact["download_url"]
    assert artifact["remote_cache"] is True
    assert "request_cache_url" not in artifact


def test_worker_preview_upload_respects_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["worker_auth_mode"] = "hmac"
    config["webui"]["default_remote_quota_gb"] = 0.00000001
    config["webui"]["preview_dir"] = str(tmp_path / "previews")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)
    record = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})

    path = f"/api/worker/jobs/{record['id']}/preview"
    body = b"x" * 100
    headers = worker_headers(
        "worker-token",
        path=path,
        body=body,
        extra_headers={
            "Content-Type": "video/mp4",
            "X-Worker-Preview-Variant": "preview",
            "X-Worker-Preview-File-Name": "too_big_preview.mp4",
            "X-Worker-Id": "worker-1",
        },
    )
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 413
    assert "Remote quota exceeded" in response.text


def test_worker_preview_upload_respects_global_remote_quota(tmp_path):
    if TestClient is None:
        pytest.skip(str(TESTCLIENT_IMPORT_ERROR))
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["webui"]["worker_auth_mode"] = "hmac"
    config["webui"]["default_remote_quota_gb"] = 1
    config["webui"]["global_remote_quota_gb"] = 0.00000001
    config["webui"]["preview_dir"] = str(tmp_path / "previews")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(config_path)
    state = app.state.web
    client = TestClient(app)
    record = create_job_record(
        state,
        "process_one",
        "Remote process",
        ["python", "-m", "ecse_localizer", "process-one", "--video", r"C:\private\lecture.mp4"],
        user="admin",
        metadata={},
        dispatch_target="worker",
    )
    update_job(state, record["id"], {"status": "claimed", "claimed_by": "worker-1"})

    path = f"/api/worker/jobs/{record['id']}/preview"
    body = b"x" * 100
    headers = worker_headers(
        "worker-token",
        path=path,
        body=body,
        extra_headers={
            "Content-Type": "video/mp4",
            "X-Worker-Preview-Variant": "preview",
            "X-Worker-Preview-File-Name": "too_big_preview.mp4",
            "X-Worker-Id": "worker-1",
        },
    )
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 413
    assert "Global remote quota exceeded" in response.text
