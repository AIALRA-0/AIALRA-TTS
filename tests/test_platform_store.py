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


def test_create_invited_user(tmp_path):
    store = PlatformStore(make_config(tmp_path))
    store.bootstrap()
    user = store.create_user("student.one", "long-enough-password", quota_local_gb=2)

    assert user["username"] == "student.one"
    assert "password_hash" not in user
    assert store.verify_user("student.one", "long-enough-password")
    assert store.list_projects("student.one")[0]["name"] == "Default"


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
