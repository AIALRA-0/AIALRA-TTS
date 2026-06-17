import json

from ecse_localizer.worker_client import post_worker_heartbeat
from ecse_localizer.worker_health import assess_worker_health


def healthy_payload() -> dict:
    return {
        "worker_id": "worker-1",
        "version": "test",
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "metrics": {
            "cpu": {"load_percent": 20},
            "memory": {"used_percent": 30},
            "gpu": [{"available": True, "util_percent": 40, "memory_used_percent": 25}],
            "local_storage": {"managed_bytes": 100, "total_reported_bytes": 100, "roots": []},
        },
        "capabilities": {
            "asr": {"available": True, "supported_languages": ["auto", "en"]},
            "translation": {"available": True, "supported_target_languages": ["zh-CN"]},
            "tts": {"available": True, "supported_languages": ["zh-CN"]},
        },
    }


def test_worker_health_passes_for_safe_local_payload():
    result = assess_worker_health(healthy_payload(), remote_checked=True, remote_ok=True)

    assert result["pass"] is True
    assert result["errors"] == 0
    assert result["remote"] == {"checked": True, "ok": True}


def test_worker_health_reports_privacy_error_and_missing_gpu_warning():
    payload = healthy_payload()
    payload["privacy"]["allow_cloud_api"] = True
    payload["metrics"]["gpu"] = [{"available": False, "error": "nvidia-smi missing"}]

    result = assess_worker_health(payload, remote_checked=True, remote_ok=False, remote_error_type="ConnectionError")

    codes = {item["code"] for item in result["findings"]}
    assert result["pass"] is False
    assert "cloud_api_not_disabled" in codes
    assert "gpu_not_detected" in codes
    assert "remote_heartbeat_failed" in codes


def test_worker_health_does_not_echo_remote_secret():
    result = assess_worker_health(healthy_payload(), remote_checked=True, remote_ok=False, remote_error_type="HTTPError")

    rendered = json.dumps(result, ensure_ascii=False)
    assert "worker-token" not in rendered
    assert "secret" not in rendered.lower()


def test_post_worker_heartbeat_sends_signed_request_without_plaintext_token(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "worker": {"status": "online"}}

    def fake_post(url, data, headers, timeout):
        captured.update({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return Response()

    monkeypatch.setattr("ecse_localizer.worker_client.requests.post", fake_post)

    result = post_worker_heartbeat("https://remote.example", "worker-token", healthy_payload())

    assert result["ok"] is True
    assert captured["url"] == "https://remote.example/api/worker/heartbeat"
    assert b"worker-1" in captured["data"]
    assert captured["headers"]["X-Worker-Auth"] == "hmac-sha256"
    assert "X-Worker-Timestamp" in captured["headers"]
    assert "X-Worker-Nonce" in captured["headers"]
    assert "X-Worker-Signature" in captured["headers"]
    assert "X-Worker-Token" not in captured["headers"]
    assert "worker-token" not in json.dumps(captured["headers"])
