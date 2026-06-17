from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import hashlib
import hmac
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from . import __version__
from .artifacts import DOWNLOADABLE_OUTPUTS
from .capabilities import language_capabilities
from .config import load_config
from .job_config import write_job_config
from .llm_local import LocalLLMClient
from .metrics import collect_system_metrics
from .redaction import sanitize_remote_command, sanitize_remote_text
from .scan import VIDEO_SUFFIXES, should_skip
from .tts import tts_health
from .utils import PROJECT_ROOT, ensure_dir, read_json, run_cmd, write_json


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
    if worker_action(job):
        result = run_worker_action(job, remote_base_url, worker_token, config_path, worker_id=worker_id)
    else:
        result = run_worker_job(job, remote_base_url, worker_token, config_path, worker_id=worker_id)
    return {"ok": True, "claimed": True, "job_id": job.get("id"), "result": result}


def poll_concurrent_once(
    *,
    remote_base_url: str,
    worker_token: str,
    config: dict[str, Any],
    config_path: str | Path,
    worker_id: str = "local-windows-worker",
    max_concurrent_jobs: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    max_jobs = worker_concurrency(config, max_concurrent_jobs)
    if dry_run or max_jobs <= 1:
        result = poll_once(
            remote_base_url=remote_base_url,
            worker_token=worker_token,
            config=config,
            config_path=config_path,
            worker_id=worker_id,
            dry_run=dry_run,
        )
        return {
            "ok": bool(result.get("ok", True)),
            "max_concurrent_jobs": 1,
            "claimed_count": 1 if result.get("claimed") else 0,
            "results": [result],
        }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="ecse-worker") as executor:
        futures = {
            executor.submit(
                poll_once,
                remote_base_url=remote_base_url,
                worker_token=worker_token,
                config=config,
                config_path=config_path,
                worker_id=worker_slot_id(worker_id, slot, max_jobs),
                dry_run=False,
            ): worker_slot_id(worker_id, slot, max_jobs)
            for slot in range(max_jobs)
        }
        for future in as_completed(futures):
            slot_id = futures[future]
            try:
                result = future.result()
                result.setdefault("worker_id", slot_id)
                results.append(result)
            except Exception as exc:
                results.append({"ok": False, "claimed": False, "worker_id": slot_id, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "ok": not any(not item.get("ok", False) for item in results),
        "max_concurrent_jobs": max_jobs,
        "claimed_count": sum(1 for item in results if item.get("claimed")),
        "results": sorted(results, key=lambda item: str(item.get("worker_id") or "")),
    }


def poll_loop(
    *,
    remote_base_url: str,
    worker_token: str,
    config: dict[str, Any],
    config_path: str | Path,
    worker_id: str = "local-windows-worker",
    interval_seconds: int = 15,
    max_concurrent_jobs: int | None = None,
) -> None:
    while True:
        try:
            poll_concurrent_once(
                remote_base_url=remote_base_url,
                worker_token=worker_token,
                config=config,
                config_path=config_path,
                worker_id=worker_id,
                max_concurrent_jobs=max_concurrent_jobs,
            )
        except Exception as exc:
            print(f"worker poll error: {exc}", file=sys.stderr)
        time.sleep(max(5, interval_seconds))


def worker_concurrency(config: dict[str, Any], override: int | None = None) -> int:
    raw = override
    if raw is None:
        worker_cfg = config.get("worker", {}) if isinstance(config.get("worker"), dict) else {}
        raw = worker_cfg.get("max_concurrent_jobs", 1)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(8, value))


def worker_slot_id(worker_id: str, slot: int, max_jobs: int) -> str:
    base = str(worker_id or "local-windows-worker")
    if max_jobs <= 1:
        return base
    return f"{base}-{slot + 1}"


