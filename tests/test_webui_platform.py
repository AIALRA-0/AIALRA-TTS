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
from ecse_localizer.utils import read_json
from ecse_localizer.webui import create_app, create_job_record, fields_from_config, update_job
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
    assert fields["tts.slot_trim_tolerance_seconds"]["type"] == "float"
    assert fields["tts.slot_trim_fade_seconds"]["type"] == "float"


def test_static_ui_does_not_expose_raw_worker_path_placeholder():
    html = (Path(__file__).parents[1] / "src" / "ecse_localizer" / "static" / "index.html").read_text(encoding="utf-8")

    assert r"D:\worker-media" not in html
    assert "jobWorkerVideoPath" in html


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
    headers = worker_headers("worker-token", path=path, body=body)
    headers.update(
        {
            "Content-Type": "video/mp4",
            "X-Worker-Preview-Variant": "preview",
            "X-Worker-Preview-Id": "preview-1",
            "X-Worker-Preview-Name": "lecture_zh_dub.mp4",
            "X-Worker-Preview-File-Name": "lecture_preview.mp4",
            "X-Worker-Preview-Source-Key": "zh_dub_mp4",
            "X-Worker-Id": "worker-1",
        }
    )
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["preview"]["preview_path"].endswith("lecture_preview.mp4")

    thumb_body = b"jpg"
    thumb_headers = worker_headers("worker-token", path=path, body=thumb_body)
    thumb_headers.update(
        {
            "Content-Type": "image/jpeg",
            "X-Worker-Preview-Variant": "thumbnail",
            "X-Worker-Preview-Id": "preview-1",
            "X-Worker-Preview-Name": "lecture_zh_dub.mp4",
            "X-Worker-Preview-File-Name": "lecture_thumb.jpg",
            "X-Worker-Preview-Source-Key": "zh_dub_mp4",
            "X-Worker-Id": "worker-1",
        }
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
    headers = worker_headers("worker-token", path=path, body=body)
    headers.update(
        {
            "Content-Type": "video/mp4",
            "X-Worker-Artifact-Id": "worker_artifact_ref1",
            "X-Worker-Artifact-Ref": "ref1",
            "X-Worker-Artifact-Name": "lecture_zh_dub.mp4",
            "X-Worker-Artifact-File-Name": "lecture_zh_dub.mp4",
            "X-Worker-Artifact-Source-Key": "zh_dub_mp4",
            "X-Worker-Id": "worker-1",
        }
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

    path = f"/api/worker/jobs/{record['id']}/preview"
    body = b"x" * 100
    headers = worker_headers("worker-token", path=path, body=body)
    headers.update(
        {
            "Content-Type": "video/mp4",
            "X-Worker-Preview-Variant": "preview",
            "X-Worker-Preview-File-Name": "too_big_preview.mp4",
            "X-Worker-Id": "worker-1",
        }
    )
    response = client.post(path, data=body, headers=headers)
    assert response.status_code == 413
    assert "Remote quota exceeded" in response.text
