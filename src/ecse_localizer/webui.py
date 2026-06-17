from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

import uvicorn
import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .artifacts import (
    artifact_catalog,
    cleanup_expired_files,
    filter_artifact_records,
    filter_artifacts_for_user,
    find_artifact,
    preview_cache_dir,
    preview_manifest_path,
    safe_delete_artifact_record,
    verify_artifact_token,
    with_signed_urls,
)
from .capabilities import language_capabilities
from .config import load_config, privacy_guard, save_config
from .job_config import write_job_config
from .llm_local import LocalLLMClient
from .metrics import collect_system_metrics, sanitize_metrics
from .platform_store import PlatformStore
from .redaction import is_remote_safe_reference, sanitize_remote_command, sanitize_remote_text, sanitize_remote_value
from .scan import VIDEO_SUFFIXES, find_videos
from .tts import tts_health
from .utils import PROJECT_ROOT, ensure_dir, now_id, read_json, slugify, write_json


ALLOWED_UPLOAD_SUFFIXES = VIDEO_SUFFIXES | {".srt", ".vtt", ".ass", ".wav", ".mp3", ".m4a"}
COOKIE_NAME = "ecse_webui_session"
TOKEN_TTL_SECONDS = 12 * 60 * 60
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
ACTIVE_JOB_STATUSES = {"queued", "claimed", "running", "retrying", "paused"}
TERMINAL_JOB_STATUSES = {"done", "passed", "failed", "cancelled"}
JOB_SCHEMA_VERSION = 2
NORMALIZED_JOB_STATUSES = {"queued", "claimed", "running", "paused", "retrying", "done", "failed", "cancelled", "deleted"}
JOB_STATUS_ALIASES = {
    "passed": "done",
    "pass": "done",
    "success": "done",
    "succeeded": "done",
    "complete": "done",
    "completed": "done",
    "error": "failed",
    "errored": "failed",
    "failure": "failed",
    "canceled": "cancelled",
    "stopped": "cancelled",
}
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
    "tts.trim_overlong_audio_to_slot": {"label": "超长配音裁切到窗口", "type": "bool"},
    "tts.slot_trim_tolerance_seconds": {"label": "配音裁切容忍秒数", "type": "float", "min": 0.0, "max": 0.25},
    "tts.slot_trim_fade_seconds": {"label": "配音裁切淡出秒数", "type": "float", "min": 0.0, "max": 0.3},
    "tts.compact_subtitle_gap_seconds": {"label": "字幕最小间隔", "type": "float", "min": 0.0, "max": 0.5},
    "tts.speaker_gender": {"label": "配音性别模式", "type": "choice", "options": ["auto", "male", "female"]},
    "tts.male_speaker": {"label": "男声 Speaker", "type": "text"},
    "tts.female_speaker": {"label": "女声 Speaker", "type": "text"},
    "tts.final_audio_filter_male": {"label": "男声最终滤波", "type": "textarea"},
    "tts.final_audio_filter_female": {"label": "女声最终滤波", "type": "textarea"},
    "dialect.enabled": {"label": "方言/轻口音", "type": "bool"},
    "dialect.target": {"label": "方言目标", "type": "choice", "options": ["mandarin", "sichuan", "cantonese", "dongbei", "shanghai", "taiwan"]},
    "mux.hard_subtitle": {"label": "生成硬字幕视频", "type": "bool"},
    "webui.global_remote_quota_gb": {"label": "远端全局存储额度 GB", "type": "float", "min": 0, "max": 10240},
    "webui.max_active_jobs_per_user": {"label": "每用户最大活动任务数", "type": "int", "min": 1, "max": 32},
    "webui.max_active_jobs_global": {"label": "全局最大活动任务数", "type": "int", "min": 1, "max": 128},
}


