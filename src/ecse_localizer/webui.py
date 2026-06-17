from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .artifacts import (
    artifact_catalog,
    cleanup_expired_files,
    filter_artifacts_for_user,
    find_artifact,
    safe_delete_artifact_record,
    verify_artifact_token,
    with_signed_urls,
)
from .config import load_config, privacy_guard, save_config
from .job_config import write_job_config
from .llm_local import LocalLLMClient
from .metrics import collect_system_metrics
from .platform_store import PlatformStore
from .scan import VIDEO_SUFFIXES, find_videos
from .tts import tts_health
from .utils import PROJECT_ROOT, ensure_dir, now_id, read_json, slugify, write_json


ALLOWED_UPLOAD_SUFFIXES = VIDEO_SUFFIXES | {".srt", ".vtt", ".ass", ".wav", ".mp3", ".m4a"}
COOKIE_NAME = "ecse_webui_session"
TOKEN_TTL_SECONDS = 12 * 60 * 60
ACTIVE_JOB_STATUSES = {"queued", "claimed", "running", "retrying", "paused"}
TERMINAL_JOB_STATUSES = {"done", "passed", "failed", "cancelled"}
TUNABLE_FIELDS: dict[str, dict[str, Any]] = {
    "audio.enhance": {"label": "音频增强", "type": "bool"},
    "llm.temperature": {"label": "LLM temperature", "type": "float", "min": 0, "max": 1},
    "llm.translation_chunk_size": {"label": "翻译分块字幕数", "type": "int", "min": 1, "max": 32},
    "translation.quality_mode": {"label": "翻译质量模式", "type": "choice", "options": ["best_quality", "balanced", "fast"]},
    "translation.context_window_segments": {"label": "翻译前后文窗口", "type": "int", "min": 0, "max": 8},
    "translation.max_zh_chars_per_subtitle_line": {"label": "中文字幕每行字数", "type": "int", "min": 12, "max": 36},
    "tts.cosyvoice_speed": {"label": "CosyVoice 语速", "type": "float", "min": 0.8, "max": 1.2},
    "tts.cosyvoice_gain": {"label": "CosyVoice 片段增益", "type": "float", "min": 0.5, "max": 5.0},
    "tts.end_gap_seconds": {"label": "句尾留白秒数", "type": "float", "min": 0.0, "max": 2.0},
    "tts.compact_max_gap_seconds": {"label": "紧凑调度最大间隔", "type": "float", "min": 0.2, "max": 4.0},
    "tts.prevent_audio_overlap": {"label": "防止配音重叠", "type": "bool"},
    "tts.min_audio_gap_seconds": {"label": "配音最小间隔秒数", "type": "float", "min": 0.0, "max": 1.0},
    "tts.compact_subtitle_gap_seconds": {"label": "字幕最小间隔", "type": "float", "min": 0.0, "max": 0.5},
    "tts.speaker_gender": {"label": "配音性别模式", "type": "choice", "options": ["auto", "male", "female"]},
    "tts.male_speaker": {"label": "男声 Speaker", "type": "text"},
    "tts.female_speaker": {"label": "女声 Speaker", "type": "text"},
    "tts.final_audio_filter_male": {"label": "男声最终滤波", "type": "textarea"},
    "tts.final_audio_filter_female": {"label": "女声最终滤波", "type": "textarea"},
    "dialect.enabled": {"label": "方言/轻口音", "type": "bool"},
    "dialect.target": {"label": "方言目标", "type": "choice", "options": ["mandarin", "sichuan", "cantonese", "dongbei", "shanghai", "taiwan"]},
    "mux.hard_subtitle": {"label": "生成硬字幕视频", "type": "bool"},
}


class WebState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = PlatformStore(self.config)
        self.store.bootstrap()
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.lock = threading.Lock()

    @property
    def webui(self) -> dict[str, Any]:
        return self.config.setdefault("webui", {})

    @property
    def upload_dir(self) -> Path:
        return ensure_dir(self.webui.get("upload_dir") or Path(self.config["output_dir"]) / "uploads")

    @property
    def job_dir(self) -> Path:
        return ensure_dir(self.webui.get("job_dir") or PROJECT_ROOT / "runs" / "webui_jobs")

    def reload_config(self) -> None:
        self.config = load_config(self.config_path)
        self.store = PlatformStore(self.config)
        self.store.bootstrap()


