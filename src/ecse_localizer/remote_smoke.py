from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import save_config
from .utils import ensure_dir, read_json, write_json
from .webui import (
    WebState,
    claim_worker_job,
    create_job_record,
    read_job,
    requeue_stale_worker_jobs,
    update_job,
    worker_status_changes,
    worker_status_payload,
)


def run_remote_smoke(base_config: dict[str, Any], *, output_dir: str | Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aialra_remote_smoke_") as tmp:
        root = Path(tmp)
        config = remote_smoke_config(base_config, root)
        config_path = root / "config.yaml"
        save_config(config_path, config)
        state = WebState(config_path)
        steps: list[dict[str, Any]] = []

        initial = worker_status_payload(state)
        steps.append(check("worker starts offline", initial["worker_required"] is True and initial["available"] is False and initial["heartbeat_online"] is False))

        heartbeat = state.store.record_worker_heartbeat(worker_payload("worker-1"))
        online = worker_status_payload(state)
        steps.append(check("worker heartbeat online", heartbeat["status"] == "online" and online["available"] is True and online["heartbeat_online"] is True))

        job = create_job_record(
            state,
            "audit",
            "Remote smoke audit",
            ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "worker-ref:demo"],
            user="admin",
            metadata={"worker_args": ["audit", "--input", "worker-ref:demo"]},
            dispatch_target="worker",
        )
        claimed = claim_worker_job(state, "worker-1")
        steps.append(check("queued job claimed", bool(claimed) and claimed["id"] == job["id"] and claimed["status"] == "claimed"))

        update_job(
            state,
            job["id"],
            worker_status_changes(
                {
                    "status": "running",
                    "worker_id": "worker-1",
                    "progress": 35,
                    "log_tail": r"processing C:\private\lecture.mp4 with token=secret",
                    "metrics": {"disk": {"path": r"C:\private\output", "used_percent": 50}},
                }
            ),
        )
        running = read_job(state, job["id"]) or {}
        serialized_running = json.dumps(running, ensure_ascii=False)
        steps.append(
            check(
                "running status redacted",
                running.get("status") == "running"
                and running.get("progress") == 35
                and "private" not in serialized_running.lower()
                and "secret" not in serialized_running.lower(),
            )
        )

        update_job(state, job["id"], worker_status_changes({"status": "done", "worker_id": "worker-1", "returncode": 0, "progress": 100}))
        done = read_job(state, job["id"]) or {}
        steps.append(check("job done", done.get("status") == "done" and done.get("returncode") == 0))

        stale_job = create_job_record(
            state,
            "audit",
            "Remote stale audit",
            ["python", "-m", "ecse_localizer", "--config", "remote.yaml", "audit", "--input", "worker-ref:stale"],
            user="admin",
            metadata={"worker_args": ["audit", "--input", "worker-ref:stale"]},
            dispatch_target="worker",
        )
        stale_claimed = claim_worker_job(state, "worker-old")
        steps.append(check("second job claimed", bool(stale_claimed) and stale_claimed["id"] == stale_job["id"]))
        update_job(state, stale_job["id"], worker_status_changes({"status": "running", "worker_id": "worker-old", "progress": 10}))
        make_job_file_stale(state, stale_job["id"], seconds=120)
        changed = requeue_stale_worker_jobs(state)
        retrying = read_job(state, stale_job["id"]) or {}
        steps.append(check("stale running job requeued", bool(changed) and retrying.get("status") == "retrying" and retrying.get("retry_count") == 1))
        reclaimed = claim_worker_job(state, "worker-restored")
        steps.append(check("restored worker claims retry", bool(reclaimed) and reclaimed["id"] == stale_job["id"] and reclaimed["claimed_by"] == "worker-restored"))

        mark_worker_stale(state, seconds=90)
        offline = worker_status_payload(state)
        steps.append(check("worker becomes offline after stale heartbeat", offline["heartbeat_online"] is False and offline["status"] == "offline"))

        result = {
            "pass": all(step["pass"] for step in steps),
            "mode": "remote_worker_queue_smoke",
            "steps": steps,
            "summary": {
                "checked_steps": len(steps),
                "failed_steps": [step["name"] for step in steps if not step["pass"]],
            },
        }

    if output_dir:
        out = ensure_dir(output_dir)
        json_path = out / "remote_smoke_report.json"
        md_path = out / "remote_smoke_report.md"
        write_json(json_path, result)
        md_path.write_text(render_remote_smoke_markdown(result), encoding="utf-8")
        result["json"] = str(json_path)
        result["markdown"] = str(md_path)
    return result