class WebState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = PlatformStore(self.config)
        self.store.bootstrap()
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.worker_nonce_cache: dict[str, int] = {}
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

    @app.middleware("http")
    async def csrf_origin_guard(request: Request, call_next: Any) -> Response:
        if csrf_origin_check_required(request, state) and not csrf_origin_allowed(request, state):
            return JSONResponse({"detail": "CSRF origin check failed"}, status_code=403)
        return await call_next(request)

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
        set_session_cookie(response, token, state)
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
        set_session_cookie(response, token, state)
        return response

    @app.post("/api/logout")
    def logout(_: str = Depends(require_user)) -> JSONResponse:
        response = JSONResponse({"ok": True})
        delete_session_cookie(response, state)
        return response

    @app.get("/api/dashboard")
    def dashboard(user: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        reports = list_visible_reports(state, user, limit=12)
        videos = list_all_video_records(state, user)
        llm = LocalLLMClient(state.config).status()
        tts = tts_health(state.config)
        worker = worker_status_payload(state)
        caps = effective_language_capabilities(state, llm=llm, tts=tts, worker=worker)
        jobs = list_jobs(state, user)
        storage = dashboard_storage_summary(state, user)
        return {
            "input_dir": storage["input"],
            "output_dir": storage["output"],
            "upload_dir": storage["upload"],
            "storage_summary": storage,
            "upload_policy": browser_upload_policy(state),
            "video_count": len(videos),
            "report_count": len(list_visible_reports(state, user, limit=10000)),
            "latest_reports": reports,
            "latest_jobs": [public_job_record(job) for job in jobs[:8]],
            "tts": tts,
            "llm": llm.__dict__,
            "capabilities": caps,
            "quota": state.store.quota_status(user),
            "projects": state.store.list_projects(user, admin=is_admin(state, user)),
            "worker": worker,
            "queue": job_queue_summary(state, user, worker=worker, jobs=jobs),
            "metrics": dashboard_metrics(state, worker),
        }

    @app.get("/api/videos")
    def videos(user: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        return {"videos": list_all_video_records(state, user), "upload_policy": browser_upload_policy(state)}

    @app.get("/api/reports")
    def reports(user: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        return {"reports": list_visible_reports(state, user, limit=200)}

    @app.get("/api/capabilities")
    def capabilities(_: str = Depends(require_user)) -> dict[str, Any]:
        state.reload_config()
        llm = LocalLLMClient(state.config).status()
        tts = tts_health(state.config)
        return effective_language_capabilities(state, llm=llm, tts=tts)

    @app.get("/api/artifacts")
    def artifacts(
        project_id: str = "",
        folder_id: str = "",
        job_id: str = "",
        kind: str = "",
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        state.reload_config()
        rows = artifact_catalog(state.config, list_jobs(state, None))
        rows = filter_artifacts_for_user(rows, user, admin=is_admin(state, user))
        rows = filter_artifact_records(rows, project_id=project_id, folder_id=folder_id, job_id=job_id, kind=kind)
        rows = with_signed_urls(rows[:300], secret=download_secret(state), username=user, ttl_seconds=int(state.webui.get("signed_url_ttl_seconds", 900)))
        return {"artifacts": rows, "quota": state.store.quota_status(user)}

    @app.get("/api/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str, request: Request, token: str = "", download: int = 0, variant: str = "") -> FileResponse:
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
        if not row.get("path"):
            raise HTTPException(status_code=409, detail="Artifact is on the Windows worker; request a temporary cache first")
        media_type = row.get("media_type") or None
        path = Path(row["path"])
        if variant == "thumbnail":
            if not row.get("thumbnail_path"):
                raise HTTPException(status_code=404, detail="Thumbnail not found")
            path = Path(str(row["thumbnail_path"]))
            media_type = row.get("thumbnail_media_type") or None
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="File missing")
        disposition = "attachment" if download else "inline"
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type=disposition)

    @app.delete("/api/artifacts/{artifact_id}")
    def delete_artifact(artifact_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        rows = filter_artifacts_for_user(artifact_catalog(state.config, list_jobs(state, None)), user, admin=is_admin(state, user))
        row = find_artifact(rows, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if row.get("remote_worker_artifact") and not row.get("path"):
            raise HTTPException(status_code=400, detail="Worker artifact refs do not delete local files; delete the source job instead")
        try:
            deleted = safe_delete_artifact_record(row, state.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "artifact": row, "deleted": deleted, "quota": state.store.quota_status(user)}

    @app.post("/api/artifacts/{artifact_id}/request-cache")
    def request_artifact_cache(artifact_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        rows = filter_artifacts_for_user(artifact_catalog(state.config, list_jobs(state, None)), user, admin=is_admin(state, user))
        row = find_artifact(rows, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if not row.get("remote_worker_artifact") or not row.get("download_requestable"):
            raise HTTPException(status_code=400, detail="Artifact is already cached or cannot be requested from a worker")
        enforce_artifact_cache_request_limits(state, user, row)
        enforce_active_job_limits(state, user)
        worker_info = worker_status_payload(state)
        metadata = {
            "worker_action": "upload_artifact_cache",
            "artifact_id": str(row["id"]),
            "artifact_ref_id": str(row["artifact_ref_id"]),
            "artifact_name": str(row.get("name") or ""),
            "source_job_id": str(row.get("source_job_id") or row.get("job_id") or ""),
            "source_output_key": str(row.get("source_output_key") or ""),
            "project_id": str(row.get("project_id") or ""),
            "folder_id": str(row.get("folder_id") or "root"),
            "worker_status_at_submit": worker_info,
        }
        record = create_job_record(
            state,
            "cache_artifact",
            f"Cache artifact: {file_display_name(str(row.get('name') or row['id']))}",
            ["worker-action", "upload-artifact-cache", str(row["artifact_ref_id"])],
            user=user,
            metadata=metadata,
            dispatch_target="worker",
        )
        return {"ok": True, "job": public_job_record(record), "dispatch": {"target": "worker", "queued": True, "worker": worker_info}}

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
        state.reload_config()
        policy = browser_upload_policy(state)
        if not policy["enabled"]:
            raise HTTPException(status_code=403, detail=policy["message"])
        saved: list[dict[str, Any]] = []
        max_bytes = int(state.webui.get("max_upload_mb", 20480)) * 1024 * 1024
        quota = state.store.quota_status(user)
        base_used = int(quota["remote_used_bytes"])
        quota_bytes = int(quota["remote_quota_bytes"])
        global_base_used = int(quota.get("remote_global_used_bytes") or 0)
        global_quota_bytes = int(quota.get("remote_global_quota_bytes") or 0)
        reserved_bytes = 0
        for item in files:
            name = safe_upload_name(item.filename or "upload.bin")
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")
            target = unique_path(state.store.user_upload_dir(user) / name)
            size = 0
            try:
                with target.open("wb") as fh:
                    while True:
                        chunk = await item.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise HTTPException(status_code=413, detail=f"Upload exceeds {state.webui.get('max_upload_mb')} MB")
                        if not upload_fits_quota(base_used, reserved_bytes, size, quota_bytes):
                            remaining = max(0, quota_bytes - base_used - reserved_bytes)
                            raise HTTPException(status_code=413, detail=f"Remote quota exceeded. Remaining bytes: {remaining}")
                        if global_quota_bytes > 0 and not upload_fits_quota(global_base_used, reserved_bytes, size, global_quota_bytes):
                            remaining = max(0, global_quota_bytes - global_base_used - reserved_bytes)
                            raise HTTPException(status_code=413, detail=f"Global remote quota exceeded. Remaining bytes: {remaining}")
                        fh.write(chunk)
            except HTTPException:
                await item.close()
                target.unlink(missing_ok=True)
                raise
            await item.close()
            saved.append({"name": target.name, "path": str(target), "size": size})
            reserved_bytes += size
        return {"ok": True, "saved": saved, "quota": state.store.quota_status(user)}

    @app.get("/api/tuning")
    def get_tuning(user: str = Depends(require_user)) -> dict[str, Any]:
        require_admin_access(state, user)
        state.reload_config()
        return {"fields": fields_from_config(state.config)}

    @app.post("/api/tuning")
    async def update_tuning(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        require_admin_access(state, user)
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
    def get_raw_config(user: str = Depends(require_user)) -> PlainTextResponse:
        require_admin_access(state, user)
        return PlainTextResponse(state.config_path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")

    @app.post("/api/config/raw")
    async def save_raw_config(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        require_admin_access(state, user)
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

    @app.patch("/api/templates/{template_id}")
    async def update_template(template_id: str, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        try:
            template = state.store.update_template(
                user,
                template_id,
                name=str(body["name"]) if "name" in body else None,
                params=body.get("params") if isinstance(body.get("params"), dict) else None,
                description=str(body["description"]) if "description" in body else None,
                shared=bool(body["shared"]) if "shared" in body else None,
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
    def projects(include_archived: bool = False, user: str = Depends(require_user)) -> dict[str, Any]:
        return {"projects": projects_with_usage(state, user, include_archived=include_archived)}

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

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        try:
            project = state.store.update_project(
                user,
                project_id,
                name=str(body.get("name")) if "name" in body else None,
                description=str(body.get("description")) if "description" in body else None,
                quota_project_gb=float(body["quota_project_gb"]) if "quota_project_gb" in body else None,
                admin=is_admin(state, user),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "project": project, "projects": projects_with_usage(state, user)}

    @app.delete("/api/projects/{project_id}")
    def archive_project(project_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        try:
            archived = state.store.archive_project(user, project_id, admin=is_admin(state, user))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "archived": archived, "projects": projects_with_usage(state, user)}

    @app.post("/api/projects/{project_id}/restore")
    def restore_project(project_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        try:
            restored = state.store.restore_project(user, project_id, admin=is_admin(state, user))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restored": restored, "projects": projects_with_usage(state, user, include_archived=True)}

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

    @app.patch("/api/projects/{project_id}/folders/{folder_id}")
    async def update_folder(project_id: str, folder_id: str, request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = await request.json()
        try:
            folder = state.store.update_folder(
                user,
                project_id,
                folder_id,
                name=str(body.get("name", "")),
                admin=is_admin(state, user),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "folder": folder, "projects": projects_with_usage(state, user)}

    @app.delete("/api/projects/{project_id}/folders/{folder_id}")
    def archive_folder(project_id: str, folder_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        try:
            folder = state.store.archive_folder(user, project_id, folder_id, admin=is_admin(state, user))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "archived": folder, "projects": projects_with_usage(state, user)}

    @app.post("/api/projects/{project_id}/folders/{folder_id}/restore")
    def restore_folder(project_id: str, folder_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        try:
            folder = state.store.restore_folder(user, project_id, folder_id, admin=is_admin(state, user))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restored": folder, "projects": projects_with_usage(state, user, include_archived=True)}

    @app.get("/api/quota")
    def quota(user: str = Depends(require_user)) -> dict[str, Any]:
        return state.store.quota_status(user)

    @app.get("/api/metrics")
    def metrics(user: str = Depends(require_user)) -> dict[str, Any]:
        worker = worker_status_payload(state)
        return {
            "metrics": dashboard_metrics(state, worker),
            "worker": worker,
            "quota": state.store.quota_status(user),
            "queue": job_queue_summary(state, user, worker=worker),
        }

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
        payload = await require_worker_request(request, state)
        return {"ok": True, "worker": state.store.record_worker_heartbeat(payload)}

    @app.post("/api/worker/jobs/claim")
    async def worker_claim_job(request: Request) -> dict[str, Any]:
        body = await require_worker_request(request, state)
        worker_id = str(body.get("worker_id") or "local-windows-worker")
        job = claim_worker_job(state, worker_id)
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": worker_id,
                "version": str(body.get("version") or ""),
                "max_concurrent_jobs": body.get("max_concurrent_jobs"),
                "metrics": body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
                "media_refs": body.get("media_refs") if isinstance(body.get("media_refs"), list) else None,
                "capabilities": body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {},
                "message": "claim poll",
            }
        )
        return {"ok": True, "job": job}

    @app.post("/api/worker/jobs/{job_id}/status")
    async def worker_update_job(job_id: str, request: Request) -> dict[str, Any]:
        body = await require_worker_request(request, state)
        record = read_job(state, job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Job not found")
        worker_id = str(body.get("worker_id") or request.headers.get("x-worker-id") or "")
        require_claimed_worker(record, worker_id, action="update job status")
        changes = worker_status_changes(body)
        if record.get("cancel_requested") and changes.get("status") in {"done", "failed"}:
            for key in ["worker_artifacts", "result", "result_report", "result_video"]:
                changes.pop(key, None)
            changes.update(
                {
                    "status": "cancelled",
                    "returncode": -9,
                    "error": "Cancelled by remote request",
                    "ended_at": iso_now(),
                    "cancel_handled_at": iso_now(),
                }
            )
        if changes.get("status") == "cancelled":
            changes.setdefault("cancel_handled_at", iso_now())
        update_job(state, job_id, changes)
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": worker_id or str(record.get("claimed_by") or "local-windows-worker"),
                "version": str(body.get("version") or ""),
                "max_concurrent_jobs": body.get("max_concurrent_jobs"),
                "metrics": body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
                "message": f"job {job_id} {changes.get('status')}",
            }
        )
        updated = read_job(state, job_id)
        return {"ok": True, "job": updated}

    @app.post("/api/worker/jobs/{job_id}/control")
    async def worker_job_control(job_id: str, request: Request) -> dict[str, Any]:
        body = await require_worker_request(request, state)
        record = read_job(state, job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Job not found")
        worker_id = str(body.get("worker_id") or request.headers.get("x-worker-id") or "")
        require_claimed_worker(record, worker_id, action="poll job control")
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": worker_id or str(record.get("claimed_by") or "local-windows-worker"),
                "version": str(body.get("version") or ""),
                "max_concurrent_jobs": body.get("max_concurrent_jobs"),
                "metrics": body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
                "message": f"job {job_id} control poll",
            }
        )
        return {"ok": True, "control": worker_control_payload(record)}

    @app.post("/api/worker/jobs/{job_id}/preview")
    async def worker_upload_preview(job_id: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        require_worker_token(request, state, body)
        record = read_job(state, job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Job not found")
        worker_id = str(request.headers.get("x-worker-id") or "")
        require_claimed_worker(record, worker_id, action="upload job preview")
        row = save_worker_preview_upload(state, record, request, body)
        update_job(
            state,
            job_id,
            {
                "preview_id": row["id"],
                "preview_uploaded_at": iso_now(),
                "preview_name": row.get("name", ""),
            },
        )
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": worker_id or str(record.get("claimed_by") or "local-windows-worker"),
                "message": f"job {job_id} preview uploaded",
            }
        )
        return {"ok": True, "preview": row, "bytes": len(body), "quota": state.store.quota_status(str(record.get("user") or ""))}

    @app.post("/api/worker/jobs/{job_id}/artifact-cache")
    async def worker_upload_artifact_cache(job_id: str, request: Request) -> dict[str, Any]:
        body = await request.body()
        require_worker_token(request, state, body)
        record = read_job(state, job_id)
        if not record:
            raise HTTPException(status_code=404, detail="Job not found")
        worker_id = str(request.headers.get("x-worker-id") or "")
        require_claimed_worker(record, worker_id, action="upload artifact cache")
        row = save_worker_artifact_cache_upload(state, record, request, body)
        update_job(
            state,
            job_id,
            {
                "cached_artifact_id": row["id"],
                "cached_artifact_uploaded_at": iso_now(),
                "cached_artifact_name": row.get("name", ""),
            },
        )
        state.store.record_worker_heartbeat(
            {
                "status": "online",
                "worker_id": worker_id or str(record.get("claimed_by") or "local-windows-worker"),
                "message": f"job {job_id} artifact cache uploaded",
            }
        )
        return {"ok": True, "artifact": row, "bytes": len(body), "quota": state.store.quota_status(str(record.get("user") or ""))}

    @app.get("/api/jobs")
    def jobs(
        project_id: str = "",
        folder_id: str = "",
        status: str = "",
        include_deleted: bool = False,
        user: str = Depends(require_user),
    ) -> dict[str, Any]:
        show_deleted = include_deleted or normalize_job_status(status) == "deleted"
        records = filter_job_records(list_jobs(state, user, include_deleted=show_deleted), project_id=project_id, folder_id=folder_id, status=status)
        return {"jobs": [public_job_record(record) for record in records]}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        return public_job_record(record)

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
        text = tail_text(log_path, max(20, min(2000, lines)))
        if record.get("dispatch_target") == "worker":
            text = sanitize_remote_text(text)
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    @app.post("/api/jobs")
    async def start_job(request: Request, user: str = Depends(require_user)) -> dict[str, Any]:
        body = dict(await request.json())
        job_type = str(body.get("type", ""))
        if job_type == "process_one":
            body["video"] = resolve_video_reference(state, user, str(body.get("video") or ""))
        execution_mode = str(state.webui.get("execution_mode", "local_subprocess"))
        worker_queue = execution_mode == "worker_queue" or bool(body.get("queue_for_worker"))
        command, title = build_job_command(job_type, body, state, validate_paths=not worker_queue)
        if worker_queue:
            validate_worker_queue_job_body(job_type, body, state)
        template_params = template_params_for_job(state, user, body)
        metadata = job_metadata_from_body(body, template_params=template_params)
        validate_job_project(state, user, metadata)
        enforce_storage_quota_before_job(state, user, metadata)
        enforce_active_job_limits(state, user)
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
            "job": public_job_record(record),
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
        if record.get("dispatch_target") == "worker" and record.get("status") in {"queued", "retrying", "paused"}:
            update_job(state, job_id, {"status": "cancelled", "ended_at": iso_now(), "returncode": -9, "updated_at": iso_now()})
            return {"ok": True, "job": public_job_record(read_job(state, job_id) or record)}
        if record.get("dispatch_target") == "worker" and record.get("status") in {"claimed", "running"}:
            update_job(
                state,
                job_id,
                {
                    "cancel_requested": True,
                    "cancel_requested_at": iso_now(),
                    "cancel_requested_by": user,
                    "updated_at": iso_now(),
                },
            )
            return {"ok": True, "job": public_job_record(read_job(state, job_id) or record), "message": "Cancel request sent to Windows worker"}
        return {"ok": False, "message": "Job is not running in this WebUI process"}

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"ok": True, "job": public_job_record(pause_job_record(state, job_id, paused_by=user))}

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"ok": True, "job": public_job_record(resume_job_record(state, job_id, resumed_by=user))}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        record = retry_job_record(state, job_id)
        if record.get("dispatch_target") == "worker":
            return {"ok": True, "job": public_job_record(record)}
        thread = threading.Thread(target=run_job, args=(state, record["id"]), daemon=True)
        thread.start()
        return {"ok": True, "job": public_job_record(record)}

    @app.post("/api/jobs/{job_id}/restore")
    def restore_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        restored = restore_deleted_job(state, job_id, restored_by=user)
        return {"ok": True, "job": public_job_record(restored)}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, user: str = Depends(require_user)) -> dict[str, Any]:
        record = read_job(state, job_id)
        if not record or not can_access_record(state, user, record):
            raise HTTPException(status_code=404, detail="Job not found")
        deleted = soft_delete_job(state, job_id, deleted_by=user)
        return {"ok": True, "job": public_job_record(deleted)}

    return app


def require_user(request: Request) -> str:
    state: WebState = request.app.state.web
    user = verify_session(request, state)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def csrf_origin_check_required(request: Request, state: WebState) -> bool:
    if not bool(state.webui.get("csrf_origin_check", False)):
        return False
    if request.method.upper() in SAFE_HTTP_METHODS:
        return False
    path = request.url.path
    if path.startswith("/api/worker/"):
        return False
    return True


def csrf_origin_allowed(request: Request, state: WebState) -> bool:
    origin = normalize_origin(str(request.headers.get("origin") or ""))
    if not origin:
        origin = origin_from_url(str(request.headers.get("referer") or ""))
    if not origin:
        return False
    return origin in allowed_csrf_origins(request, state)


def allowed_csrf_origins(request: Request, state: WebState) -> set[str]:
    origins = {request_origin(request)}
    configured = state.webui.get("csrf_trusted_origins")
    if isinstance(configured, list):
        origins.update(normalize_origin(str(item)) for item in configured)
    return {origin for origin in origins if origin}


def request_origin(request: Request) -> str:
    scheme = str(request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return normalize_origin(f"{scheme}://{host}") if host else ""


def origin_from_url(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return normalize_origin(f"{parsed.scheme}://{parsed.netloc}")


def normalize_origin(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        return f"{scheme}://{hostname}:{port}"
    return f"{scheme}://{hostname}"


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


def set_session_cookie(response: Response, token: str, state: WebState) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=session_cookie_secure(state),
        samesite="lax",
        max_age=TOKEN_TTL_SECONDS,
    )


def delete_session_cookie(response: Response, state: WebState) -> None:
    response.delete_cookie(COOKIE_NAME, secure=session_cookie_secure(state), samesite="lax")


def session_cookie_secure(state: WebState) -> bool:
    return bool(state.webui.get("cookie_secure", False))


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
    record = state.store.get_user(username)
    if not record or record.get("disabled"):
        return None
    return username


def is_admin(state: WebState, username: str) -> bool:
    user = state.store.get_user(username) or {}
    return user.get("role") == "admin"


def require_admin_access(state: WebState, username: str) -> None:
    if not is_admin(state, username):
        raise HTTPException(status_code=403, detail="Admin required")


def dashboard_storage_summary(state: WebState, username: str) -> dict[str, Any]:
    if is_admin(state, username):
        return {
            "redacted": False,
            "input": str(state.config["input_dir"]),
            "output": str(state.config["output_dir"]),
            "upload": str(state.store.user_upload_dir(username)),
        }
    return {
        "redacted": True,
        "input": "managed course media",
        "output": "managed output storage",
        "upload": "your upload storage",
    }


def can_access_record(state: WebState, username: str, record: dict[str, Any]) -> bool:
    return is_admin(state, username) or record.get("user") == username


def upload_fits_quota(base_used_bytes: int, reserved_bytes: int, current_file_bytes: int, quota_bytes: int) -> bool:
    if quota_bytes <= 0:
        return True
    return base_used_bytes + reserved_bytes + current_file_bytes <= quota_bytes


def browser_upload_policy(state: WebState) -> dict[str, Any]:
    execution_mode = str(state.webui.get("execution_mode", "local_subprocess") or "local_subprocess")
    explicit = state.webui.get("allow_remote_media_uploads")
    enabled = bool(explicit) if explicit is not None else execution_mode != "worker_queue"
    allow_worker_path_submission = bool(state.webui.get("allow_worker_path_submission", False))
    worker_ref_required = execution_mode == "worker_queue" and not allow_worker_path_submission
    if enabled:
        message = "Browser uploads are stored in the configured WebUI upload directory and count against remote quota."
        mode = "webui_upload_dir"
    else:
        message = (
            "Browser media upload is disabled for remote worker_queue mode. "
            "Keep original videos on the Windows worker and submit worker-ref media options from the worker registry."
        )
        mode = "disabled"
    return {
        "enabled": enabled,
        "mode": mode,
        "execution_mode": execution_mode,
        "max_upload_mb": int(state.webui.get("max_upload_mb", 20480) or 20480),
        "allowed_suffixes": sorted(ALLOWED_UPLOAD_SUFFIXES),
        "allow_worker_path_submission": allow_worker_path_submission,
        "worker_ref_required": worker_ref_required,
        "worker_path_message": (
            "Submit worker-ref media options published by the Windows worker."
            if worker_ref_required
            else "Raw worker paths are enabled only for a private trusted deployment."
        ),
        "message": message,
    }


def save_worker_preview_upload(state: WebState, record: dict[str, Any], request: Request, body: bytes) -> dict[str, Any]:
    if not body:
        raise HTTPException(status_code=400, detail="Preview body is empty")
    worker_id = str(request.headers.get("x-worker-id") or "")
    claimed_by = str(record.get("claimed_by") or "")
    if claimed_by and worker_id and worker_id != claimed_by:
        raise HTTPException(status_code=403, detail="Worker does not own this job")
    variant = str(request.headers.get("x-worker-preview-variant") or "preview").lower()
    if variant not in {"preview", "thumbnail"}:
        raise HTTPException(status_code=400, detail="Unsupported preview variant")
    max_bytes = int(state.webui.get("worker_preview_max_upload_mb", 256) or 256) * 1024 * 1024
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Worker preview exceeds {state.webui.get('worker_preview_max_upload_mb', 256)} MB")

    job_id = safe_job_id(str(record.get("id") or ""))
    filename = safe_upload_name(str(request.headers.get("x-worker-preview-file-name") or f"{variant}.bin"))
    validate_worker_preview_suffix(filename, variant)
    target_dir = ensure_dir(preview_cache_dir(state.config) / safe_preview_id(job_id))
    target = (target_dir / filename).resolve()
    if target.parent != target_dir.resolve():
        raise HTTPException(status_code=400, detail="Invalid preview file name")

    owner = str(record.get("user") or "")
    existing_size = target.stat().st_size if target.exists() else 0
    quota = state.store.quota_status(owner)
    base_used = max(0, int(quota["remote_used_bytes"]) - existing_size)
    quota_bytes = int(quota["remote_quota_bytes"])
    if not upload_fits_quota(base_used, 0, len(body), quota_bytes):
        remaining = max(0, quota_bytes - base_used)
        raise HTTPException(status_code=413, detail=f"Remote quota exceeded. Remaining bytes: {remaining}")
    global_quota_bytes = int(quota.get("remote_global_quota_bytes") or 0)
    global_base_used = max(0, int(quota.get("remote_global_used_bytes") or 0) - existing_size)
    if global_quota_bytes > 0 and not upload_fits_quota(global_base_used, 0, len(body), global_quota_bytes):
        remaining = max(0, global_quota_bytes - global_base_used)
        raise HTTPException(status_code=413, detail=f"Global remote quota exceeded. Remaining bytes: {remaining}")

    target.write_bytes(body)
    row = upsert_worker_preview_manifest(state, record, request, target, variant)
    return row


def save_worker_artifact_cache_upload(state: WebState, record: dict[str, Any], request: Request, body: bytes) -> dict[str, Any]:
    if not body:
        raise HTTPException(status_code=400, detail="Artifact cache body is empty")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if metadata.get("worker_action") != "upload_artifact_cache":
        raise HTTPException(status_code=400, detail="Job is not an artifact cache upload")
    worker_id = str(request.headers.get("x-worker-id") or "")
    claimed_by = str(record.get("claimed_by") or "")
    if claimed_by and worker_id and worker_id != claimed_by:
        raise HTTPException(status_code=403, detail="Worker does not own this job")
    max_bytes = int(state.webui.get("worker_artifact_cache_max_upload_mb", 2048) or 2048) * 1024 * 1024
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Worker artifact cache exceeds {state.webui.get('worker_artifact_cache_max_upload_mb', 2048)} MB")

    job_id = safe_job_id(str(record.get("id") or ""))
    filename = safe_upload_name(str(request.headers.get("x-worker-artifact-file-name") or request.headers.get("x-worker-artifact-name") or "artifact.bin"))
    validate_worker_artifact_suffix(filename)
    target_dir = ensure_dir(preview_cache_dir(state.config) / "artifact_cache" / safe_preview_id(job_id))
    target = (target_dir / filename).resolve()
    if target.parent != target_dir.resolve():
        raise HTTPException(status_code=400, detail="Invalid artifact cache file name")

    owner = str(record.get("user") or "")
    existing_size = target.stat().st_size if target.exists() else 0
    quota = state.store.quota_status(owner)
    base_used = max(0, int(quota["remote_used_bytes"]) - existing_size)
    quota_bytes = int(quota["remote_quota_bytes"])
    if not upload_fits_quota(base_used, 0, len(body), quota_bytes):
        remaining = max(0, quota_bytes - base_used)
        raise HTTPException(status_code=413, detail=f"Remote quota exceeded. Remaining bytes: {remaining}")
    global_quota_bytes = int(quota.get("remote_global_quota_bytes") or 0)
    global_base_used = max(0, int(quota.get("remote_global_used_bytes") or 0) - existing_size)
    if global_quota_bytes > 0 and not upload_fits_quota(global_base_used, 0, len(body), global_quota_bytes):
        remaining = max(0, global_quota_bytes - global_base_used)
        raise HTTPException(status_code=413, detail=f"Global remote quota exceeded. Remaining bytes: {remaining}")

    target.write_bytes(body)
    row = upsert_worker_artifact_cache_manifest(state, record, request, target)
    return row


def enforce_artifact_cache_request_limits(state: WebState, username: str, artifact: dict[str, Any]) -> None:
    size = int(max(0, float(artifact.get("size") or 0)))
    if size <= 0:
        return
    max_bytes = int(float(state.webui.get("worker_artifact_cache_max_upload_mb", 2048) or 2048) * 1024 * 1024)
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"Worker artifact cache exceeds {state.webui.get('worker_artifact_cache_max_upload_mb', 2048)} MB")
    quota = state.store.quota_status(username)
    remaining = max(0, int(quota["remote_quota_bytes"]) - int(quota["remote_used_bytes"]))
    if size > remaining:
        raise HTTPException(status_code=413, detail=f"Remote quota exceeded. Remaining bytes: {remaining}")
    global_quota = int(quota.get("remote_global_quota_bytes") or 0)
    global_remaining = max(0, global_quota - int(quota.get("remote_global_used_bytes") or 0)) if global_quota else 0
    if global_quota > 0 and size > global_remaining:
        raise HTTPException(status_code=413, detail=f"Global remote quota exceeded. Remaining bytes: {global_remaining}")


def upsert_worker_preview_manifest(
    state: WebState,
    record: dict[str, Any],
    request: Request,
    target: Path,
    variant: str,
) -> dict[str, Any]:
    manifest_path = preview_manifest_path(state.config)
    ensure_dir(manifest_path.parent)
    preview_id = safe_preview_id(str(request.headers.get("x-worker-preview-id") or f"{record.get('id')}_{variant}"))
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    rows: list[dict[str, Any]]
    with state.lock:
        if manifest_path.exists():
            try:
                data = read_json(manifest_path)
                raw_rows = data.get("previews", data) if isinstance(data, dict) else data
            except Exception:
                raw_rows = []
        else:
            raw_rows = []
        rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
        row = next((item for item in rows if str(item.get("id") or "") == preview_id), None)
        if row is None:
            row = {"id": preview_id}
            rows.append(row)
        row.update(
            {
                "id": preview_id,
                "kind": "remote_preview",
                "name": safe_preview_display_name(str(request.headers.get("x-worker-preview-name") or target.name)),
                "owner": str(record.get("user") or ""),
                "project_id": str(metadata.get("project_id") or ""),
                "folder_id": str(metadata.get("folder_id") or "root"),
                "job_id": str(record.get("id") or ""),
                "source_output_key": str(request.headers.get("x-worker-preview-source-key") or "zh_dub_mp4"),
                "updated_at": iso_now(),
            }
        )
        row.pop("source_path", None)
        if variant == "preview":
            row["preview_path"] = str(target)
            row["path"] = str(target)
            row["display_path"] = f"preview cache: {target.name}"
        else:
            row["thumbnail_path"] = str(target)
        write_json(manifest_path, {"previews": rows})
        return dict(row)


def upsert_worker_artifact_cache_manifest(state: WebState, record: dict[str, Any], request: Request, target: Path) -> dict[str, Any]:
    manifest_path = preview_manifest_path(state.config)
    ensure_dir(manifest_path.parent)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    artifact_id = safe_preview_id(str(request.headers.get("x-worker-artifact-id") or metadata.get("artifact_id") or f"{record.get('id')}_artifact"))
    rows: list[dict[str, Any]]
    with state.lock:
        if manifest_path.exists():
            try:
                data = read_json(manifest_path)
                raw_rows = data.get("previews", data) if isinstance(data, dict) else data
            except Exception:
                raw_rows = []
        else:
            raw_rows = []
        rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
        row = next((item for item in rows if str(item.get("id") or "") == artifact_id), None)
        if row is None:
            row = {"id": artifact_id}
            rows.append(row)
        row.update(
            {
                "id": artifact_id,
                "kind": str(request.headers.get("x-worker-artifact-source-key") or metadata.get("source_output_key") or "remote_cached_artifact"),
                "name": safe_preview_display_name(str(request.headers.get("x-worker-artifact-name") or metadata.get("artifact_name") or target.name)),
                "owner": str(record.get("user") or ""),
                "project_id": str(metadata.get("project_id") or ""),
                "folder_id": str(metadata.get("folder_id") or "root"),
                "job_id": str(metadata.get("source_job_id") or record.get("id") or ""),
                "cache_job_id": str(record.get("id") or ""),
                "artifact_ref_id": str(request.headers.get("x-worker-artifact-ref") or metadata.get("artifact_ref_id") or ""),
                "source_output_key": str(request.headers.get("x-worker-artifact-source-key") or metadata.get("source_output_key") or ""),
                "preview_path": str(target),
                "path": str(target),
                "display_path": f"remote cache: {target.name}",
                "remote_cache": True,
                "full_available": True,
                "updated_at": iso_now(),
            }
        )
        row.pop("source_path", None)
        write_json(manifest_path, {"previews": rows})
        return dict(row)


def validate_worker_preview_suffix(filename: str, variant: str) -> None:
    suffix = Path(filename).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp"} if variant == "thumbnail" else {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported worker preview file type: {suffix}")


def validate_worker_artifact_suffix(filename: str) -> None:
    suffix = Path(filename).suffix.lower()
    allowed = ALLOWED_UPLOAD_SUFFIXES | {".json", ".tsv", ".txt", ".md"}
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported worker artifact cache file type: {suffix}")


def safe_preview_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._-")
    return cleaned[:120] or uuid.uuid4().hex[:12]


def safe_preview_display_name(name: str) -> str:
    return safe_upload_name(name)[:180]


def active_job_counts(state: WebState, user: str) -> dict[str, int]:
    user_active = 0
    global_active = 0
    for record in list_jobs(state, None):
        if record.get("status") not in ACTIVE_JOB_STATUSES:
            continue
        global_active += 1
        if record.get("user") == user:
            user_active += 1
    return {"user": user_active, "global": global_active}


def filter_job_records(
    records: list[dict[str, Any]],
    *,
    project_id: str = "",
    folder_id: str = "",
    status: str = "",
) -> list[dict[str, Any]]:
    project = str(project_id or "").strip()
    folder = str(folder_id or "").strip()
    status_filter = str(status or "").strip()
    normalized_status = normalize_job_status(status_filter) if status_filter else ""
    out: list[dict[str, Any]] = []
    for record in records:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if project and str(metadata.get("project_id") or "") != project:
            continue
        if folder and folder != "all" and str(metadata.get("folder_id") or "root") != folder:
            continue
        if normalized_status and normalize_job_status(str(record.get("status") or "")) != normalized_status:
            continue
        out.append(record)
    return out


def job_queue_summary(
    state: WebState,
    user: str,
    *,
    worker: dict[str, Any] | None = None,
    jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    visible_jobs = jobs if jobs is not None else list_jobs(state, user)
    status_counts = {status: 0 for status in NORMALIZED_JOB_STATUSES}
    for record in visible_jobs:
        status = normalize_job_status(str(record.get("status") or "queued"))
        status_counts[status] = status_counts.get(status, 0) + 1

    all_jobs = list_jobs(state, None)
    counts = active_job_counts(state, user)
    worker = worker or worker_status_payload(state)
    max_slots = max(1, min(8, coerce_int(worker.get("max_concurrent_jobs"), 1)))
    worker_busy = sum(
        1
        for record in all_jobs
        if record.get("dispatch_target") == "worker" and normalize_job_status(str(record.get("status") or "")) in {"claimed", "running"}
    )
    worker_waiting = sum(
        1
        for record in all_jobs
        if record.get("dispatch_target") == "worker" and normalize_job_status(str(record.get("status") or "")) in {"queued", "retrying"}
    )
    slots_used = min(max_slots, worker_busy)
    return {
        "visible_total": sum(status_counts.values()),
        "status_counts": status_counts,
        "active_user": counts["user"],
        "active_global": counts["global"],
        "worker_max_slots": max_slots,
        "worker_slots_used": slots_used,
        "worker_slots_available": max(0, max_slots - slots_used),
        "worker_busy_jobs": worker_busy,
        "worker_waiting_jobs": worker_waiting,
        "worker_online": bool(worker.get("heartbeat_online")),
    }


def enforce_active_job_limits(state: WebState, user: str) -> None:
    max_user = int(state.webui.get("max_active_jobs_per_user", 2) or 0)
    max_global = int(state.webui.get("max_active_jobs_global", 8) or 0)
    counts = active_job_counts(state, user)
    if max_user > 0 and counts["user"] >= max_user:
        raise HTTPException(status_code=429, detail=f"Active job limit reached for user: {counts['user']}/{max_user}")
    if max_global > 0 and counts["global"] >= max_global:
        raise HTTPException(status_code=429, detail=f"Global active job limit reached: {counts['global']}/{max_global}")


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def require_worker_request(request: Request, state: WebState) -> dict[str, Any]:
    body = await request.body()
    require_worker_token(request, state, body)
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid worker JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Worker payload must be a JSON object")
    return payload


def require_worker_token(request: Request, state: WebState, body: bytes = b"") -> None:
    token = request.headers.get("x-worker-token", "")
    expected = str(state.webui.get("worker_token") or os.environ.get("WORKER_SHARED_TOKEN") or "")
    if not expected:
        raise HTTPException(status_code=503, detail="Worker token is not configured")
    mode = str(state.webui.get("worker_auth_mode", "hmac_or_token") or "hmac_or_token").lower()
    signature = request.headers.get("x-worker-signature", "")
    timestamp = request.headers.get("x-worker-timestamp", "")
    if signature or timestamp:
        require_worker_hmac(request, expected, body)
        return
    if mode in {"hmac", "signed", "signature"}:
        raise HTTPException(status_code=401, detail="Worker HMAC signature is required")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid worker token")


def require_worker_hmac(request: Request, expected_token: str, body: bytes) -> None:
    timestamp = request.headers.get("x-worker-timestamp", "")
    signature = request.headers.get("x-worker-signature", "")
    nonce = request.headers.get("x-worker-nonce", "").strip()
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Worker HMAC timestamp and signature are required")
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid worker HMAC timestamp") from exc
    state = request.app.state.web
    max_skew = int(state.webui.get("worker_signature_max_skew_seconds", 300) or 300)
    if abs(int(time.time()) - timestamp_int) > max(30, max_skew):
        raise HTTPException(status_code=401, detail="Worker HMAC timestamp is outside the allowed window")
    mode = str(state.webui.get("worker_auth_mode", "hmac_or_token") or "hmac_or_token").lower()
    if worker_hmac_requires_nonce(state, mode) and not nonce:
        raise HTTPException(status_code=401, detail="Worker HMAC nonce is required")
    if nonce and not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", nonce):
        raise HTTPException(status_code=401, detail="Invalid worker HMAC nonce")
    expected_signature = worker_hmac_signature(
        expected_token,
        timestamp=timestamp,
        method=request.method,
        path=request.url.path,
        body=body,
        nonce=nonce or None,
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid worker HMAC signature")
    if nonce:
        remember_worker_nonce(state, nonce, timestamp_int=timestamp_int, max_skew_seconds=max_skew)


def worker_hmac_requires_nonce(state: WebState, mode: str) -> bool:
    configured = state.webui.get("worker_require_nonce")
    if configured is not None:
        if isinstance(configured, str):
            return configured.strip().lower() in {"1", "true", "yes", "on", "required"}
        return bool(configured)
    return mode in {"hmac", "signed", "signature"}


def remember_worker_nonce(state: WebState, nonce: str, *, timestamp_int: int, max_skew_seconds: int) -> None:
    now_epoch = int(time.time())
    cutoff = now_epoch - max(30, max_skew_seconds)
    with state.lock:
        for cached_nonce, cached_timestamp in list(state.worker_nonce_cache.items()):
            if int(cached_timestamp) < cutoff:
                state.worker_nonce_cache.pop(cached_nonce, None)
        if nonce in state.worker_nonce_cache:
            raise HTTPException(status_code=401, detail="Worker HMAC nonce has already been used")
        state.worker_nonce_cache[nonce] = timestamp_int


def worker_hmac_signature(worker_token: str, *, timestamp: str, method: str, path: str, body: bytes, nonce: str | None = None) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    parts = [str(timestamp), method.upper(), path]
    if nonce:
        parts.append(str(nonce))
    parts.append(body_hash)
    message = "\n".join(parts).encode("utf-8")
    return hmac.new(worker_token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def claim_worker_job(state: WebState, worker_id: str) -> dict[str, Any] | None:
    with state.lock:
        requeue_stale_worker_jobs_locked(state)
        for path in sorted(state.job_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                record = normalize_job_record(state, read_json(path), path=path, persist=True)
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


def requeue_stale_worker_jobs(state: WebState) -> list[dict[str, Any]]:
    with state.lock:
        return requeue_stale_worker_jobs_locked(state)


def requeue_stale_worker_jobs_locked(state: WebState) -> list[dict[str, Any]]:
    webui = state.webui
    if not bool(webui.get("worker_requeue_stale_jobs", True)):
        return []
    timeout_seconds = max(30, int(webui.get("worker_job_heartbeat_timeout_seconds", 300) or 300))
    max_auto_retries = max(0, int(webui.get("worker_job_max_auto_retries", 2) or 0))
    now_epoch = time.time()
    changed: list[dict[str, Any]] = []
    for path in sorted(state.job_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            record = normalize_job_record(state, read_json(path), path=path, persist=False)
        except Exception:
            continue
        if record.get("dispatch_target") != "worker":
            continue
        status = str(record.get("status") or "")
        if status not in {"claimed", "running"}:
            continue
        age_seconds = int(now_epoch - path.stat().st_mtime)
        if age_seconds < timeout_seconds:
            continue
        retry_count = int(record.get("retry_count") or 0)
        previous_status = status
        reason = f"Worker job stale for {age_seconds}s without status update"
        if record.get("cancel_requested"):
            record.update(
                {
                    "status": "cancelled",
                    "previous_status": previous_status,
                    "ended_at": iso_now(),
                    "returncode": -9,
                    "error": f"Cancelled after stale worker control timeout: {age_seconds}s",
                    "cancel_handled_at": iso_now(),
                    "updated_at": iso_now(),
                }
            )
        elif retry_count < max_auto_retries:
            record.update(
                {
                    "status": "retrying",
                    "retry_count": retry_count + 1,
                    "previous_status": previous_status,
                    "last_claimed_by": record.get("claimed_by"),
                    "claimed_by": None,
                    "claimed_at": None,
                    "pid": None,
                    "worker_requeue_reason": reason,
                    "worker_requeued_at": iso_now(),
                    "updated_at": iso_now(),
                }
            )
        else:
            record.update(
                {
                    "status": "failed",
                    "previous_status": previous_status,
                    "ended_at": iso_now(),
                    "returncode": -2,
                    "error": f"{reason}; max auto retries reached",
                    "updated_at": iso_now(),
                }
            )
        record = normalize_job_record(state, record, path=path, persist=False)
        write_json(path, record)
        changed.append(record)
    return changed


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
        "worker_artifacts",
        "metrics",
        "command",
    ]:
        if key in body:
            if key == "metrics":
                changes[key] = sanitize_metrics(body[key])
            elif key == "command":
                changes[key] = sanitize_remote_command(body[key])
            elif key in {"log_tail", "error"}:
                changes[key] = sanitize_remote_text(body[key])
            elif key in {"result", "worker_artifacts"}:
                changes[key] = sanitize_remote_value(body[key])
            elif key in {"result_report", "result_video"}:
                changes[key] = sanitize_remote_text(file_display_name(str(body[key])))
            else:
                changes[key] = body[key]
    result = body.get("result")
    if isinstance(result, dict):
        if result.get("report"):
            changes["result_report"] = sanitize_remote_text(file_display_name(str(result.get("report"))))
        if result.get("video"):
            changes["result_video"] = sanitize_remote_text(file_display_name(str(result.get("video"))))
    return changes


def worker_control_payload(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status") or "")
    cancel_requested = bool(record.get("cancel_requested")) and status in {"claimed", "running", "paused"}
    return {
        "job_id": str(record.get("id") or ""),
        "status": status,
        "cancel_requested": cancel_requested,
        "cancel_requested_at": record.get("cancel_requested_at"),
        "cancel_requested_by": record.get("cancel_requested_by"),
    }


def require_claimed_worker(record: dict[str, Any], worker_id: str, *, action: str) -> None:
    if record.get("dispatch_target") != "worker":
        raise HTTPException(status_code=409, detail=f"Cannot {action}: job is not a worker job")
    status = normalize_job_status(str(record.get("status") or ""))
    if status in {"queued", "retrying", "paused"}:
        raise HTTPException(status_code=409, detail=f"Cannot {action}: job is not claimed by a worker")
    if status in {"done", "failed", "cancelled", "deleted"}:
        raise HTTPException(status_code=409, detail=f"Cannot {action}: job is already {status}")
    claimed_by = str(record.get("claimed_by") or "")
    if claimed_by and not worker_id:
        raise HTTPException(status_code=400, detail="worker_id is required for claimed worker jobs")
    if claimed_by and worker_id != claimed_by:
        raise HTTPException(status_code=409, detail=f"Cannot {action}: job is claimed by another worker")


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


def enforce_storage_quota_before_job(state: WebState, user: str, metadata: dict[str, Any]) -> None:
    quota = state.store.quota_status(user)
    local_quota = int(quota.get("local_quota_bytes") or 0)
    local_used = int(quota.get("local_used_bytes") or 0)
    if local_quota > 0 and local_used >= local_quota:
        raise HTTPException(
            status_code=413,
            detail=(
                "Local worker quota exceeded. "
                f"Used bytes: {local_used}; quota bytes: {local_quota}; source: {quota.get('local_usage_source')}"
            ),
        )

    project_id = str(metadata.get("project_id") or "")
    if not project_id:
        return
    project = state.store.get_project(user, project_id, admin=is_admin(state, user))
    if not project:
        return
    project_quota = int(project.get("quota_project_bytes") or 0)
    if project_quota <= 0:
        return
    usage = project_artifact_usage(state, user, admin=is_admin(state, user))
    project_used = int(usage.get(project_id, 0))
    if project_used >= project_quota:
        raise HTTPException(
            status_code=413,
            detail=f"Project quota exceeded. Used bytes: {project_used}; quota bytes: {project_quota}",
        )


def projects_with_usage(state: WebState, user: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    admin = is_admin(state, user)
    projects = state.store.list_projects(user, admin=admin, include_archived=include_archived)
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


def effective_language_capabilities(
    state: WebState,
    *,
    llm: Any | None = None,
    tts: dict[str, Any] | None = None,
    worker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution_mode = str(state.webui.get("execution_mode", "local_subprocess") or "local_subprocess")
    if execution_mode == "worker_queue":
        worker = worker or worker_status_payload(state)
        worker_caps = worker.get("capabilities") if isinstance(worker, dict) else {}
        if worker.get("heartbeat_online") and isinstance(worker_caps, dict) and worker_caps:
            caps = dict(worker_caps)
            caps["source"] = "worker_heartbeat"
            caps["worker_id"] = worker.get("worker_id")
            return caps
    caps = language_capabilities(state.config, llm_status=llm or LocalLLMClient(state.config).status(), tts_status=tts or tts_health(state.config))
    caps["source"] = "webui_config"
    return caps


def dashboard_metrics(state: WebState, worker: dict[str, Any] | None = None) -> dict[str, Any]:
    execution_mode = str(state.webui.get("execution_mode", "local_subprocess") or "local_subprocess")
    if execution_mode == "worker_queue":
        worker = worker or worker_status_payload(state)
        metrics = worker.get("metrics") if isinstance(worker, dict) else {}
        if isinstance(metrics, dict) and metrics:
            out = sanitize_metrics(metrics)
            out["source"] = "worker_heartbeat" if worker.get("heartbeat_online") else "worker_heartbeat_stale"
            return out
        return {
            "source": "worker_unavailable",
            "cpu": {},
            "memory": {},
            "gpu": [{"available": False, "error": "No worker heartbeat metrics"}],
            "disk": {},
            "local_storage": {"managed_bytes": 0, "total_reported_bytes": 0, "roots": [], "partial": False},
        }
    out = sanitize_metrics(collect_system_metrics(state.config))
    out["source"] = "webui_host"
    return out


def worker_args_from_command(command: list[str]) -> list[str]:
    try:
        idx = command.index("ecse_localizer")
    except ValueError:
        return command
    args = command[idx + 1 :]
    if len(args) >= 2 and args[0] == "--config":
        args = args[2:]
    return args


def public_job_record(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    row = sanitize_remote_value(dict(record))
    row["command"] = sanitize_remote_command(record.get("command"))
    metadata = dict(record.get("metadata") or {}) if isinstance(record.get("metadata"), dict) else {}
    if isinstance(metadata.get("worker_args"), list):
        metadata["worker_args"] = sanitize_remote_command(metadata["worker_args"])
    row["metadata"] = sanitize_remote_value(metadata)
    return row


def validate_worker_queue_job_body(job_type: str, body: dict[str, Any], state: WebState) -> None:
    if job_type != "process_one":
        return
    video = str(body.get("video") or "").strip()
    if not video or is_remote_safe_reference(video):
        return
    if bool(state.webui.get("allow_worker_path_submission", False)):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "Remote worker_queue process-one requires a worker-ref:<id> video reference. "
            "Set webui.allow_worker_path_submission=true only for a private trusted deployment."
        ),
    )


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


def list_all_video_records(state: WebState, user: str) -> list[dict[str, Any]]:
    execution_mode = str(state.webui.get("execution_mode", "local_subprocess"))
    include_local_paths = execution_mode != "worker_queue" or bool(state.webui.get("allow_worker_path_submission", False))
    records = list_video_records(state.config, state.store.user_upload_dir(user)) if include_local_paths else []
    if not is_admin(state, user):
        records = [public_video_record(state, user, row) for row in records]
    if execution_mode == "worker_queue":
        records.extend(worker_media_video_records(state))
    return records


def public_video_record(state: WebState, user: str, row: dict[str, Any]) -> dict[str, Any]:
    name = safe_upload_name(str(row.get("name") or "video.mp4"))
    uploaded = bool(row.get("uploaded"))
    return {
        "name": name,
        "path": video_ref_for_path(state, user, str(row.get("path") or "")),
        "size": int(row.get("size", 0) or 0),
        "uploaded": uploaded,
        "local_video_ref": True,
        "display_path": f"{'uploaded media' if uploaded else 'course media'}: {name}",
    }


def video_ref_for_path(state: WebState, user: str, path: str) -> str:
    resolved = str(Path(path).resolve()).lower()
    secret = download_secret(state)
    body = f"{user}\n{resolved}".encode("utf-8", errors="ignore")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()[:24]
    return f"video-ref:{digest}"


def resolve_video_reference(state: WebState, user: str, video: str) -> str:
    value = str(video or "").strip()
    if not value.startswith("video-ref:"):
        return value
    records = list_video_records(state.config, state.store.user_upload_dir(user))
    for row in records:
        path = str(row.get("path") or "")
        if video_ref_for_path(state, user, path) == value:
            return path
    raise HTTPException(status_code=404, detail="Video reference not found")


def worker_media_video_records(state: WebState) -> list[dict[str, Any]]:
    worker = worker_status_payload(state)
    media_refs = worker.get("media_refs") if isinstance(worker, dict) else []
    if not isinstance(media_refs, list):
        return []
    records: list[dict[str, Any]] = []
    for item in media_refs:
        if not isinstance(item, dict) or not item.get("ref_id"):
            continue
        name = safe_upload_name(str(item.get("name") or "worker-media.mp4"))
        records.append(
            {
                "name": name,
                "path": f"worker-ref:{item.get('ref_id')}",
                "size": int(item.get("size", 0) or 0),
                "uploaded": False,
                "worker_ref": True,
                "media_type": str(item.get("media_type") or ""),
                "display_path": f"Windows worker: {name}",
            }
        )
    return records


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


def list_visible_reports(state: WebState, user: str, limit: int = 50) -> list[dict[str, Any]]:
    admin = is_admin(state, user)
    reports = list_reports(state.config, limit=10000)
    jobs_by_report: dict[str, dict[str, Any]] = {}
    for job in list_jobs(state, None):
        report = str(job.get("result_report") or "")
        if report:
            jobs_by_report[str(Path(report).resolve()).lower()] = job
    visible: list[dict[str, Any]] = []
    for report in reports:
        report_path = str(report.get("path") or "")
        job = jobs_by_report.get(str(Path(report_path).resolve()).lower()) if report_path else None
        owner = str((job or {}).get("user") or report.get("owner") or "")
        if admin or owner == user:
            row = dict(report)
            row["owner"] = owner
            visible.append(row)
        if len(visible) >= limit:
            break
    return visible


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
        "owner": data.get("user") or "",
        "project_id": data.get("project_id") or "",
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
        display_name = str(body.get("video_name") or file_display_name(video))
        return base + ["process-one", "--video", video], f"Process one: {file_display_name(display_name)}"
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
        return cmd, f"Compact rerender: {file_display_name(report)}"
    if job_type == "fidelity_audit":
        report = str(body.get("report") or "")
        if not report or (validate_paths and not Path(report).exists()):
            raise HTTPException(status_code=400, detail="Report path is required")
        return base + ["fidelity-audit", "--report", report], f"Fidelity audit: {file_display_name(report)}"
    if job_type == "repair_fidelity":
        report = str(body.get("report") or "")
        if not report or (validate_paths and not Path(report).exists()):
            raise HTTPException(status_code=400, detail="Report path is required")
        cmd = base + ["repair-fidelity", "--report", report]
        fidelity_report = str(body.get("fidelity_report") or "")
        if fidelity_report:
            if validate_paths and not Path(fidelity_report).exists():
                raise HTTPException(status_code=400, detail="Fidelity report path is invalid")
            cmd += ["--fidelity-report", fidelity_report]
        max_score = body.get("max_score")
        if max_score not in (None, ""):
            cmd += ["--max-score", str(max(1, min(5, int(max_score))))]
        if bool(body.get("skip_high")):
            cmd.append("--skip-high")
        return cmd, f"Fidelity repair: {file_display_name(report)}"
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
        "schema_version": JOB_SCHEMA_VERSION,
        "id": job_id,
        "user": user,
        "type": job_type,
        "title": title,
        "status": "queued",
        "dispatch_target": dispatch_target,
        "queued_for_worker": dispatch_target == "worker",
        "created_at": iso_now(),
        "updated_at": iso_now(),
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


def normalize_job_record(
    state: WebState,
    record: dict[str, Any],
    *,
    path: Path | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    before = stable_json(record)
    row = dict(record) if isinstance(record, dict) else {}
    job_id = str(row.get("id") or (path.stem if path else "") or now_id("webui_legacy"))
    row["schema_version"] = JOB_SCHEMA_VERSION
    row["id"] = safe_job_id(job_id)
    row["type"] = str(row.get("type") or infer_job_type(row)).strip() or "unknown"
    row["title"] = str(row.get("title") or title_for_job(row)).strip()

    raw_status = str(row.get("status") or "queued").strip().lower()
    status = normalize_job_status(raw_status)
    if raw_status and raw_status != status and not row.get("legacy_status"):
        row["legacy_status"] = raw_status
    row["status"] = status

    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {"legacy_metadata": metadata} if metadata not in (None, "") else {}
    row["metadata"] = metadata

    command = normalize_command(row.get("command"))
    row["command"] = command

    dispatch = str(row.get("dispatch_target") or "").strip().lower()
    if dispatch not in {"local", "worker"}:
        if bool(row.get("queued_for_worker")) or isinstance(metadata.get("worker_args"), list):
            dispatch = "worker"
        else:
            dispatch = "local"
    row["dispatch_target"] = dispatch
    row["queued_for_worker"] = dispatch == "worker"

    if dispatch == "worker" and "worker_args" not in metadata and command:
        metadata["worker_args"] = worker_args_from_command(command)

    created_at = str(row.get("created_at") or row.get("submitted_at") or timestamp_from_path(path) or iso_now())
    row["created_at"] = created_at
    row["updated_at"] = str(row.get("updated_at") or row.get("ended_at") or row.get("started_at") or created_at)
    row.setdefault("started_at", None)
    row.setdefault("ended_at", None)
    row.setdefault("returncode", None)
    row.setdefault("pid", None)
    row.setdefault("retry_count", 0)
    row.setdefault("log", str(state.job_dir / f"{row['id']}.log"))

    if persist and path and stable_json(row) != before:
        write_json(path, row)
    return row


def normalize_job_status(status: str) -> str:
    cleaned = re.sub(r"[\s-]+", "_", (status or "").strip().lower())
    cleaned = JOB_STATUS_ALIASES.get(cleaned, cleaned)
    if cleaned in NORMALIZED_JOB_STATUSES:
        return cleaned
    return "failed" if any(token in cleaned for token in ("fail", "error")) else "queued"


def normalize_command(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    if isinstance(command, str) and command.strip():
        try:
            return [str(item).strip('"') for item in shlex.split(command, posix=False)]
        except ValueError:
            return [command]
    return []


def infer_job_type(record: dict[str, Any]) -> str:
    command = normalize_command(record.get("command"))
    for item in command:
        normalized = item.replace("-", "_")
        if normalized in {"audit", "smoke", "process_one", "process_all", "report", "compact_rerender", "fidelity_audit", "repair_fidelity"}:
            return normalized
    job_id = str(record.get("id") or "")
    match = re.match(r"webui_([A-Za-z0-9_]+)_\d{8}_\d{6}", job_id)
    return match.group(1) if match else "unknown"


def title_for_job(record: dict[str, Any]) -> str:
    job_type = str(record.get("type") or infer_job_type(record) or "unknown")
    if record.get("source_video"):
        return f"{job_type}: {file_display_name(str(record['source_video']))}"
    return f"Job: {job_type}"


def file_display_name(path_text: str) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    windows_name = PureWindowsPath(text).name
    posix_name = Path(text).name
    if "\\" in text and windows_name:
        return windows_name
    return posix_name or windows_name or text


def timestamp_from_path(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def read_job(state: WebState, job_id: str) -> dict[str, Any] | None:
    path = state.job_dir / f"{safe_job_id(job_id)}.json"
    if not path.exists():
        return None
    return normalize_job_record(state, read_json(path), path=path, persist=True)


def update_job(state: WebState, job_id: str, changes: dict[str, Any]) -> None:
    path = state.job_dir / f"{safe_job_id(job_id)}.json"
    if not path.exists():
        return
    with state.lock:
        record = normalize_job_record(state, read_json(path), path=path, persist=False)
        record.update(changes)
        record = normalize_job_record(state, record, path=path, persist=False)
        write_json(path, record)


def list_jobs(state: WebState, user: str | None = None, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    if str(state.webui.get("execution_mode", "")) == "worker_queue":
        requeue_stale_worker_jobs(state)
    records = []
    for path in sorted(state.job_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = normalize_job_record(state, read_json(path), path=path, persist=True)
            if not include_deleted and record.get("status") == "deleted":
                continue
            if user and not can_access_record(state, user, record):
                continue
            records.append(record)
        except Exception:
            continue
    return records


def pause_job_record(state: WebState, job_id: str, *, paused_by: str) -> dict[str, Any]:
    record = read_job(state, job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.get("dispatch_target") != "worker":
        raise HTTPException(status_code=409, detail="Only queued worker jobs can be paused")
    status = str(record.get("status") or "")
    if status not in {"queued", "retrying"}:
        raise HTTPException(status_code=409, detail=f"Job cannot be paused from status: {status}")
    update_job(
        state,
        job_id,
        {
            "status": "paused",
            "previous_status": status,
            "paused_at": iso_now(),
            "paused_by": paused_by,
            "updated_at": iso_now(),
        },
    )
    return read_job(state, job_id) or record


def resume_job_record(state: WebState, job_id: str, *, resumed_by: str) -> dict[str, Any]:
    record = read_job(state, job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.get("dispatch_target") != "worker":
        raise HTTPException(status_code=409, detail="Only paused worker jobs can be resumed")
    status = str(record.get("status") or "")
    if status != "paused":
        raise HTTPException(status_code=409, detail=f"Job cannot be resumed from status: {status}")
    previous_status = str(record.get("previous_status") or "queued")
    target_status = "retrying" if previous_status == "retrying" else "queued"
    update_job(
        state,
        job_id,
        {
            "status": target_status,
            "previous_status": "paused",
            "resumed_at": iso_now(),
            "resumed_by": resumed_by,
            "updated_at": iso_now(),
            "cancel_requested": False,
            "cancel_requested_at": None,
            "cancel_requested_by": None,
            "cancel_handled_at": None,
        },
    )
    return read_job(state, job_id) or record


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
        "cancel_requested": False,
        "cancel_requested_at": None,
        "cancel_requested_by": None,
        "cancel_handled_at": None,
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


def restore_deleted_job(state: WebState, job_id: str, *, restored_by: str) -> dict[str, Any]:
    record = read_job(state, job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    status = normalize_job_status(str(record.get("status") or ""))
    if status != "deleted":
        raise HTTPException(status_code=409, detail=f"Job is not deleted: {status}")
    previous_status = normalize_job_status(str(record.get("previous_status") or "failed"))
    restored_status = previous_status if previous_status in TERMINAL_JOB_STATUSES else "failed"
    changes: dict[str, Any] = {
        "status": restored_status,
        "previous_status": "deleted",
        "restored_at": iso_now(),
        "restored_by": restored_by,
        "updated_at": iso_now(),
        "cancel_requested": False,
        "cancel_requested_at": None,
        "cancel_requested_by": None,
        "cancel_handled_at": None,
    }
    if restored_status == "failed" and not record.get("error"):
        changes["error"] = "Restored from deleted history; retry manually if this job should run again."
    update_job(state, job_id, changes)
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
