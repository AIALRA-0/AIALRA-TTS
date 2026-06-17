import json
from pathlib import Path

import yaml

from ecse_localizer.cli import main


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
        "webui": {
            "platform_dir": str(tmp_path / "platform"),
            "job_dir": str(tmp_path / "jobs"),
            "upload_dir": str(tmp_path / "uploads"),
        },
    }
    Path(config["input_dir"]).mkdir()
    Path(config["output_dir"]).mkdir()
    Path(config["work_dir"]).mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


def test_worker_once_sends_heartbeat_then_polls(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_build_payload(config, *, worker_id):
        calls.append(("build", worker_id))
        return {"worker_id": worker_id, "privacy": config["privacy"], "metrics": {}, "capabilities": {}}

    def fake_heartbeat(remote_base_url, worker_token, payload):
        calls.append(("heartbeat", remote_base_url, worker_token, payload["worker_id"]))
        return {"ok": True, "worker": {"status": "online"}}

    def fake_poll_once(**kwargs):
        calls.append(("poll", kwargs["remote_base_url"], kwargs["worker_token"], kwargs["worker_id"], kwargs["dry_run"]))
        return {"ok": True, "claimed": False}

    monkeypatch.setattr("ecse_localizer.cli.build_worker_status_payload", fake_build_payload)
    monkeypatch.setattr("ecse_localizer.cli.post_worker_heartbeat", fake_heartbeat)
    monkeypatch.setattr("ecse_localizer.cli.poll_once", fake_poll_once)

    rc = main(
        [
            "--config",
            str(write_config(tmp_path)),
            "worker",
            "--remote-base-url",
            "https://remote.example",
            "--worker-token",
            "worker-token",
            "--worker-id",
            "worker-1",
            "--once",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert rc == 0
    assert payload["heartbeat"]["ok"] is True
    assert payload["poll"] == {"ok": True, "claimed": False}
    assert calls == [
        ("build", "worker-1"),
        ("heartbeat", "https://remote.example", "worker-token", "worker-1"),
        ("poll", "https://remote.example", "worker-token", "worker-1", True),
    ]
    assert "worker-token" not in output


def test_worker_once_can_skip_heartbeat(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_heartbeat(*args, **kwargs):
        raise AssertionError("heartbeat should be skipped")

    def fake_poll_once(**kwargs):
        calls.append(("poll", kwargs["worker_id"], kwargs["dry_run"]))
        return {"ok": True, "claimed": False}

    monkeypatch.setattr("ecse_localizer.cli.post_worker_heartbeat", fake_heartbeat)
    monkeypatch.setattr("ecse_localizer.cli.poll_once", fake_poll_once)

    rc = main(
        [
            "--config",
            str(write_config(tmp_path)),
            "worker",
            "--remote-base-url",
            "https://remote.example",
            "--worker-token",
            "worker-token",
            "--worker-id",
            "worker-1",
            "--once",
            "--dry-run",
            "--no-heartbeat",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["heartbeat"] == {"sent": False, "skipped": True}
    assert calls == [("poll", "worker-1", True)]


def test_worker_once_reads_remote_values_from_environment(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_build_payload(config, *, worker_id):
        return {"worker_id": worker_id, "privacy": config["privacy"], "metrics": {}, "capabilities": {}}

    def fake_heartbeat(remote_base_url, worker_token, payload):
        calls.append(("heartbeat", remote_base_url, worker_token, payload["worker_id"]))
        return {"ok": True, "worker": {"status": "online"}}

    def fake_poll_once(**kwargs):
        calls.append(("poll", kwargs["remote_base_url"], kwargs["worker_token"], kwargs["worker_id"], kwargs["dry_run"]))
        return {"ok": True, "claimed": False}

    monkeypatch.setenv("REMOTE_PUBLIC_BASE_URL", "https://remote.example")
    monkeypatch.setenv("WORKER_SHARED_TOKEN", "worker-token")
    monkeypatch.setattr("ecse_localizer.cli.build_worker_status_payload", fake_build_payload)
    monkeypatch.setattr("ecse_localizer.cli.post_worker_heartbeat", fake_heartbeat)
    monkeypatch.setattr("ecse_localizer.cli.poll_once", fake_poll_once)

    rc = main(
        [
            "--config",
            str(write_config(tmp_path)),
            "worker",
            "--worker-id",
            "worker-1",
            "--once",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert calls == [
        ("heartbeat", "https://remote.example", "worker-token", "worker-1"),
        ("poll", "https://remote.example", "worker-token", "worker-1", True),
    ]
    assert "worker-token" not in output


def test_worker_local_check_does_not_require_remote_or_token(monkeypatch, tmp_path, capsys):
    def fake_build_payload(config, *, worker_id):
        return {
            "worker_id": worker_id,
            "version": "test",
            "privacy": config["privacy"],
            "metrics": {
                "cpu": {"load_percent": 10},
                "memory": {"used_percent": 20},
                "gpu": [{"available": True}],
                "local_storage": {"managed_bytes": 0, "total_reported_bytes": 0, "roots": []},
            },
            "capabilities": {
                "asr": {"available": True, "supported_languages": ["auto", "en"]},
                "translation": {"available": True, "supported_target_languages": ["zh-CN"]},
                "tts": {"available": True, "supported_languages": ["zh-CN"]},
            },
        }

    def fake_heartbeat(*args, **kwargs):
        raise AssertionError("local check must not send heartbeat")

    def fake_poll_once(**kwargs):
        raise AssertionError("local check must not poll remote")

    monkeypatch.setattr("ecse_localizer.cli.build_worker_health_payload", fake_build_payload)
    monkeypatch.setattr("ecse_localizer.cli.post_worker_heartbeat", fake_heartbeat)
    monkeypatch.setattr("ecse_localizer.cli.poll_once", fake_poll_once)

    rc = main(["--config", str(write_config(tmp_path)), "worker", "--local-check"])

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert rc == 0
    assert payload["mode"] == "worker_local_check"
    assert payload["health"]["pass"] is True
    assert "worker-token" not in output
    assert "C:\\Users" not in output


def test_worker_remote_args_required_without_local_check(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("REMOTE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("WORKER_SHARED_TOKEN", raising=False)

    rc = main(["--config", str(write_config(tmp_path)), "worker", "--once", "--dry-run"])

    assert rc == 1
    assert "requires --remote-base-url" in capsys.readouterr().err


def test_worker_poll_reads_remote_values_from_environment(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_poll_once(**kwargs):
        calls.append((kwargs["remote_base_url"], kwargs["worker_token"], kwargs["worker_id"], kwargs["dry_run"]))
        return {"ok": True, "claimed": False}

    monkeypatch.setenv("REMOTE_PUBLIC_BASE_URL", "https://remote.example")
    monkeypatch.setenv("WORKER_SHARED_TOKEN", "worker-token")
    monkeypatch.setattr("ecse_localizer.cli.poll_once", fake_poll_once)

    rc = main(
        [
            "--config",
            str(write_config(tmp_path)),
            "worker-poll",
            "--worker-id",
            "worker-1",
            "--once",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert calls == [("https://remote.example", "worker-token", "worker-1", True)]
    assert "worker-token" not in output
