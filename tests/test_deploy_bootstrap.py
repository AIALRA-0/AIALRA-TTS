from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from ecse_localizer.deploy_check import check_deploy_config


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
    (deploy / "config.remote.example.yaml").write_text(
        'webui:\n'
        '  execution_mode: worker_queue\n'
        '  username: "${WEBUI_ADMIN_USERNAME}"\n'
        '  password: "${WEBUI_ADMIN_PASSWORD}"\n'
        '  session_secret: "${WEBUI_SESSION_SECRET}"\n'
        '  worker_token: "${WORKER_SHARED_TOKEN}"\n'
        '  download_secret: "${WEBUI_DOWNLOAD_SECRET}"\n',
        encoding="utf-8",
    )

    written = module.bootstrap_contabo(repo, public_base_url="https://localizer.example.com", admin_username="owner")

    env_path = Path(written[".env"])
    config_path = Path(written["remote_config"])
    values = module.parse_env(env_path.read_text(encoding="utf-8"))
    config_text = config_path.read_text(encoding="utf-8")
    assert "${" not in config_text
    assert 'username: "owner"' in config_text
    assert values["WEBUI_ADMIN_PASSWORD"] in config_text
    assert values["WORKER_SHARED_TOKEN"] in config_text
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


def test_bootstrap_real_template_generates_deploy_check_passing_config(tmp_path):
    module = load_bootstrap_module()
    repo = tmp_path
    deploy = repo / "deploy"
    deploy.mkdir()
    source_template = Path(__file__).resolve().parents[1] / "deploy" / "config.remote.example.yaml"
    (deploy / "config.remote.example.yaml").write_text(source_template.read_text(encoding="utf-8"), encoding="utf-8")

    written = module.bootstrap_contabo(repo, public_base_url="https://localizer.example.com", admin_username="owner")

    config = yaml.safe_load(Path(written["remote_config"]).read_text(encoding="utf-8"))
    result = check_deploy_config(config)
    assert result["pass"] is True
    assert result["errors"] == 0


def test_worker_tunnel_systemd_unit_is_template_only_and_secret_free():
    path = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "aialra-worker-tunnel.service"
    text = path.read_text(encoding="utf-8")

    assert "cloudflared tunnel" in text
    assert "EnvironmentFile=-/etc/aialra/worker-tunnel.env" in text
    assert "Restart=always" in text
    for forbidden in [
        "WORKER_SHARED_TOKEN",
        "WEBUI_ADMIN_PASSWORD",
        "http://",
        "https://",
        "localizer.example",
        "10.0.",
        "192.168.",
    ]:
        assert forbidden not in text


def test_web_dockerfile_runs_as_non_root_and_stays_secret_free():
    path = Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile.web"
    text = path.read_text(encoding="utf-8")

    assert "useradd --create-home --uid 10001" in text
    assert "chown -R aialra:aialra /app /srv/aialra" in text
    assert "USER 10001:10001" in text
    for forbidden in [
        "WORKER_SHARED_TOKEN",
        "WEBUI_ADMIN_PASSWORD",
        "WEBUI_SESSION_SECRET",
        "WEBUI_DOWNLOAD_SECRET",
    ]:
        assert forbidden not in text
