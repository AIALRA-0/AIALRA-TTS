import json
from pathlib import Path

from ecse_localizer.cli import main
from ecse_localizer.deploy_check import check_deploy_config


def valid_remote_config() -> dict:
    return {
        "project_root": r"C:\build\repo",
        "input_dir": "/srv/aialra/no-local-media",
        "output_dir": "/srv/aialra/previews",
        "work_dir": "/srv/aialra/runs",
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "asr": {"supported_languages": ["auto", "en", "zh-CN"]},
        "translation": {"supported_target_languages": ["zh-CN", "en"], "allow_unlisted_targets": True},
        "tts": {"supported_languages": ["zh-CN", "yue"]},
        "webui": {
            "enabled": True,
            "host": "0.0.0.0",
            "execution_mode": "worker_queue",
            "cookie_secure": True,
            "csrf_origin_check": True,
            "allow_remote_media_uploads": False,
            "allow_worker_path_submission": False,
            "bind_local_only": False,
            "upload_dir": "/srv/aialra/previews/uploads",
            "preview_dir": "/srv/aialra/previews/cache",
            "job_dir": "/srv/aialra/runs/webui_jobs",
            "platform_dir": "/srv/aialra/platform",
            "session_secret": "session-secret-value-000000000000000000",
            "download_secret": "download-secret-value-00000000000000000",
            "worker_token": "worker-hmac-secret-value-00000000000000",
            "password": "strong-admin-password",
            "worker_auth_mode": "hmac",
            "worker_require_nonce": True,
            "signed_url_ttl_seconds": 900,
            "worker_signature_max_skew_seconds": 300,
            "worker_offline_after_seconds": 180,
            "cleanup_older_than_days": 7,
            "global_remote_quota_gb": 50,
            "default_remote_quota_gb": 10,
            "worker_preview_max_upload_mb": 256,
            "worker_artifact_cache_max_upload_mb": 2048,
            "max_active_jobs_per_user": 2,
            "max_active_jobs_global": 8,
        },
    }


def test_deploy_check_accepts_hardened_remote_config():
    result = check_deploy_config(valid_remote_config())

    assert result["pass"] is True
    assert result["errors"] == 0


