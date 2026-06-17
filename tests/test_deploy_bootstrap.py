from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_bootstrap_module():
    path = Path(__file__).resolve().parents[1] / "deploy" / "bootstrap_contabo.py"
    spec = importlib.util.spec_from_file_location("bootstrap_contabo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_contabo_generates_env_and_remote_config(tmp_path):
    module = load_bootstrap_module()
    repo = tmp_path
    deploy = repo / "deploy"
    deploy.mkdir()
    (deploy / "config.remote.example.yaml").write_text("webui:\n  execution_mode: worker_queue\n", encoding="utf-8")

    written = module.bootstrap_contabo(repo, public_base_url="https://localizer.example.com", admin_username="owner")

    env_path = Path(written[".env"])
    config_path = Path(written["remote_config"])
    values = module.parse_env(env_path.read_text(encoding="utf-8"))
    assert config_path.read_text(encoding="utf-8") == "webui:\n  execution_mode: worker_queue\n"
    assert values["APP_ENV"] == "remote"
    assert values["WEBUI_HOST"] == "0.0.0.0"
    assert values["WEBUI_ADMIN_USERNAME"] == "owner"
    assert values["REMOTE_PUBLIC_BASE_URL"] == "https://localizer.example.com"
    assert values["REMOTE_PUBLIC_HOST"] == "localizer.example.com"
    assert values["WEBUI_SESSION_SECRET"].startswith("change-me") is False
    assert values["WEBUI_DOWNLOAD_SECRET"].startswith("change-me") is False
    assert values["WORKER_SHARED_TOKEN"].startswith("change-me") is False
    assert len(values["WORKER_SHARED_TOKEN"]) >= 48


def test_bootstrap_contabo_refuses_overwrite_without_force(tmp_path):
    module = load_bootstrap_module()
    repo = tmp_path
    deploy = repo / "deploy"
    deploy.mkdir()
    (repo / ".env").write_text("EXISTING=1\n", encoding="utf-8")
    (deploy / "config.remote.example.yaml").write_text("template\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        module.bootstrap_contabo(repo, public_base_url="https://localizer.example.com")


def test_bootstrap_contabo_requires_https_by_default():
    module = load_bootstrap_module()

    with pytest.raises(ValueError):
        module.validate_public_base_url("http://localizer.example.com")

    module.validate_public_base_url("http://localizer.example.com", allow_http=True)