def remote_smoke_config(base_config: dict[str, Any], root: Path) -> dict[str, Any]:
    return {
        "input_dir": str(root / "no-local-media"),
        "output_dir": str(root / "previews"),
        "work_dir": str(root / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
        "translation": dict(base_config.get("translation", {}) if isinstance(base_config.get("translation"), dict) else {}),
        "webui": {
            "enabled": True,
            "execution_mode": "worker_queue",
            "platform_dir": str(root / "platform"),
            "job_dir": str(root / "jobs"),
            "upload_dir": str(root / "uploads"),
            "preview_dir": str(root / "previews" / "cache"),
            "preview_manifest": str(root / "previews" / "cache" / "preview_manifest.json"),
            "username": "admin",
            "password": "local-password",
            "session_secret": "remote-smoke-session-secret",
            "worker_token": "remote-smoke-worker-token",
            "worker_auth_mode": "hmac",
            "worker_require_nonce": True,
            "worker_requeue_stale_jobs": True,
            "worker_job_heartbeat_timeout_seconds": 30,
            "worker_job_max_auto_retries": 2,
            "worker_offline_after_seconds": 45,
            "default_local_quota_gb": 1,
            "default_remote_quota_gb": 1,
            "default_project_quota_gb": 1,
        },
    }


def worker_payload(worker_id: str) -> dict[str, Any]:
    return {
        "status": "online",
        "worker_id": worker_id,
        "version": "remote-smoke",
        "message": "remote smoke heartbeat",
        "metrics": {
            "cpu": {"load_percent": 12},
            "memory": {"used_percent": 34},
            "gpu": [{"available": True, "util_percent": 55, "memory_used_percent": 44}],
            "disk": {"path": r"C:\private\worker-output", "used_percent": 51},
            "local_storage": {"managed_bytes": 1024, "total_reported_bytes": 2048, "roots": []},
        },
        "capabilities": {
            "asr": {"available": True, "supported_languages": ["auto", "en"]},
            "translation": {"available": True, "supported_target_languages": ["zh-CN"]},
            "tts": {"available": True, "supported_languages": ["zh-CN"]},
        },
        "media_refs": [
            {
                "ref_id": "demo",
                "name": r"C:\private\lecture.mp4",
                "path": r"C:\private\lecture.mp4",
                "size": 100,
                "media_type": "video/mp4",
            }
        ],
    }


def check(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "pass": bool(passed)}


def make_job_file_stale(state: WebState, job_id: str, *, seconds: int) -> None:
    path = state.job_dir / f"{job_id}.json"
    old = time.time() - seconds
    os.utime(path, (old, old))


def mark_worker_stale(state: WebState, *, seconds: int) -> None:
    row = state.store.worker_status()
    row["updated_at_epoch"] = int(time.time()) - seconds
    write_json(state.store.worker_path, row)


def render_remote_smoke_markdown(result: dict[str, Any]) -> str:
    lines = ["# Remote Worker Queue Smoke", "", f"Status: {'PASS' if result.get('pass') else 'FAIL'}", "", "## Steps", ""]
    for step in result.get("steps", []):
        lines.append(f"- {'PASS' if step.get('pass') else 'FAIL'}: {step.get('name')}")
    return "\n".join(lines).rstrip() + "\n"
