from __future__ import annotations

import json
import hashlib
import hmac
import mimetypes
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
from .utils import PROJECT_ROOT, ensure_dir, run_cmd


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
    path = "/api/worker/jobs/claim"
    payload = {
        "worker_id": worker_id,
        "version": __version__,
        "metrics": collect_system_metrics(config),
    }
    body = canonical_json(payload)
    response = requests.post(
        endpoint(remote_base_url, path),
        data=body.encode("utf-8"),
        headers=worker_headers(worker_token, path=path, body=body),
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
    preview_summary: dict[str, Any] = {}
    if status == "done":
        preview_summary = try_create_and_upload_preview(result, job, remote_base_url, worker_token, base_config, worker_id=worker_id)
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
    if preview_summary:
        payload["preview"] = preview_summary
    if final_progress is not None:
        payload["progress"] = final_progress
    post_status(remote_base_url, worker_token, job_id, payload)
    return {"status": status, "returncode": returncode, "log": str(log_path), "result": summarize_result(result), "preview": preview_summary}


def try_create_and_upload_preview(
    result: dict[str, Any] | None,
    job: dict[str, Any],
    remote_base_url: str,
    worker_token: str,
    config: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    try:
        return create_and_upload_preview(result, job, remote_base_url, worker_token, config, worker_id=worker_id)
    except Exception as exc:
        print(f"worker preview upload warning for {job.get('id')}: {exc}", file=sys.stderr)
        return {}


def create_and_upload_preview(
    result: dict[str, Any] | None,
    job: dict[str, Any],
    remote_base_url: str,
    worker_token: str,
    config: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    worker_cfg = config.get("worker", {}) if isinstance(config.get("worker"), dict) else {}
    if not bool(worker_cfg.get("upload_previews", True)):
        return {}
    source = preview_source_path(result)
    if not source or not source.exists():
        return {}
    job_id = str(job.get("id") or "worker_job")
    preview_root = ensure_dir(PROJECT_ROOT / "runs" / "worker_previews" / safe_name(job_id))
    stem = safe_name(source.stem) or "preview"
    preview_path = preview_root / f"{stem}_preview.mp4"
    thumbnail_path = preview_root / f"{stem}_thumb.jpg"
    generate_preview_files(source, preview_path, thumbnail_path, config)
    source_key = str(worker_cfg.get("preview_source_output_key") or "zh_dub_mp4")
    preview_id = f"{safe_name(job_id)}_{source_key}"
    preview_row = upload_worker_preview(
        remote_base_url,
        worker_token,
        job_id,
        preview_path,
        variant="preview",
        preview_id=preview_id,
        display_name=source.name,
        source_output_key=source_key,
        worker_id=worker_id,
    )
    thumbnail_row = {}
    if thumbnail_path.exists():
        thumbnail_row = upload_worker_preview(
            remote_base_url,
            worker_token,
            job_id,
            thumbnail_path,
            variant="thumbnail",
            preview_id=preview_id,
            display_name=source.name,
            source_output_key=source_key,
            worker_id=worker_id,
        )
    return {
        "uploaded": True,
        "preview": preview_row.get("preview", preview_row),
        "thumbnail": thumbnail_row.get("preview", thumbnail_row) if thumbnail_row else {},
    }


def preview_source_path(result: dict[str, Any] | None) -> Path | None:
    if not result:
        return None
    for key in ("video", "hard_sub"):
        value = result.get(key)
        if value:
            path = Path(str(value))
            if path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".m4v"}:
                return path
    return None


def generate_preview_files(source: Path, preview_path: Path, thumbnail_path: Path, config: dict[str, Any]) -> None:
    worker_cfg = config.get("worker", {}) if isinstance(config.get("worker"), dict) else {}
    max_width = int(worker_cfg.get("preview_max_width", 854) or 854)
    video_bitrate = str(worker_cfg.get("preview_video_bitrate", "700k") or "700k")
    audio_bitrate = str(worker_cfg.get("preview_audio_bitrate", "96k") or "96k")
    max_seconds = float(worker_cfg.get("preview_max_seconds", 0) or 0)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
    ]
    if max_seconds > 0:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += [
        "-vf",
        f"scale='min({max_width},iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        video_bitrate,
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        str(preview_path),
    ]
    run_cmd(cmd)
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(worker_cfg.get("thumbnail_at_seconds", 1.0) or 1.0),
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-1",
            str(thumbnail_path),
        ]
    )


def upload_worker_preview(
    remote_base_url: str,
    worker_token: str,
    job_id: str,
    file_path: Path,
    *,
    variant: str,
    preview_id: str,
    display_name: str,
    source_output_key: str,
    worker_id: str,
) -> dict[str, Any]:
    path = f"/api/worker/jobs/{job_id}/preview"
    body = file_path.read_bytes()
    headers = worker_headers(worker_token, path=path, body=body)
    headers["Content-Type"] = media_type_for(file_path)
    headers["X-Worker-Preview-Variant"] = variant
    headers["X-Worker-Preview-Id"] = preview_id
    headers["X-Worker-Preview-Name"] = display_name
    headers["X-Worker-Preview-File-Name"] = file_path.name
    headers["X-Worker-Preview-Source-Key"] = source_output_key
    headers["X-Worker-Id"] = worker_id
    response = requests.post(endpoint(remote_base_url, path), data=body, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()


def media_type_for(file_path: Path) -> str:
    return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def post_status(remote_base_url: str, worker_token: str, job_id: str, payload: dict[str, Any]) -> None:
    path = f"/api/worker/jobs/{job_id}/status"
    body = canonical_json(payload)
    last_error = ""
    for attempt in range(1, 4):
        try:
            response = requests.post(
                endpoint(remote_base_url, path),
                data=body.encode("utf-8"),
                headers=worker_headers(worker_token, path=path, body=body),
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


def worker_headers(worker_token: str, *, path: str | None = None, body: str | bytes = b"", method: str = "POST") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if path:
        timestamp = str(int(time.time()))
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        headers.update(
            {
                "X-Worker-Auth": "hmac-sha256",
                "X-Worker-Timestamp": timestamp,
                "X-Worker-Signature": worker_signature(worker_token, timestamp=timestamp, method=method, path=path, body=body_bytes),
            }
        )
    else:
        headers["X-Worker-Token"] = worker_token
    return headers


def worker_signature(worker_token: str, *, timestamp: str, method: str, path: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    message = "\n".join([str(timestamp), method.upper(), path, body_hash]).encode("utf-8")
    return hmac.new(worker_token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def endpoint(remote_base_url: str, path: str) -> str:
    return remote_base_url.rstrip("/") + path


def tail_text(path: Path, lines: int) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:120]
