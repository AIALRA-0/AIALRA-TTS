from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from . import __version__
from .config import load_config
from .job_config import write_job_config
from .metrics import collect_system_metrics
from .utils import PROJECT_ROOT, ensure_dir


def poll_once(
    *,
    remote_base_url: str,
    worker_token: str,
    config: dict[str, Any],
    config_path: str | Path,
    worker_id: str = "local-windows-worker",
    dry_run: bool = False,
) -> dict[str, Any]:
    job = claim_job(remote_base_url, worker_token, worker_id, config)
    if not job:
        return {"ok": True, "claimed": False}
    if dry_run:
        return {"ok": True, "claimed": True, "dry_run": True, "job": job}
    result = run_worker_job(job, remote_base_url, worker_token, config_path, worker_id=worker_id)
    return {"ok": True, "claimed": True, "job_id": job.get("id"), "result": result}


def poll_loop(
    *,
    remote_base_url: str,
    worker_token: str,
    config: dict[str, Any],
    config_path: str | Path,
    worker_id: str = "local-windows-worker",
    interval_seconds: int = 15,
) -> None:
    while True:
        try:
            poll_once(
                remote_base_url=remote_base_url,
                worker_token=worker_token,
                config=config,
                config_path=config_path,
                worker_id=worker_id,
            )
        except Exception as exc:
            print(f"worker poll error: {exc}", file=sys.stderr)
        time.sleep(max(5, interval_seconds))