def test_reverse_proxy_examples_keep_sse_unbuffered():
    root = Path(__file__).parents[1]
    nginx = (root / "deploy" / "nginx.conf.example").read_text(encoding="utf-8")
    caddy = (root / "deploy" / "Caddyfile.example").read_text(encoding="utf-8")
    compose = (root / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    prompt = (root / "DEPLOY_CONTABO_PROMPT.md").read_text(encoding="utf-8")

    assert "location /api/events" in nginx
    assert "proxy_buffering off;" in nginx
    assert "proxy_cache off;" in nginx
    assert 'add_header X-Accel-Buffering "no" always;' in nginx
    assert "handle /api/events" in caddy
    assert "flush_interval -1" in caddy
    assert "encode @notEvents zstd gzip" in caddy
    assert "healthcheck:" in compose
    assert "http://127.0.0.1:7861/readyz" in compose
    assert "dedicated `/api/events` reverse-proxy rule" in prompt
    assert "Use unauthenticated `/healthz`" in prompt


def test_deploy_check_rejects_placeholders_and_unsafe_remote_mode():
    config = valid_remote_config()
    config["privacy"]["allow_cloud_api"] = True
    config["webui"]["execution_mode"] = "local_subprocess"
    config["webui"]["allow_remote_media_uploads"] = True
    config["webui"]["allow_worker_path_submission"] = True
    config["webui"]["worker_auth_mode"] = "hmac_or_token"
    config["webui"]["worker_require_nonce"] = False
    config["webui"]["cookie_secure"] = False
    config["webui"]["csrf_origin_check"] = False
    config["webui"]["session_secret"] = "${WEBUI_SESSION_SECRET}"
    config["webui"]["worker_token"] = "change-me-token"
    private_ip = ".".join(["10", "0", "0", "5"])
    config["worker"] = {"tunnel_endpoint": f"https://{private_ip}"}

    result = check_deploy_config(config)

    codes = {item["code"] for item in result["findings"]}
    assert result["pass"] is False
    assert "must_be_false" in codes
    assert "worker_queue_required" in codes
    assert "remote_media_uploads_enabled" in codes
    assert "worker_path_submission_enabled" in codes
    assert "weak_worker_auth" in codes
    assert "must_be_true" in codes
    assert "secret_placeholder" in codes
    assert "private_ip_in_remote_config" in codes


def test_deploy_check_does_not_echo_secret_values():
    config = valid_remote_config()
    config["webui"]["download_secret"] = config["webui"]["session_secret"]

    result = check_deploy_config(config)

    rendered = json.dumps(result, ensure_ascii=False)
    assert "session-secret-value-000000000000000000" not in rendered
    assert "download-secret-value-00000000000000000" not in rendered
    assert "worker-hmac-secret-value-00000000000000" not in rendered
    assert "secret_reused" in {item["code"] for item in result["findings"]}


def test_deploy_check_warns_when_worker_offline_threshold_is_too_low():
    config = valid_remote_config()
    config["webui"]["worker_offline_after_seconds"] = 45

    result = check_deploy_config(config)

    findings = {(item["path"], item["code"]) for item in result["findings"]}
    assert result["pass"] is True
    assert ("webui.worker_offline_after_seconds", "number_below_recommended") in findings


def test_deploy_check_accepts_https_remote_public_base_url():
    result = check_deploy_config(valid_remote_config(), env={"REMOTE_PUBLIC_BASE_URL": "https://localizer.example.com"})

    assert result["pass"] is True
    assert result["errors"] == 0


def test_deploy_check_rejects_unsafe_remote_public_base_url():
    private_ip = ".".join(["10", "0", "0", "5"])
    bad_urls = [
        "http://localizer.example.com",
        "https://localhost:7861",
        "https://127.0.0.1:7861",
        f"https://{private_ip}",
    ]

    for url in bad_urls:
        result = check_deploy_config(valid_remote_config(), env={"REMOTE_PUBLIC_BASE_URL": url})
        codes = {item["code"] for item in result["findings"]}
        assert result["pass"] is False
        assert codes & {"remote_public_base_url_invalid", "remote_public_base_url_not_public"}


def test_deploy_check_rejects_cloud_or_nonlocal_inference_endpoints():
    private_ip = ".".join(["192", "168", "1", "55"])
    config = valid_remote_config()
    config["llm"] = {"endpoint": "https://api.openai.com/v1"}
    config["tts"] = {"supported_languages": ["zh-CN"], "server_url": "https://api.elevenlabs.io/v1"}
    config["asr"] = {"supported_languages": ["auto", "en"], "endpoint": f"http://{private_ip}:9000/asr"}

    result = check_deploy_config(config)

    codes = {item["code"] for item in result["findings"]}
    assert result["pass"] is False
    assert "cloud_inference_endpoint" in codes
    assert "non_local_inference_endpoint" in codes


def test_deploy_check_warns_for_loopback_inference_endpoint_without_failing():
    config = valid_remote_config()
    config["llm"] = {"endpoint": "http://127.0.0.1:11434/v1"}

    result = check_deploy_config(config)

    findings = {(item["path"], item["code"]) for item in result["findings"]}
    assert result["pass"] is True
    assert ("llm.endpoint", "local_inference_endpoint_in_remote_config") in findings


def test_deploy_check_cli_returns_nonzero_for_unsafe_config(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
input_dir: "/srv/aialra/no-local-media"
output_dir: "/srv/aialra/previews"
work_dir: "/srv/aialra/runs"
privacy:
  allow_cloud_api: false
  allow_upload_media: false
  allow_voice_clone_without_consent: false
asr:
  supported_languages: ["auto", "en"]
translation:
  supported_target_languages: ["zh-CN"]
tts:
  supported_languages: ["zh-CN"]
webui:
  enabled: true
  host: "0.0.0.0"
  execution_mode: "worker_queue"
  cookie_secure: true
  csrf_origin_check: true
  allow_remote_media_uploads: false
  bind_local_only: false
  upload_dir: "/srv/aialra/previews/uploads"
  preview_dir: "/srv/aialra/previews/cache"
  job_dir: "/srv/aialra/runs/webui_jobs"
  platform_dir: "/srv/aialra/platform"
  session_secret: "change-me-session-secret"
  download_secret: "download-secret-value-00000000000000000"
  worker_token: "worker-hmac-secret-value-00000000000000"
  password: "strong-admin-password"
  worker_auth_mode: "hmac"
  worker_require_nonce: true
  signed_url_ttl_seconds: 900
  worker_signature_max_skew_seconds: 300
  worker_offline_after_seconds: 180
  cleanup_older_than_days: 7
  global_remote_quota_gb: 50
  default_remote_quota_gb: 10
  worker_preview_max_upload_mb: 256
  worker_artifact_cache_max_upload_mb: 2048
  max_active_jobs_per_user: 2
  max_active_jobs_global: 8
""",
        encoding="utf-8",
    )

    rc = main(["--config", str(path), "deploy-check"])

    out = capsys.readouterr().out
    assert rc == 2
    assert "secret_placeholder" in out
    assert "change-me-session-secret" not in out