def claim_job(remote_base_url: str, worker_token: str, worker_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
    path = "/api/worker/jobs/claim"
    payload = {
        "worker_id": worker_id,
        "version": __version__,
        "max_concurrent_jobs": worker_concurrency(config),
        "metrics": collect_system_metrics(config),
        "media_refs": collect_worker_media_refs(config),
        "capabilities": worker_language_capabilities(config),
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


def post_worker_heartbeat(remote_base_url: str, worker_token: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    path = "/api/worker/heartbeat"
    body = canonical_json(payload)
    response = requests.post(
        endpoint(remote_base_url, path),
        data=body.encode("utf-8"),
        headers=worker_headers(worker_token, path=path, body=body),
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def run_worker_job(
    job: dict[str, Any],
    remote_base_url: str,
    worker_token: str,
    config_path: str | Path,
    *,
    worker_id: str,
) -> dict[str, Any]:
    job_id = str(job["id"])
    args = resolve_worker_media_args(worker_args(job))
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
        cancelled_by_control = False
        cancel_message = ""
        while True:
            returncode = proc.poll()
            if returncode is not None:
                break
            if time.time() - last_update >= status_interval:
                log.flush()
                control = try_get_worker_control(remote_base_url, worker_token, job_id, worker_id=worker_id)
                if worker_control_cancel_requested(control):
                    cancelled_by_control = True
                    cancel_message = "Cancelled by remote request"
                    log.write(f"\n{cancel_message}\n")
                    log.flush()
                    returncode = terminate_process_tree(proc)
                    break
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
    status = "cancelled" if cancelled_by_control else "done" if returncode == 0 else "failed"
    final_returncode = -9 if cancelled_by_control else returncode
    worker_artifacts: list[dict[str, Any]] = []
    if status == "done":
        worker_artifacts = register_worker_artifacts(result, job)
    preview_summary: dict[str, Any] = {}
    if status == "done":
        preview_summary = try_create_and_upload_preview(result, job, remote_base_url, worker_token, base_config, worker_id=worker_id)
    log_tail = sanitize_remote_text(tail_text(log_path, log_tail_lines))
    final_progress = 100 if returncode == 0 else extract_progress_from_text(log_tail)
    payload = {
        "status": status,
        "returncode": final_returncode,
        "worker_id": worker_id,
        "max_concurrent_jobs": worker_concurrency(base_config),
        "log_tail": log_tail,
        "result": summarize_result(result),
        "metrics": collect_system_metrics(base_config),
    }
    if cancel_message:
        payload["error"] = cancel_message
    if worker_artifacts:
        payload["worker_artifacts"] = worker_artifacts
    if preview_summary:
        payload["preview"] = preview_summary
    if final_progress is not None:
        payload["progress"] = final_progress
    post_status(remote_base_url, worker_token, job_id, payload)
    return {
        "status": status,
        "returncode": final_returncode,
        "log": str(log_path),
        "result": summarize_result(result),
        "preview": preview_summary,
        "worker_artifacts": worker_artifacts,
    }


def worker_action(job: dict[str, Any]) -> str:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    return str(metadata.get("worker_action") or "").strip()


def worker_language_capabilities(config: dict[str, Any]) -> dict[str, Any]:
    try:
        llm = LocalLLMClient(config).status()
    except Exception:
        llm = {"available": False, "backend": "none", "model": None}
    try:
        tts = tts_health(config)
    except Exception:
        tts = {"backend": "none"}
    return language_capabilities(config, llm_status=llm, tts_status=tts)


def run_worker_action(
    job: dict[str, Any],
    remote_base_url: str,
    worker_token: str,
    config_path: str | Path,
    *,
    worker_id: str,
) -> dict[str, Any]:
    action = worker_action(job)
    if action == "upload_artifact_cache":
        return run_upload_artifact_cache_job(job, remote_base_url, worker_token, config_path, worker_id=worker_id)
    raise RuntimeError(f"Unsupported worker action: {action}")


def run_upload_artifact_cache_job(
    job: dict[str, Any],
    remote_base_url: str,
    worker_token: str,
    config_path: str | Path,
    *,
    worker_id: str,
) -> dict[str, Any]:
    job_id = str(job["id"])
    config = load_config(config_path)
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    ref_id = str(metadata.get("artifact_ref_id") or "")
    try_post_status(
        remote_base_url,
        worker_token,
        job_id,
        {
            "status": "running",
            "worker_id": worker_id,
            "progress": 5,
            "metrics": collect_system_metrics(config),
            "log_tail": sanitize_remote_text(f"Preparing artifact cache upload for {ref_id}"),
        },
    )
    try:
        artifact = find_registered_artifact(ref_id)
        path = Path(str(artifact["path"]))
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"Registered artifact is missing on worker: {artifact.get('name') or ref_id}")
        uploaded = upload_worker_artifact_cache(
            remote_base_url,
            worker_token,
            job_id,
            path,
            artifact_id=str(metadata.get("artifact_id") or f"worker_artifact_{ref_id}"),
            artifact_ref_id=ref_id,
            display_name=str(metadata.get("artifact_name") or artifact.get("name") or path.name),
            source_output_key=str(metadata.get("source_output_key") or artifact.get("source_output_key") or "artifact"),
            worker_id=worker_id,
        )
        payload = {
            "status": "done",
            "returncode": 0,
            "worker_id": worker_id,
            "max_concurrent_jobs": worker_concurrency(config),
            "progress": 100,
            "result": {"artifact_cache": uploaded.get("artifact", uploaded), "artifact_ref_id": ref_id},
            "metrics": collect_system_metrics(config),
            "log_tail": sanitize_remote_text(f"Uploaded artifact cache for {artifact.get('name') or path.name}"),
        }
        post_status(remote_base_url, worker_token, job_id, payload)
        return {"status": "done", "artifact": uploaded}
    except Exception as exc:
        payload = {
            "status": "failed",
            "returncode": 1,
            "worker_id": worker_id,
            "max_concurrent_jobs": worker_concurrency(config),
            "progress": 100,
            "error": sanitize_remote_text(str(exc)),
            "metrics": collect_system_metrics(config),
            "log_tail": sanitize_remote_text(f"Artifact cache upload failed: {exc}"),
        }
        try_post_status(remote_base_url, worker_token, job_id, payload)
        return {"status": "failed", "error": str(exc)}


def register_worker_artifacts(result: dict[str, Any] | None, job: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = collect_result_output_paths(result)
    if not outputs:
        return []
    registry = load_artifact_registry()
    rows = registry.setdefault("artifacts", [])
    existing = {str(row.get("ref_id")): row for row in rows if isinstance(row, dict)}
    job_id = str(job.get("id") or "")
    summaries: list[dict[str, Any]] = []
    for source_output_key, path in outputs.items():
        if not path.exists() or not path.is_file():
            continue
        stat = path.stat()
        ref_id = artifact_ref_id(job_id, source_output_key, path, stat.st_size, stat.st_mtime)
        row = {
            "ref_id": ref_id,
            "job_id": job_id,
            "source_output_key": source_output_key,
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "media_type": media_type_for(path),
            "registered_at": int(time.time()),
        }
        if ref_id in existing:
            existing[ref_id].update(row)
        else:
            rows.append(row)
        summaries.append(worker_artifact_summary(row))
    save_artifact_registry(registry)
    return summaries


def collect_result_output_paths(result: dict[str, Any] | None) -> dict[str, Path]:
    outputs: dict[str, Path] = {}
    if not result:
        return outputs
    report_json = result_report_json_path(result)
    if report_json and report_json.exists():
        try:
            report = read_json(report_json)
        except Exception:
            report = {}
        for key, value in (report.get("outputs") or {}).items():
            if key in DOWNLOADABLE_OUTPUTS and value:
                outputs[str(key)] = Path(str(value))
    if not outputs and result.get("video"):
        outputs["zh_dub_mp4"] = Path(str(result["video"]))
    return outputs


def result_report_json_path(result: dict[str, Any]) -> Path | None:
    raw = result.get("report")
    if not raw:
        return None
    path = Path(str(raw))
    if path.suffix.lower() == ".json":
        return path
    if path.suffix.lower() == ".md":
        return path.with_suffix(".json")
    candidate = path.with_suffix(".json")
    return candidate if candidate.exists() else path


def artifact_registry_path() -> Path:
    return PROJECT_ROOT / "runs" / "worker_artifacts" / "registry.json"


def load_artifact_registry() -> dict[str, Any]:
    path = artifact_registry_path()
    if not path.exists():
        return {"artifacts": []}
    try:
        data = read_json(path)
    except Exception:
        return {"artifacts": []}
    if not isinstance(data, dict):
        return {"artifacts": []}
    if not isinstance(data.get("artifacts"), list):
        data["artifacts"] = []
    if not isinstance(data.get("media"), list):
        data["media"] = []
    return data


def save_artifact_registry(registry: dict[str, Any]) -> None:
    path = artifact_registry_path()
    ensure_dir(path.parent)
    write_json(path, registry)


def artifact_ref_id(job_id: str, source_output_key: str, path: Path, size: int, mtime: float) -> str:
    raw = f"{job_id}\0{source_output_key}\0{path.resolve()}\0{size}\0{mtime:.6f}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]


def worker_artifact_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref_id": str(row.get("ref_id") or ""),
        "source_output_key": str(row.get("source_output_key") or ""),
        "name": str(row.get("name") or ""),
        "size": int(row.get("size", 0) or 0),
        "mtime": float(row.get("mtime", 0) or 0),
        "media_type": str(row.get("media_type") or "application/octet-stream"),
    }


def collect_worker_media_refs(config: dict[str, Any]) -> list[dict[str, Any]]:
    worker_cfg = config.get("worker", {}) if isinstance(config.get("worker"), dict) else {}
    if not bool(worker_cfg.get("expose_media_refs", True)):
        return []
    roots = worker_media_roots(config)
    max_refs = max(0, int(worker_cfg.get("max_media_refs", 500) or 500))
    if max_refs <= 0:
        return []
    registry = load_artifact_registry()
    rows = registry.setdefault("media", [])
    existing = {str(row.get("ref_id")): row for row in rows if isinstance(row, dict)}
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: p.name.lower()):
            if len(summaries) >= max_refs:
                break
            try:
                relative = path.relative_to(root)
            except ValueError:
                relative = Path(path.name)
            if should_skip(relative):
                continue
            if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            try:
                stat = path.stat()
                resolved = str(path.resolve())
            except OSError:
                continue
            ref_id = media_ref_id(path, stat.st_size, stat.st_mtime)
            row = {
                "ref_id": ref_id,
                "name": path.name,
                "path": resolved,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "media_type": media_type_for(path),
                "registered_at": int(time.time()),
            }
            if ref_id in existing:
                existing[ref_id].update(row)
            else:
                rows.append(row)
            seen.add(ref_id)
            summaries.append(worker_media_summary(row))
        if len(summaries) >= max_refs:
            break
    registry["media"] = [row for row in rows if isinstance(row, dict) and str(row.get("ref_id") or "") in seen]
    save_artifact_registry(registry)
    return summaries


def worker_media_roots(config: dict[str, Any]) -> list[Path]:
    worker_cfg = config.get("worker", {}) if isinstance(config.get("worker"), dict) else {}
    raw_roots = worker_cfg.get("media_roots")
    roots = raw_roots if isinstance(raw_roots, list) else []
    if not roots and config.get("input_dir"):
        roots = [config["input_dir"]]
    out: list[Path] = []
    for value in roots:
        text = str(value or "").strip()
        if text:
            out.append(Path(text))
    return out


def media_ref_id(path: Path, size: int, mtime: float) -> str:
    raw = f"media\0{path.resolve()}\0{size}\0{mtime:.6f}"
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]


def worker_media_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref_id": str(row.get("ref_id") or ""),
        "name": str(row.get("name") or ""),
        "size": int(row.get("size", 0) or 0),
        "mtime": float(row.get("mtime", 0) or 0),
        "media_type": str(row.get("media_type") or "application/octet-stream"),
    }