def claim_job(remote_base_url: str, worker_token: str, worker_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
    payload = {
        "worker_id": worker_id,
        "version": __version__,
        "metrics": collect_system_metrics(config),
    }
    response = requests.post(
        endpoint(remote_base_url, "/api/worker/jobs/claim"),
        json=payload,
        headers=worker_headers(worker_token),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("job")


def run_worker_job(
    job: dict[str, Any],
    remote_base_url: str,
    worker_token: str,
    config_path: str | Path,
    *,
    worker_id: str,
) -> dict[str, Any]:
    job_id = str(job["id"])
    args = worker_args(job)
    if not args:
        raise RuntimeError(f"Worker job {job_id} has no worker_args")
    base_config = load_config(config_path)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    job_config_path = write_job_config(base_config, metadata, job_id=job_id, root=PROJECT_ROOT / "runs" / "worker_job_configs")
    command = [sys.executable, "-m", "ecse_localizer", "--config", str(job_config_path), *args]
    log_dir = ensure_dir(PROJECT_ROOT / "runs" / "worker_jobs")
    log_path = log_dir / f"{safe_name(job_id)}.log"
    worker_cfg = base_config.get("worker", {}) if isinstance(base_config.get("worker"), dict) else {}
    status_interval = max(5, int(worker_cfg.get("status_interval_seconds", 15) or 15))
    log_tail_lines = max(20, min(1000, int(worker_cfg.get("log_tail_lines", 160) or 160)))
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(redacted_command(command)) + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try_post_status(
            remote_base_url,
            worker_token,
            job_id,
            running_status_payload(worker_id, log_path, base_config, pid=proc.pid, command=redacted_command(command), lines=log_tail_lines),
        )
        last_update = time.time()
        while True:
            returncode = proc.poll()
            if returncode is not None:
                break
            if time.time() - last_update >= status_interval:
                log.flush()
                try_post_status(
                    remote_base_url,
                    worker_token,
                    job_id,
                    running_status_payload(worker_id, log_path, base_config, pid=proc.pid, lines=log_tail_lines),
                )
                last_update = time.time()
            time.sleep(1.0)
        log.flush()
    result = extract_json_result(log_path)
    status = "done" if returncode == 0 else "failed"
    log_tail = tail_text(log_path, log_tail_lines)
    final_progress = 100 if returncode == 0 else extract_progress_from_text(log_tail)
    payload = {
        "status": status,
        "returncode": returncode,
        "worker_id": worker_id,
        "log_tail": log_tail,
        "result": summarize_result(result),
        "metrics": collect_system_metrics(base_config),
    }
    if final_progress is not None:
        payload["progress"] = final_progress
    post_status(remote_base_url, worker_token, job_id, payload)
    return {"status": status, "returncode": returncode, "log": str(log_path), "result": summarize_result(result)}


def post_status(remote_base_url: str, worker_token: str, job_id: str, payload: dict[str, Any]) -> None:
    last_error = ""
    for attempt in range(1, 4):
        try:
            response = requests.post(
                endpoint(remote_base_url, f"/api/worker/jobs/{job_id}/status"),
                json=payload,
                headers=worker_headers(worker_token),
                timeout=30,
            )
            response.raise_for_status()
            return
        except Exception as exc:
            last_error = str(exc)
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"Worker status update failed for {job_id}: {last_error}")


def try_post_status(remote_base_url: str, worker_token: str, job_id: str, payload: dict[str, Any]) -> bool:
    try:
        post_status(remote_base_url, worker_token, job_id, payload)
        return True
    except Exception as exc:
        print(f"worker status update warning for {job_id}: {exc}", file=sys.stderr)
        return False


def worker_args(job: dict[str, Any]) -> list[str]:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    args = metadata.get("worker_args")
    if isinstance(args, list):
        return [str(item) for item in args]
    command = job.get("command") if isinstance(job.get("command"), list) else []
    try:
        idx = command.index("ecse_localizer")
    except ValueError:
        return [str(item) for item in command]
    args = command[idx + 1 :]
    if len(args) >= 2 and args[0] == "--config":
        args = args[2:]
    return [str(item) for item in args]


def extract_json_result(log_path: Path) -> dict[str, Any] | None:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder()
    best: dict[str, Any] | None = None
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(value, dict) and any(k in value for k in ("report", "video", "pass", "smoke", "index")):
            best = value
    return best


def summarize_result(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    summary: dict[str, Any] = {}
    for key in ["pass", "smoke", "failed", "processed", "skipped"]:
        if key in result:
            summary[key] = result[key]
    for key in ["report", "video", "hard_sub", "index", "batch_report"]:
        if result.get(key):
            path = Path(str(result[key]))
            summary[key] = path.name
    return summary


def running_status_payload(
    worker_id: str,
    log_path: Path,
    config: dict[str, Any],
    *,
    pid: int | None = None,
    command: list[str] | None = None,
    lines: int = 160,
) -> dict[str, Any]:
    tail = tail_text(log_path, lines) if log_path.exists() else ""
    payload: dict[str, Any] = {
        "status": "running",
        "worker_id": worker_id,
        "log_tail": tail,
        "metrics": collect_system_metrics(config),
    }
    progress = extract_progress_from_text(tail)
    if progress is not None:
        payload["progress"] = progress
    if pid is not None:
        payload["pid"] = pid
    if command:
        payload["command"] = command
    return payload


def extract_progress_from_text(text: str) -> int | None:
    percent_matches = list(
        re.finditer(
            r"(?i)\b(?:progress|processed|complete(?:d)?|overall|total)?\D{0,20}(\d{1,3})(?:\.\d+)?\s*%",
            text or "",
        )
    )
    if percent_matches:
        value = int(percent_matches[-1].group(1))
        return max(0, min(100, value))
    fraction_matches = list(
        re.finditer(
            r"(?i)\b(?:segment|segments|chunk|chunks|file|files|video|videos|processed)\D{0,20}(\d+)\s*/\s*(\d+)\b",
            text or "",
        )
    )
    if fraction_matches:
        done = int(fraction_matches[-1].group(1))
        total = int(fraction_matches[-1].group(2))
        if total > 0:
            return max(0, min(100, round(done * 100 / total)))
    return None


def redacted_command(command: list[str]) -> list[str]:
    redacted = []
    skip_next = False
    for item in command:
        if skip_next:
            redacted.append("<local-config>")
            skip_next = False
            continue
        redacted.append(item)
        if item == "--config":
            skip_next = True
    return redacted


def worker_headers(worker_token: str) -> dict[str, str]:
    return {"X-Worker-Token": worker_token}


def endpoint(remote_base_url: str, path: str) -> str:
    return remote_base_url.rstrip("/") + path


def tail_text(path: Path, lines: int) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:120]
