import json
from pathlib import Path

from ecse_localizer.platform_store import PlatformStore, hash_password, verify_password


def make_config(tmp_path: Path) -> dict:
    return {
        "output_dir": str(tmp_path / "out"),
        "webui": {
            "username": "admin",
            "password": "local-password",
            "platform_dir": str(tmp_path / "platform"),
            "upload_dir": str(tmp_path / "uploads"),
            "default_local_quota_gb": 1,
            "default_remote_quota_gb": 1,
            "default_project_quota_gb": 3,
        },
    }


def test_password_hash_roundtrip():
    stored = hash_password("very-local-password")
    assert "very-local-password" not in stored
    assert verify_password("very-local-password", stored)
    assert not verify_password("wrong-password", stored)


def test_bootstrap_user_project_and_quota(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()

    assert store.verify_user("admin", "local-password")
    assert store.list_projects("admin")
    quota = store.quota_status("admin")
    assert quota["local_quota_bytes"] > 0
    assert quota["local_used_bytes"] == 0
    assert store.can_store("admin", 1024)
    project = store.list_projects("admin")[0]
    assert project["quota_project_bytes"] == 3 * 1024 * 1024 * 1024
    assert project["folders"][0]["id"] == "root"


def test_remote_quota_counts_uploads_and_preview_manifest(tmp_path):
    config = make_config(tmp_path)
    preview_dir = tmp_path / "previews"
    preview_dir.mkdir()
    config["webui"]["preview_dir"] = str(preview_dir)
    config["webui"]["preview_manifest"] = str(preview_dir / "preview_manifest.json")
    store = PlatformStore(config)
    store.bootstrap()

    upload = store.user_upload_dir("admin") / "lecture.mp4"
    upload.write_bytes(b"upload")
    preview = preview_dir / "lecture_preview.mp4"
    thumbnail = preview_dir / "lecture_thumb.jpg"
    preview.write_bytes(b"preview")
    thumbnail.write_bytes(b"thumb")
    Path(config["webui"]["preview_manifest"]).write_text(
        json.dumps({"previews": [{"owner": "admin", "preview_path": str(preview), "thumbnail_path": str(thumbnail)}]}),
        encoding="utf-8",
    )

    quota = store.quota_status("admin")

    assert quota["remote_used_bytes"] == len(b"uploadpreviewthumb")
    assert quota["remote_remaining_bytes"] == quota["remote_quota_bytes"] - quota["remote_used_bytes"]
    assert quota["local_used_bytes"] == 0


def test_can_store_uses_remote_quota(tmp_path):
    config = make_config(tmp_path)
    config["webui"]["default_remote_quota_gb"] = 0.000001
    store = PlatformStore(config)
    store.bootstrap()

    upload = store.user_upload_dir("admin") / "almost_full.bin"
    upload.write_bytes(b"x" * 900)

    assert store.can_store("admin", 100)
    assert not store.can_store("admin", 1000)


def test_create_invited_user(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()
    user = store.create_user("student.one", "long-enough-password", quota_local_gb=2)

    assert user["username"] == "student.one"
    assert "password_hash" not in user
    assert store.verify_user("student.one", "long-enough-password")
    assert store.list_projects("student.one")[0]["name"] == "Default"
    assert store.list_templates("student.one")[0]["name"] == "Best Quality Mandarin"


def test_update_user_quota_role_and_disabled_state(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()
    store.create_user("student.one", "long-enough-password", quota_local_gb=2, quota_remote_gb=3)

    updated = store.update_user("student.one", disabled=True, quota_local_gb=4, quota_remote_gb=5)
    assert updated["disabled"] is True
    assert updated["quota_local_bytes"] == 4 * 1024 * 1024 * 1024
    assert updated["quota_remote_bytes"] == 5 * 1024 * 1024 * 1024
    assert store.verify_user("student.one", "long-enough-password") is None

    updated = store.update_user("student.one", disabled=False, role="admin")
    assert updated["role"] == "admin"
    assert store.verify_user("student.one", "long-enough-password")


def test_update_user_preserves_at_least_one_active_admin(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()

    try:
        store.update_user("admin", disabled=True)
    except ValueError as exc:
        assert "active admin" in str(exc)
    else:
        raise AssertionError("disabling only admin should fail")


def test_project_folder_create_and_validation(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()
    project = store.create_project("admin", "Course", quota_project_gb=2)
    folder = store.create_folder("admin", project["id"], "Week 1")

    assert folder["name"] == "Week 1"
    store.validate_project_folder("admin", project["id"], folder["id"])
    projects = store.list_projects("admin")
    saved = next(item for item in projects if item["id"] == project["id"])
    assert saved["quota_project_bytes"] == 2 * 1024 * 1024 * 1024
    assert any(item["id"] == folder["id"] for item in saved["folders"])


def test_parameter_templates_are_user_scoped_and_sanitized(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()

    default_template = store.list_templates("admin")[0]
    assert default_template["params"]["quality_mode"] == "best_quality"

    template = store.create_template(
        "admin",
        "Fast Cantonese",
        {
            "target_subtitle_language": "zh-HK",
            "target_tts_language": "yue",
            "quality_mode": "fast",
            "tts_speed": "1.1",
            "mux_hard_subtitle": "false",
            "unknown_secret": "must-drop",
        },
    )
    assert template["params"]["tts_speed"] == 1.1
    assert template["params"]["mux_hard_subtitle"] is False
    assert "unknown_secret" not in template["params"]
    assert store.get_template("admin", template["id"])["name"] == "Fast Cantonese"

    deleted = store.delete_template("admin", template["id"])
    assert deleted["id"] == template["id"]
    assert store.get_template("admin", template["id"]) is None


def test_worker_heartbeat_redacts_message_and_media_ref_names(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()
    win_root = "C:" + "\\Users\\Alice\\Desktop\\ECSE 4961"
    private_ip = "10." + "0.0.12"

    row = store.record_worker_heartbeat(
        {
            "status": "online",
            "worker_id": "worker-1",
            "message": f"watching {win_root}\\lecture.mp4 via http://{private_ip}:8787/?token=abc123",
            "metrics": {"disk": {"path": f"{win_root}\\_localizer_output", "used_percent": 55}},
            "media_refs": [
                {
                    "ref_id": "media-1",
                    "name": f"{win_root}\\lecture.mp4",
                    "size": 12,
                    "mtime": 123.5,
                    "media_type": "video/mp4",
                    "path": f"{win_root}\\lecture.mp4",
                }
            ],
        }
    )

    serialized = json.dumps(row, ensure_ascii=False)
    assert "Alice" not in serialized
    assert win_root not in serialized
    assert private_ip not in serialized
    assert "token=abc123" not in serialized
    assert "path" not in row["metrics"]["disk"]
    assert row["media_refs"][0]["name"] == "lecture.mp4"
    assert "path" not in row["media_refs"][0]