def resolve_worker_media_args(args: list[str]) -> list[str]:
    out: list[str] = []
    for item in args:
        text = str(item)
        if text.startswith("worker-ref:"):
            out.append(find_registered_media(text.removeprefix("worker-ref:"))["path"])
        else:
            out.append(text)
    return out


def find_registered_media(ref_id: str) -> dict[str, Any]:
    registry = load_artifact_registry()
    for row in registry.get("media", []):
        if isinstance(row, dict) and str(row.get("ref_id") or "") == ref_id:
            path = Path(str(row.get("path") or ""))
            if not path.exists() or not path.is_file():
                raise RuntimeError(f"Worker media ref is missing on worker: {row.get('name') or ref_id}")
            return dict(row)
    raise RuntimeError(f"Worker media ref not found: {ref_id}")


def find_registered_artifact(ref_id: str) -> dict[str, Any]:
    registry = load_artifact_registry()
    for row in registry.get("artifacts", []):
        if isinstance(row, dict) and str(row.get("ref_id") or "") == ref_id:
            return row
    raise RuntimeError(f"Worker artifact ref not found: {ref_id}")


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


def upload_worker_artifact_cache(
    remote_base_url: str,
    worker_token: str,
    job_id: str,
    file_path: Path,
    *,
    artifact_id: str,
    artifact_ref_id: str,
    display_name: str,
    source_output_key: str,
    worker_id: str,
) -> dict[str, Any]:
    path = f"/api/worker/jobs/{job_id}/artifact-cache"
    body = file_path.read_bytes()
    headers = worker_headers(worker_token, path=path, body=body)
    headers["Content-Type"] = media_type_for(file_path)
    headers["X-Worker-Artifact-Id"] = artifact_id
    headers["X-Worker-Artifact-Ref"] = artifact_ref_id
    headers["X-Worker-Artifact-Name"] = display_name
    headers["X-Worker-Artifact-File-Name"] = file_path.name
    headers["X-Worker-Artifact-Source-Key"] = source_output_key
    headers["X-Worker-Id"] = worker_id
    response = requests.post(endpoint(remote_base_url, path), data=body, headers=headers, timeout=300)
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