def create_app(config_path: str | Path | None = None) -> FastAPI:
    state = WebState(Path(config_path or PROJECT_ROOT / "config.yaml"))
    privacy_guard(state.config)
    app = FastAPI(title="AIALRA Localizer WebUI", docs_url=None, redoc_url=None)
    app.state.web = state
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/login")
    async def login_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        if not verify_credentials(username, password, state):
            return RedirectResponse("/?login_error=1", status_code=303)
        token = make_session(username, state)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=TOKEN_TTL_SECONDS)
        return response

    @app.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        user = verify_session(request, state)
        record = state.store.get_user(user) if user else None
        return {
            "authenticated": bool(user),
            "user": user,
            "user_record": record,
            "host": state.webui.get("host", "127.0.0.1"),
            "port": state.webui.get("port", 7861),
        }

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        body = await request.json()
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if not verify_credentials(username, password, state):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = make_session(username, state)
        response = JSONResponse({"ok": True, "user": username})
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=TOKEN_TTL_SECONDS)
        return response

    @app.post("/api/logout")
    def logout(_: str = Depends(require_user)) -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/api/dashboard")
    def dashboard(user: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        reports = list_reports(state.config, limit=12)
        videos = list_video_records(state.config, state.store.user_upload_dir(user))
        llm = LocalLLMClient(state.config).status()
        return {
            "input_dir": state.config["input_dir"],
            "output_dir": state.config["output_dir"],
            "upload_dir": str(state.store.user_upload_dir(user)),
            "video_count": len(videos),
            "report_count": len(list_reports(state.config, limit=10000)),
            "latest_reports": reports,
            "latest_jobs": list_jobs(state, user)[:8],
            "tts": tts_health(state.config),
            "llm": llm.__dict__,
            "quota": state.store.quota_status(user),
            "projects": state.store.list_projects(user, admin=is_admin(state, user)),
            "worker": worker_status_payload(state),
            "metrics": collect_system_metrics(state.config),
        }

    @app.get("/api/videos")
    def videos(user: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        return {"videos": list_video_records(state.config, state.store.user_upload_dir(user))}

    @app.get("/api/reports")
    def reports(_: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        return {"reports": list_reports(state.config, limit=200)}

    @app.get("/api/artifacts")
    def artifacts(user: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        rows = artifact_catalog(state.config, list_jobs(state, None))
        rows = filter_artifacts_for_user(rows, user, admin=is_admin(state, user))
        rows = with_signed_urls(rows[:300], secret=download_secret(state), username=user, ttl_seconds=int(state.webui.get("signed_url_ttl_seconds", 900)))
        return {"artifacts": rows, "quota": state.store.quota_status(user)}

    @app.get("/api/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str, request: Request, token: str = "", download: int = 0) -> FileResponse:
        user = verify_session(request, state)
        token_user = user or token_username(state, token, artifact_id)
        if not token_user:
            raise HTTPException(status_code=401, detail="Login or signed token required")
        if token and not verify_artifact_token(download_secret(state), token, artifact_id, token_user):
            raise HTTPException(status_code=401, detail="Invalid signed URL")
        rows = filter_artifacts_for_user(artifact_catalog(state.config, list_jobs(state, None)), token_user, admin=is_admin(state, token_user))
        row = find_artifact(rows, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = Path(row["path"])
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="File missing")
        disposition = "attachment" if download else "inline"
        return FileResponse(path, media_type=row.get("media_type") or None, filename=path.name, content_disposition_type=disposition)

    @app.delete("/api/artifacts/{artifact_id}")
    def delete_artifact(artifact_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        rows = filter_artifacts_for_user(artifact_catalog(state.config, list_jobs(state, None)), user, admin=is_admin(state, user))
        row = find_artifact(rows, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        try:
            deleted = safe_delete_artifact_record(row, state.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "artifact": row, "deleted": deleted, "quota": state.store.quota_status(user)}

    @app.post("/api/cleanup")
    async def cleanup(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        if not is_admin(state, user):
            raise HTTPException(status_code=403, detail="Admin required")
        body = await request.json()
        dry_run = bool(body.get("dry_run", True))
        older_than_days = int(body.get("older_than_days", state.webui.get("cleanup_older_than_days", 7)) or 7)
        result = cleanup_expired_files(state.config, older_than_days=older_than_days, dry_run=dry_run)
        return {"ok": True, "cleanup": result}

    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...), user: str = Depends(require_user)) -> dict[str, Any]:
        saved: list[dict[str, Any]] = []
        max_bytes = int(state.webui.get("max_upload_mb", 20480)) * 1024 * 1024
        for item in files:
            name = safe_upload_name(item.filename or "upload.bin")
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")
            target = unique_path(state.store.user_upload_dir(user) / name)
            size = 0
            with target.open("wb") as fh:
                while True:
                    chunk = await item.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        target.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail=f"Upload exceeds {state.webui.get('max_upload_mb')} MB")
                    if not state.store.can_store(user, size):
                        target.unlink(missing_ok=True)
                        quota = state.store.quota_status(user)
                        raise HTTPException(status_code=413, detail=f"User quota exceeded. Remaining bytes: {quota['local_remaining_bytes']}")
                    fh.write(chunk)
            saved.append({"name": target.name, "path": str(target), "size": size})
        return {"ok": True, "saved": saved, "quota": state.store.quota_status(user)}

    @app.get("/api/tuning")
    def get_tuning(_: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        return {"fields": fields_from_config(state.config)}

    @app.post("/api/tuning")
    async def update_tuning(request: Request, _: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        values = body.get("values", {})
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="values must be an object")
        state.reload_config()
        config = dict_without_runtime(state.config)
        for key, value in values.items():
            meta = TUNABLE_FIELDS.get(key)
            if not meta:
                raise HTTPException(status_code=400, detail=f"Unsupported tuning field: {key}")
            set_nested(config, key, coerce_value(value, meta))
        privacy_guard(config)
        backup_config(state.config_path)
        save_config(state.config_path, config)
        state.reload_config()
        return {"ok": True, "fields": fields_from_config(state.config)}

    @app.get("/api/config/raw")
    def get_raw_config(_: str = Depends(require_user)) -> PlainTextResponse:
        return PlainTextResponse(state.config_path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")

    @app.post("/api/config/raw")
    async def save_raw_config(request: Request, _: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        raw = str(body.get("yaml", ""))
        try:
            data = yaml.safe_load(raw) or {}
            data["project_root"] = str(PROJECT_ROOT)
            privacy_guard(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
        backup_config(state.config_path)
        state.config_path.write_text(raw.rstrip() + "\n", encoding="utf-8")
        state.reload_config()
        return {"ok": True}

    @app.get("/api/templates")
    def templates(user: str = Depends(require_user)) -> dict[str, Any]:
        return {"templates": state.store.list_templates(user, admin=is_admin(state, user))}

    @app.post("/api/templates")
    async def create_template(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        try:
            template = state.store.create_template(
                user,
                str(body.get("name", "")),
                body.get("params") if isinstance(body.get("params"), dict) else {},
                description=str(body.get("description", "")),
                shared=bool(body.get("shared", False)),
                admin=is_admin(state, user),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "template": template, "templates": state.store.list_templates(user, admin=is_admin(state, user))}

    @app.delete("/api/templates/{template_id}")
    def delete_template(template_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        try:
            deleted = state.store.delete_template(user, template_id, admin=is_admin(state, user))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "deleted": deleted, "templates": state.store.list_templates(user, admin=is_admin(state, user))}

    @app.get("/api/projects")
    def projects(user: str = Depends(require_user)) -> dict[str, Any]:
        return {"projects": projects_with_usage(state, user)}

    @app.post("/api/projects")
    async def create_project(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        try:
            project = state.store.create_project(
                user,
                str(body.get("name", "")),
                description=str(body.get("description", "")),
                quota_project_gb=float(body.get("quota_project_gb", state.store.default_project_quota_gb()) or state.store.default_project_quota_gb()),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "project": project}

    @app.post("/api/projects/{project_id}/folders")
    async def create_folder(project_id: str, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        try:
            folder = state.store.create_folder(
                user,
                project_id,
                str(body.get("name", "")),
                parent_id=str(body.get("parent_id") or "root"),
                admin=is_admin(state, user),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "folder": folder, "projects": projects_with_usage(state, user)}

    @app.get("/api/quota")
    def quota(user: str = Depends(require_user)) -> dict[str, Any]:
        return state.store.quota_status(user)

    @app.get("/api/metrics")
    def metrics(user: str = Depends(require_user)) -> dict[str, Any]:
        return {"metrics": collect_system_metrics(state.config), "worker": worker_status_payload(state), "quota": state.store.quota_status(user)}

    @app.get("/api/users")
    def users(user: str = Depends(require_user)) -> dict[str, Any]:
        if not is_admin(state, user):
            raise HTTPException(status_code=403, detail="Admin required")
        return {"users": state.store.list_users()}

    @app.post("/api/users")
    async def create_user(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        if not is_admin(state, user):
            raise HTTPException(status_code=403, detail="Admin required")
        body = await request.json()
        try:
            created = state.store.create_user(
                str(body.get("username", "")),
                str(body.get("password", "")),
                role=str(body.get("role", "user")),
                quota_local_gb=float(body.get("quota_local_gb", 500) or 500),
                quota_remote_gb=float(body.get("quota_remote_gb", 10) or 10),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "user": created}

    @app.patch("/api/users/{username}")
    async def update_user(username: str, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        if not is_admin(state, user):
            raise HTTPException(status_code=403, detail="Admin required")
        body = await request.json()
        if username.lower() == user.lower() and bool(body.get("disabled", False)):
            raise HTTPException(status_code=400, detail="Cannot disable the current admin session")
        if username.lower() == user.lower() and str(body.get("role", "admin")) != "admin":
            raise HTTPException(status_code=400, detail="Cannot demote the current admin session")
        try:
            updated = state.store.update_user(
                username,
                role=str(body["role"]) if "role" in body else None,
                disabled=bool(body["disabled"]) if "disabled" in body else None,
                quota_local_gb=float(body["quota_local_gb"]) if "quota_local_gb" in body else None,
                quota_remote_gb=float(body["quota_remote_gb"]) if "quota_remote_gb" in body else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "user": updated, "users": state.store.list_users()}

    @app.post("/api/worker/heartbeat")
    async def worker_heartbeat(request: Request) -> dict[str, Any]:
        require_worker_token(request, state)
        payload = await request.json()
        return {"ok": True, "worker": state.store.record_worker_heartbeat(payload)}

    @app.post("/api/worker/jobs/claim")
    async def worker_claim_job(request: Request) -> dict[str, Any]:
        require_worker_token(request, state)
        body = await request.json()
        worker_id = str(body.get("worker_id") or "local-windows-worker")
        job = claim_worker_job(state, worker_id)
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": worker_id,
                "version": str(body.get("version") or ""),
                "metrics": body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
                "message": "claim poll",
            }
        )
        return {"ok": True, "job": job}

    @app.post("/api/worker/jobs/{job_id}/status")
    async def worker_update_job(job_id: str, request: Request) -> dict[str, Any]:
        require_worker_token(request, state)
        body = await request.json()
        record = read_job(state, job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Job not found")
        changes = worker_status_changes(body)
        update_job(state, job_id, changes)
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": str(body.get("worker_id") or record.get("claimed_by") or "local-windows-worker"),
                "version": str(body.get("version") or ""),
                "metrics": body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
                "message": f"job {job_id} {changes.get('status')}",
            }
        )
        updated = read_job(state, job_id)
        return {"ok": True, "job": updated}

    @app.get("/api/jobs")
    def jobs(user: str = Depends(require_user)) -> dict[str, Any]:
        return {"jobs": list_jobs(state, user)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        return record

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: str, lines: int = 240, user: str = Depends(require_user)) -> PlainTextResponse:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        log_path = Path(record.get("log", ""))
        if not log_path.exists():
            if record.get("log_tail"):
                return PlainTextResponse(str(record.get("log_tail") or ""), media_type="text/plain; charset=utf-8")
            return PlainTextResponse("")
        return PlainTextResponse(tail_text(log_path, max(20, min(2000, lines))), media_type="text/plain; charset=utf-8")

    @app.post("/api/jobs")
    async def start_job(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        job_type = str(body.get("type", ""))
        execution_mode = str(state.webui.get("execution_mode", "local_subprocess"))
        worker_queue = execution_mode == "worker_queue" or bool(body.get("queue_for_worker"))
        command, title = build_job_command(job_type, body, state, validate_paths=not worker_queue)
        template_params = template_params_for_job(state, user, body)
        metadata = job_metadata_from_body(body, template_params=template_params)
        validate_job_project(state, user, metadata)
        worker_info: dict[str, Any] | None = None
        if worker_queue:
            worker_info = worker_status_payload(state)
            metadata["worker_args"] = worker_args_from_command(command)
            metadata["worker_status_at_submit"] = worker_info
            metadata["worker_queue_note"] = worker_info["message"]
        record = create_job_record(
            state,
            job_type,
            title,
            command,
            user=user,
            metadata=metadata,
            dispatch_target="worker" if worker_queue else "local",
        )
        if worker_queue:
            update_job(
                state,
                record["id"],
                {
                    "status": "queued",
                    "queued_for_worker": True,
                    "worker_status_at_submit": worker_info,
                },
            )
            record = read_job(state, record["id"]) or record
        else:
            job_config_path = write_job_config(state.config, metadata, job_id=record["id"])
            command = command_with_config(command, job_config_path)
            metadata = dict(record.get("metadata") or {})
            metadata["job_config"] = str(job_config_path)
            update_job(state, record["id"], {"command": command, "metadata": metadata, "job_config": str(job_config_path)})
            record = read_job(state, record["id"]) or record
            thread = threading.Thread(target=run_job, args=(state, record["id"]), daemon=True)
            thread.start()
        return {
            "ok": True,
            "job": record,
            "dispatch": {
                "target": "worker" if worker_queue else "local",
                "queued": worker_queue,
                "worker": worker_info,
            },
        }

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        with state.lock:
            proc = state.processes.get(job_id)
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
            update_job(state, job_id, {"status": "cancelled", "ended_at": iso_now(), "returncode": -9})
            return {"ok": True}
        if record.get("dispatch_target") == "worker" and record.get("status") in {"queued", "retrying"}:
            update_job(state, job_id, {"status": "cancelled", "ended_at": iso_now(), "returncode": -9, "updated_at": iso_now()})
            return {"ok": True}
        return {"ok": False, "message": "Job is not running in this WebUI process"}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        record = retry_job_record(state, job_id)
        if record.get("dispatch_target") == "worker":
            return {"ok": True, "job": record}
        thread = threading.Thread(target=run_job, args=(state, record["id"]), daemon=True)
        thread.start()
        return {"ok": True, "job": record}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        deleted = soft_delete_job(state, job_id, deleted_by=user)
        return {"ok": True, "job": deleted}

    return app


def require_user(request: Request) -> str:
    state: WebState = request.app.state.web
    user = verify_session(request, state)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def verify_credentials(username: str, password: str, state: WebState) -> bool:
    if state.store.verify_user(username, password):
        return True
    web = state.webui
    expected_user = str(web.get("username", "admin"))
    if not hmac.compare_digest(username, expected_user):
        return False
    if "password_hash" in web:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, str(web["password_hash"]))
    return hmac.compare_digest(password, str(web.get("password", "localizer")))


def make_session(username: str, state: WebState) -> str:
    payload = {"u": username, "exp": int(time.time()) + TOKEN_TTL_SECONDS, "n": uuid.uuid4().hex}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = sign(body, state)
    return f"{body}.{sig}"


def verify_session(request: Request, state: WebState) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, sign(body, state)):
        return None
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    username = str(payload.get("u") or "") or None
    record = state.store.get_user(username) if username else None
    if username and (not record or record.get("disabled")):
        return None
    return username


def sign(body: str, state: WebState) -> str:
    secret = str(state.webui.get("session_secret") or state.webui.get("password") or "ecse-localizer")
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def download_secret(state: WebState) -> str:
    return str(
        state.webui.get("download_secret")
        or state.webui.get("session_secret")
        or state.webui.get("password")
        or "ecse-localizer-download"
    )


def token_username(state: WebState, token: str, artifact_id_value: str) -> str | None:
    if not token or "." not in token:
        return None
    body, _ = token.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    username = str(payload.get("u") or "")
    if not username or payload.get("a") != artifact_id_value:
        return None
    if not state.store.get_user(username):
        return None
    return username


def is_admin(state: WebState, username: str) -> bool:
    user = state.store.get_user(username) or {}
    return user.get("role") == "admin"


def can_access_record(state: WebState, username: str, record: dict[str, Any]) -> bool:
    return is_admin(state, username) or not record.get("user") or record.get("user") == username


def require_worker_token(request: Request, state: WebState) -> None:
    token = request.headers.get("x-worker-token", "")
    expected = str(state.webui.get("worker_token") or os.environ.get("WORKER_SHARED_TOKEN") or "")
    if not expected:
        raise HTTPException(status_code=503, detail="Worker token is not configured")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid worker token")


def claim_worker_job(state: WebState, worker_id: str) -> dict[str, Any] | None:
    with state.lock:
        for path in sorted(state.job_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                record = read_json(path)
            except Exception:
                continue
            if record.get("dispatch_target") != "worker":
                continue
            if record.get("status") not in {"queued", "retrying"}:
                continue
            record.update(
                {
                    "status": "claimed",
                    "claimed_by": worker_id,
                    "claimed_at": iso_now(),
                    "updated_at": iso_now(),
                }
            )
            write_json(path, record)
            return record
    return None


def worker_status_changes(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"queued", "claimed", "running", "paused", "retrying", "done", "passed", "failed", "cancelled", "deleted"}
    status = str(body.get("status") or "").lower()
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported worker job status: {status}")
    if status == "passed":
        status = "done"
    changes: dict[str, Any] = {"status": status, "updated_at": iso_now()}
    if status == "running":
        changes.setdefault("started_at", iso_now())
    if status in {"done", "failed", "cancelled", "deleted"}:
        changes["ended_at"] = iso_now()
    for key in [
        "returncode",
        "pid",
        "progress",
        "error",
        "log_tail",
        "result",
        "result_report",
        "result_video",
        "worker_id",
        "metrics",
        "command",
    ]:
        if key in body:
            changes[key] = body[key]
    result = body.get("result")
    if isinstance(result, dict):
        if result.get("report"):
            changes["result_report"] = result.get("report")
        if result.get("video"):
            changes["result_video"] = result.get("video")
    return changes


def template_params_for_job(state: WebState, user: str, body: dict[str, Any]) -> dict[str, Any]:
    template_id = str(body.get("template_id") or "")
    if not template_id:
        return {}
    template = state.store.get_template(user, template_id, admin=is_admin(state, user))
    if not template:
        raise HTTPException(status_code=400, detail="Template not found")
    return dict(template.get("params") or {})


def job_metadata_from_body(body: dict[str, Any], *, template_params: dict[str, Any] | None = None) -> dict[str, Any]:
    template = template_params or {}
    metadata = {
        "project_id": str(body.get("project_id") or ""),
        "folder_id": str(body.get("folder_id") or "root"),
        "template_id": str(body.get("template_id") or ""),
        "source_language": str(template_value(body, template, "source_language", "auto")),
        "target_subtitle_language": str(template_value(body, template, "target_subtitle_language", "zh-CN")),
        "target_tts_language": str(template_value(body, template, "target_tts_language", "zh-CN")),
        "quality_mode": str(template_value(body, template, "quality_mode", "best_quality")),
        "style": str(template_value(body, template, "style", "")),
    }
    for key in [
        "tts_speed",
        "tts_emotion",
        "tts_end_gap_seconds",
        "tts_min_audio_gap_seconds",
        "tts_speaker_gender",
        "mux_keep_original_audio",
        "mux_original_audio_volume",
        "mux_hard_subtitle",
        "mux_soft_subtitle",
        "max_subtitle_line_chars",
    ]:
        value = template_value(body, template, key, None)
        if value is not None and value != "":
            metadata[key] = value
    return metadata


def template_value(body: dict[str, Any], template: dict[str, Any], key: str, default: Any) -> Any:
    return body[key] if key in body else template.get(key, default)


def validate_job_project(state: WebState, user: str, metadata: dict[str, Any]) -> None:
    try:
        state.store.validate_project_folder(
            user,
            str(metadata.get("project_id") or ""),
            str(metadata.get("folder_id") or "root"),
            admin=is_admin(state, user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def projects_with_usage(state: WebState, user: str) -> list[dict[str, Any]]:
    admin = is_admin(state, user)
    projects = state.store.list_projects(user, admin=admin)
    usage = project_artifact_usage(state, user, admin=admin)
    rows = []
    for project in projects:
        row = dict(project)
        used = int(usage.get(str(project.get("id")), 0))
        quota = int(project.get("quota_project_bytes") or 0)
        row["project_used_bytes"] = used
        row["project_remaining_bytes"] = max(0, quota - used) if quota else 0
        row["project_percent"] = round((used / quota) * 100, 2) if quota else 0
        rows.append(row)
    return rows


def project_artifact_usage(state: WebState, user: str, *, admin: bool) -> dict[str, int]:
    rows = filter_artifacts_for_user(artifact_catalog(state.config, list_jobs(state, None)), user, admin=admin)
    usage: dict[str, int] = {}
    seen_paths: set[str] = set()
    for row in rows:
        if row.get("kind") == "report_bundle":
            continue
        project_id = str(row.get("project_id") or "")
        path = str(row.get("path") or "")
        if not project_id or not path:
            continue
        key = str(Path(path).resolve()).lower()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        usage[project_id] = usage.get(project_id, 0) + int(row.get("size", 0) or 0)
    return usage


def worker_status_payload(state: WebState) -> dict[str, Any]:
    row = dict(state.store.worker_status())
    execution_mode = str(state.webui.get("execution_mode", "local_subprocess") or "local_subprocess")
    status = str(row.get("status") or "unknown")
    worker_required = execution_mode == "worker_queue"
    heartbeat_online = status == "online"
    available = heartbeat_online if worker_required else True
    if worker_required:
        if heartbeat_online:
            message = "Worker online; queued jobs can be claimed."
        elif status == "offline":
            age = row.get("age_seconds")
            message = f"Worker heartbeat is stale ({age}s); queued jobs will wait."
        else:
            message = "No worker heartbeat yet; queued jobs will wait for the Windows worker."
    else:
        message = "Local subprocess mode; worker queue is not required."
    row.update(
        {
            "execution_mode": execution_mode,
            "worker_required": worker_required,
            "heartbeat_online": heartbeat_online,
            "available": available,
            "queue_accepting": True,
            "message": message,
        }
    )
    return row


def worker_args_from_command(command: list[str]) -> list[str]:
    try:
        idx = command.index("ecse_localizer")
    except ValueError:
        return command
    args = command[idx + 1 :]
    if len(args) >= 2 and args[0] == "--config":
        args = args[2:]
    return args


def command_with_config(command: list[str], config_path: str | Path) -> list[str]:
    out = list(command)
    try:
        idx = out.index("--config")
    except ValueError:
        if len(out) >= 3 and out[1:3] == ["-m", "ecse_localizer"]:
            return [*out[:3], "--config", str(config_path), *out[3:]]
        return [sys.executable, "-m", "ecse_localizer", "--config", str(config_path), *out]
    if idx + 1 >= len(out):
        return out + [str(config_path)]
    out[idx + 1] = str(config_path)
    return out


def safe_upload_name(name: str) -> str:
    name = Path(name).name
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    return cleaned[:180] or f"upload_{uuid.uuid4().hex[:8]}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(1, 10000):
        candidate = path.with_name(f"{stem}_{i:03d}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique path for {path}")


def list_video_records(config: dict[str, Any], upload_dir: Path) -> list[dict[str, Any]]:
    paths: dict[str, Path] = {}
    for root in [Path(config["input_dir"]), upload_dir]:
        if root.exists():
            for video in find_videos(root):
                paths[str(video.resolve()).lower()] = video
    records = []
    for path in sorted(paths.values(), key=lambda p: p.name.lower()):
        records.append({"name": path.name, "path": str(path), "size": path.stat().st_size, "uploaded": upload_dir in path.parents})
    return records


def list_reports(config: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    out = Path(config["output_dir"])
    reports = sorted(out.glob("*_report.json"), key=lambda p: p.stat().st_mtime, reverse=True) if out.exists() else []
    return [report_summary(p) for p in reports[:limit]]


def report_summary(path: Path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except Exception as exc:
        return {"name": path.name, "path": str(path), "ok": False, "error": str(exc)}
    qa = data.get("qa", {})
    outputs = data.get("outputs", {})
    return {
        "name": data.get("name") or path.stem,
        "path": str(path),
        "mtime": path.stat().st_mtime,
        "pass": bool(qa.get("pass")),
        "issues": len(qa.get("issues", []) or []),
        "mode": data.get("mode"),
        "video": data.get("source_video"),
        "zh_dub_mp4": outputs.get("zh_dub_mp4"),
        "hard_sub": outputs.get("zh_dub_bilingual_hardsub_mp4"),
        "backend": data.get("translation_backend"),
        "tts": (data.get("tts") or {}).get("backend"),
    }


def fields_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    fields = []
    for path, meta in TUNABLE_FIELDS.items():
        item = dict(meta)
        item["path"] = path
        item["value"] = get_nested(config, path)
        fields.append(item)
    return fields


def get_nested(config: dict[str, Any], dotted: str) -> Any:
    cur: Any = config
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def set_nested(config: dict[str, Any], dotted: str, value: Any) -> None:
    cur = config
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def coerce_value(value: Any, meta: dict[str, Any]) -> Any:
    kind = meta["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value)


def dict_without_runtime(config: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(config, ensure_ascii=False))
    data.pop("project_root", None)
    return data


def backup_config(config_path: Path) -> None:
    backup = config_path.with_name(f"{config_path.name}.bak_{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")


def build_job_command(job_type: str, body: dict[str, Any], state: WebState, *, validate_paths: bool = True) -> tuple[list[str], str]:
    py = sys.executable
    base = [py, "-m", "ecse_localizer", "--config", str(state.config_path)]
    input_dir = str(body.get("input_dir") or state.config.get("input_dir"))
    output_dir = str(body.get("output_dir") or state.config.get("output_dir"))
    if job_type == "audit":
        return base + ["audit", "--input", input_dir], "Audit input directory"
    if job_type == "smoke":
        seconds = str(max(60, min(120, int(body.get("seconds") or 90))))
        return base + ["smoke", "--input", input_dir, "--seconds", seconds], f"Smoke test {seconds}s"
    if job_type == "process_one":
        video = str(body.get("video") or "")
        if not video or (validate_paths and not Path(video).exists()):
            raise HTTPException(status_code=400, detail="Video path is required")
        return base + ["process-one", "--video", video], f"Process one: {Path(video).name}"
    if job_type == "process_all":
        cmd = base + ["process-all", "--input", input_dir]
        if bool(body.get("force")):
            cmd.append("--force")
        return cmd, "Process all videos"
    if job_type == "report":
        return base + ["report", "--output", output_dir], "Build report index"
    if job_type == "compact_rerender":
        report = str(body.get("report") or "")
        if not report or (validate_paths and not Path(report).exists()):
            raise HTTPException(status_code=400, detail="Report path is required")
        tag = str(body.get("tag") or f"webui_{int(time.time())}")
        cmd = base + ["compact-rerender", "--report", report, "--tag", tag]
        run_dir = str(body.get("run_dir") or "")
        if run_dir:
            cmd += ["--run-dir", run_dir]
        return cmd, f"Compact rerender: {Path(report).name}"
    if job_type == "fidelity_audit":
        report = str(body.get("report") or "")
        if not report or (validate_paths and not Path(report).exists()):
            raise HTTPException(status_code=400, detail="Report path is required")
        return base + ["fidelity-audit", "--report", report], f"Fidelity audit: {Path(report).name}"
    raise HTTPException(status_code=400, detail=f"Unsupported job type: {job_type}")


def create_job_record(
    state: WebState,
    job_type: str,
    title: str,
    command: list[str],
    *,
    user: str,
    metadata: dict[str, Any] | None = None,
    dispatch_target: str = "local",
) -> dict[str, Any]:
    job_id = now_id(f"webui_{job_type}") + "_" + uuid.uuid4().hex[:6]
    log_path = state.job_dir / f"{job_id}.log"
    record = {
        "id": job_id,
        "user": user,
        "type": job_type,
        "title": title,
        "status": "queued",
        "dispatch_target": dispatch_target,
        "queued_for_worker": dispatch_target == "worker",
        "created_at": iso_now(),
        "started_at": None,
        "ended_at": None,
        "returncode": None,
        "pid": None,
        "command": command,
        "log": str(log_path),
        "metadata": metadata or {},
    }
    write_json(state.job_dir / f"{job_id}.json", record)
    return record


def run_job(state: WebState, job_id: str) -> None:
    record = read_job(state, job_id)
    if not record:
        return
    log_path = Path(record["log"])
    ensure_dir(log_path.parent)
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    update_job(state, job_id, {"status": "running", "started_at": iso_now()})
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {' '.join(record['command'])}\n\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                record["command"],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with state.lock:
                state.processes[job_id] = proc
            update_job(state, job_id, {"pid": proc.pid})
            returncode = proc.wait()
            log.flush()
            result_payload = extract_job_result_from_log(log_path)
            changes = {
                "status": "done" if returncode == 0 else "failed",
                "ended_at": iso_now(),
                "returncode": returncode,
            }
            if result_payload:
                changes["result"] = result_payload
                if result_payload.get("report"):
                    changes["result_report"] = result_payload.get("report")
                if result_payload.get("video"):
                    changes["result_video"] = result_payload.get("video")
            update_job(
                state,
                job_id,
                changes,
            )
        except Exception as exc:
            log.write(f"\nWEBUI JOB ERROR: {exc}\n")
            update_job(state, job_id, {"status": "failed", "ended_at": iso_now(), "error": str(exc), "returncode": -1})
        finally:
            with state.lock:
                state.processes.pop(job_id, None)


def read_job(state: WebState, job_id: str) -> dict[str, Any] | None:
    path = state.job_dir / f"{safe_job_id(job_id)}.json"
    if not path.exists():
        return None
    return read_json(path)


def update_job(state: WebState, job_id: str, changes: dict[str, Any]) -> None:
    path = state.job_dir / f"{safe_job_id(job_id)}.json"
    if not path.exists():
        return
    with state.lock:
        record = read_json(path)
        record.update(changes)
        write_json(path, record)


def list_jobs(state: WebState, user: str | None = None, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    records = []
    for path in sorted(state.job_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = read_json(path)
            if not include_deleted and record.get("status") == "deleted":
                continue
            if user and not can_access_record(state, user, record):
                continue
            records.append(record)
        except Exception:
            continue
    return records


def retry_job_record(state: WebState, job_id: str) -> dict[str, Any]:
    record = read_job(state, job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    status = str(record.get("status") or "")
    if status in ACTIVE_JOB_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job is already active: {status}")
    if status == "deleted":
        raise HTTPException(status_code=409, detail="Deleted jobs cannot be retried")
    if status not in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job cannot be retried from status: {status}")
    retry_count = int(record.get("retry_count") or 0) + 1
    log_path = state.job_dir / f"{safe_job_id(job_id)}_retry{retry_count}.log"
    changes = {
        "status": "retrying",
        "retry_count": retry_count,
        "previous_status": status,
        "started_at": None,
        "ended_at": None,
        "returncode": None,
        "pid": None,
        "error": None,
        "log": str(log_path),
        "updated_at": iso_now(),
    }
    update_job(state, job_id, changes)
    return read_job(state, job_id) or record


def soft_delete_job(state: WebState, job_id: str, *, deleted_by: str) -> dict[str, Any]:
    record = read_job(state, job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    status = str(record.get("status") or "")
    with state.lock:
        proc = state.processes.get(job_id)
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
    elif status in {"running", "claimed"}:
        raise HTTPException(status_code=409, detail="Cancel the running worker job before deleting it")
    update_job(
        state,
        job_id,
        {
            "status": "deleted",
            "previous_status": status,
            "deleted_at": iso_now(),
            "deleted_by": deleted_by,
            "ended_at": record.get("ended_at") or iso_now(),
            "updated_at": iso_now(),
        },
    )
    return read_job(state, job_id) or record


def extract_job_result_from_log(log_path: Path) -> dict[str, Any] | None:
    if not log_path.exists():
        return None
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


def safe_job_id(job_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    return job_id


def tail_text(path: Path, lines: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def iso_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ecse-localizer-webui")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    privacy_guard(config)
    web = config.get("webui", {})
    host = args.host or str(web.get("host", "127.0.0.1"))
    port = int(args.port or web.get("port", 7861))
    if web.get("bind_local_only", True) and host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("WebUI is configured for local-only binding. Use 127.0.0.1 or set bind_local_only: false.")
    app = create_app(args.config)
    print(f"ECSE Localizer WebUI: http://{host}:{port}")
    print(f"Login user: {web.get('username', 'admin')} (password is read from local config or user store)")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
