import json
from pathlib import Path

import yaml

from ecse_localizer.cli import main
from ecse_localizer.remote_smoke import run_remote_smoke


def config(tmp_path: Path) -> dict:
    return {
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "translation": {"quality_mode": "best_quality"},
    }


def test_remote_smoke_validates_worker_queue_flow(tmp_path):
    result = run_remote_smoke(config(tmp_path), output_dir=tmp_path / "remote-smoke")

    assert result["pass"] is True
    names = [step["name"] for step in result["steps"]]
    assert "worker starts offline" in names
    assert "worker heartbeat online" in names
    assert "queued job claimed" in names
    assert "running status redacted" in names
    assert "stale running job requeued" in names
    assert "restored worker claims retry" in names
    assert "worker becomes offline after stale heartbeat" in names
    assert Path(result["json"]).exists()
    assert Path(result["markdown"]).exists()
    assert "private" not in json.dumps(result, ensure_ascii=False).lower()
    assert "secret" not in json.dumps(result, ensure_ascii=False).lower()


def test_remote_smoke_cli_writes_report(tmp_path, capsys):
    cfg = config(tmp_path)
    Path(cfg["input_dir"]).mkdir()
    Path(cfg["output_dir"]).mkdir()
    Path(cfg["work_dir"]).mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"

    rc = main(["--config", str(config_path), "remote-smoke", "--output", str(out)])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["pass"] is True
    assert payload["summary"]["failed_steps"] == []
    assert Path(payload["json"]).exists()