def get_worker_control(remote_base_url: str, worker_token: str, job_id: str, *, worker_id: str) -> dict[str, Any]:
    path = f"/api/worker/jobs/{job_id}/control"
    body = canonical_json({"worker_id": worker_id, "version": __version__})
    response = requests.post(
        endpoint(remote_base_url, path),
        data=body.encode("utf-8"),
        headers=worker_headers(worker_token, path=path, body=body),
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    control = data.get("control") if isinstance(data, dict) else {}
    return control if isinstance(control, dict) else {}


def try_get_worker_control(remote_base_url: str, worker_token: str, job_id: str, *, worker_id: str) -> dict[str, Any]:
    try:
        return get_worker_control(remote_base_url, worker_token, job_id, worker_id=worker_id)
    except Exception as exc:
        print(f"worker control poll warning for {job_id}: {exc}", file=sys.stderr)
        return {}


def worker_control_cancel_requested(control: dict[str, Any]) -> bool:
    return bool(control.get("cancel_requested")) and str(control.get("status") or "") in {"claimed", "running", "paused"}


def terminate_process_tree(proc: subprocess.Popen) -> int | None:
    if proc.poll() is not None:
        return proc.returncode
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
    else:
        proc.terminate()
    try:
        return proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=10)


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
    tail = sanitize_remote_text(tail_text(log_path, lines) if log_path.exists() else "")
    payload: dict[str, Any] = {
        "status": "running",
        "worker_id": worker_id,
        "max_concurrent_jobs": worker_concurrency(config),
        "log_tail": tail,
        "metrics": collect_system_metrics(config),
    }
    progress = extract_progress_from_text(tail)
    if progress is not None:
        payload["progress"] = progress
    if pid is not None:
        payload["pid"] = pid
    if command:
        payload["command"] = sanitize_remote_command(command)
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
    return sanitize_remote_command(redacted)


def worker_headers(
    worker_token: str,
    *,
    path: str | None = None,
    body: str | bytes = b"",
    method: str = "POST",
    legacy_token: bool = False,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not path:
        if not legacy_token:
            raise ValueError("worker_headers requires a request path for HMAC signing")
        headers["X-Worker-Token"] = worker_token
        return headers

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    headers.update(
        {
            "X-Worker-Auth": "hmac-sha256",
            "X-Worker-Timestamp": timestamp,
            "X-Worker-Nonce": nonce,
            "X-Worker-Signature": worker_signature(worker_token, timestamp=timestamp, method=method, path=path, body=body_bytes, nonce=nonce),
        }
    )
    return headers


def worker_signature(worker_token: str, *, timestamp: str, method: str, path: str, body: bytes, nonce: str | None = None) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    parts = [str(timestamp), method.upper(), path]
    if nonce:
        parts.append(str(nonce))
    parts.append(body_hash)
    message = "\n".join(parts).encode("utf-8")
    return hmac.new(worker_token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def endpoint(remote_base_url: str, path: str) -> str:
    return remote_base_url.rstrip("/") + path


def tail_text(path: Path, lines: int) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:120]
