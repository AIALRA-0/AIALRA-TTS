from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from .metrics import sanitize_metrics
from .redaction import sanitize_remote_text, sanitize_remote_value
from .utils import PROJECT_ROOT, ensure_dir, read_json, write_json


PBKDF2_ROUNDS = 210_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
TEMPLATE_PARAM_KEYS = {
    "source_language",
    "target_subtitle_language",
    "target_tts_language",
    "quality_mode",
    "style",
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
}


class PlatformStore:
    """Small JSON-backed platform store for the local/remote control plane.

    The media pipeline remains file-based; this store only keeps users,
    projects, quota metadata, and worker heartbeat state. The default location
    is under runs/, which is intentionally ignored by git.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        web = config.setdefault("webui", {})
        self.root = ensure_dir(web.get("platform_dir") or PROJECT_ROOT / "runs" / "platform")
        self.users_path = self.root / "users.json"
        self.projects_path = self.root / "projects.json"
        self.templates_path = self.root / "templates.json"
        self.worker_path = self.root / "worker_status.json"

    def bootstrap(self) -> None:
        users = self._load(self.users_path, {"users": []})
        if not users.get("users"):
            web = self.config.get("webui", {})
            username = str(web.get("username") or "admin")
            password = str(web.get("password") or os.environ.get("WEBUI_ADMIN_PASSWORD") or "")
            if not password:
                password = uuid.uuid4().hex + uuid.uuid4().hex
            users["users"].append(
                {
                    "id": safe_id(username),
                    "username": username,
                    "password_hash": hash_password(password),
                    "role": "admin",
                    "disabled": False,
                    "quota_local_bytes": gb_to_bytes(float(web.get("default_local_quota_gb", 500))),
                    "quota_remote_bytes": gb_to_bytes(float(web.get("default_remote_quota_gb", 10))),
                    "created_at": iso_now(),
                }
            )
            self._save(self.users_path, users)
        for user in users.get("users", []):
            username = str(user.get("username", ""))
            self.ensure_default_project(username)
            self.ensure_default_template(username)

    def verify_user(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.get_user(username, include_hash=True)
        if not user or user.get("disabled"):
            return None
        if verify_password(password, str(user.get("password_hash", ""))):
            return public_user(user)
        return None

    def get_user(self, username: str, *, include_hash: bool = False) -> dict[str, Any] | None:
        users = self._load(self.users_path, {"users": []})
        for user in users.get("users", []):
            if str(user.get("username", "")).lower() == username.lower():
                return dict(user) if include_hash else public_user(user)
        return None

    def list_users(self) -> list[dict[str, Any]]:
        users = self._load(self.users_path, {"users": []})
        return [public_user(user) for user in users.get("users", [])]

    def create_user(
        self,
        username: str,
        password: str,
        *,
        role: str = "user",
        quota_local_gb: float = 500,
        quota_remote_gb: float = 10,
    ) -> dict[str, Any]:
        validate_username(username)
        if len(password) < 10:
            raise ValueError("Password must be at least 10 characters")
        users = self._load(self.users_path, {"users": []})
        if any(str(u.get("username", "")).lower() == username.lower() for u in users.get("users", [])):
            raise ValueError(f"User already exists: {username}")
        row = {
            "id": safe_id(username),
            "username": username,
            "password_hash": hash_password(password),
            "role": role if role in {"admin", "user"} else "user",
            "disabled": False,
            "quota_local_bytes": gb_to_bytes(quota_local_gb),
            "quota_remote_bytes": gb_to_bytes(quota_remote_gb),
            "created_at": iso_now(),
        }
        users.setdefault("users", []).append(row)
        self._save(self.users_path, users)
        self.ensure_default_project(username)
        self.ensure_default_template(username)
        return public_user(row)

    def update_user(
        self,
        username: str,
        *,
        role: str | None = None,
        disabled: bool | None = None,
        quota_local_gb: float | None = None,
        quota_remote_gb: float | None = None,
    ) -> dict[str, Any]:
        users = self._load(self.users_path, {"users": []})
        rows = users.get("users", [])
        target: dict[str, Any] | None = None
        for row in rows:
            if str(row.get("username", "")).lower() == username.lower():
                target = row
                break
        if not target:
            raise ValueError("User not found")

        updated = dict(target)
        if role is not None:
            updated["role"] = role if role in {"admin", "user"} else "user"
        if disabled is not None:
            updated["disabled"] = bool(disabled)
        if quota_local_gb is not None:
            updated["quota_local_bytes"] = gb_to_bytes(quota_local_gb)
        if quota_remote_gb is not None:
            updated["quota_remote_bytes"] = gb_to_bytes(quota_remote_gb)
        updated["updated_at"] = iso_now()

        simulated = [updated if row is target else row for row in rows]
        if not any(row.get("role") == "admin" and not row.get("disabled") for row in simulated):
            raise ValueError("At least one active admin user is required")

        target.clear()
        target.update(updated)
        self._save(self.users_path, users)
        return public_user(target)

    def list_projects(self, username: str, *, admin: bool = False, include_archived: bool = False) -> list[dict[str, Any]]:
        data = self._load(self.projects_path, {"projects": []})
        rows = data.get("projects", [])
        changed = False
        for row in rows:
            changed = self._normalize_project(row) or changed
        if changed:
            self._save(self.projects_path, data)
        visible = [project_public_view(row, include_archived=include_archived) for row in rows if include_archived or not row.get("archived_at")]
        if admin:
            return visible
        return [p for p in visible if p.get("owner") == username]

    def create_project(self, username: str, name: str, *, description: str = "", quota_project_gb: float | None = None) -> dict[str, Any]:
        clean = clean_name(name, default="Untitled Project")
        data = self._load(self.projects_path, {"projects": []})
        row = {
            "id": f"prj_{uuid.uuid4().hex[:12]}",
            "owner": username,
            "name": clean,
            "description": description[:500],
            "folders": [{"id": "root", "name": "Root"}],
            "quota_project_bytes": gb_to_bytes(self.default_project_quota_gb() if quota_project_gb is None else quota_project_gb),
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
        data.setdefault("projects", []).append(row)
        self._save(self.projects_path, data)
        return row

    def create_folder(self, username: str, project_id: str, name: str, *, parent_id: str = "root", admin: bool = False) -> dict[str, Any]:
        clean = clean_name(name, default="Untitled Folder")
        data = self._load(self.projects_path, {"projects": []})
        for project in data.get("projects", []):
            self._normalize_project(project)
            if project.get("id") != project_id:
                continue
            if not admin and project.get("owner") != username:
                raise ValueError("Project not found")
            if project.get("archived_at"):
                raise ValueError("Project is archived")
            folders = project.setdefault("folders", [{"id": "root", "name": "Root"}])
            if not any(folder.get("id") == parent_id and not folder.get("archived_at") for folder in folders):
                raise ValueError(f"Parent folder not found: {parent_id}")
            if any(
                not folder.get("archived_at")
                and folder.get("parent_id", "root") == parent_id
                and str(folder.get("name", "")).lower() == clean.lower()
                for folder in folders
            ):
                raise ValueError(f"Folder already exists: {clean}")
            row = {
                "id": f"fld_{uuid.uuid4().hex[:12]}",
                "name": clean,
                "parent_id": parent_id,
                "created_at": iso_now(),
            }
            folders.append(row)
            project["updated_at"] = iso_now()
            self._save(self.projects_path, data)
            return row
        raise ValueError("Project not found")

    def get_project(self, username: str, project_id: str, *, admin: bool = False) -> dict[str, Any] | None:
        for project in self.list_projects(username, admin=admin):
            if project.get("id") == project_id:
                return project
        return None

    def archive_project(self, username: str, project_id: str, *, admin: bool = False) -> dict[str, Any]:
        data = self._load(self.projects_path, {"projects": []})
        rows = data.get("projects", [])
        target: dict[str, Any] | None = None
        for project in rows:
            self._normalize_project(project)
            if project.get("id") == project_id:
                if not admin and project.get("owner") != username:
                    raise ValueError("Project not found")
                target = project
                break
        if not target:
            raise ValueError("Project not found")
        owner = str(target.get("owner") or username)
        if target.get("archived_at"):
            return project_public_view(target)
        active_for_owner = [
            project for project in rows if project.get("owner") == owner and not project.get("archived_at") and project.get("id") != project_id
        ]
        if not active_for_owner:
            raise ValueError("At least one active project is required")
        target["archived_at"] = iso_now()
        target["archived_by"] = username
        target["updated_at"] = iso_now()
        self._save(self.projects_path, data)
        return project_public_view(target, include_archived=True)

    def archive_folder(self, username: str, project_id: str, folder_id: str, *, admin: bool = False) -> dict[str, Any]:
        if not folder_id or folder_id == "root":
            raise ValueError("Root folder cannot be archived")
        data = self._load(self.projects_path, {"projects": []})
        for project in data.get("projects", []):
            self._normalize_project(project)
            if project.get("id") != project_id:
                continue
            if not admin and project.get("owner") != username:
                raise ValueError("Project not found")
            if project.get("archived_at"):
                raise ValueError("Project is archived")
            for folder in project.get("folders", []):
                if folder.get("id") != folder_id:
                    continue
                if folder.get("archived_at"):
                    return dict(folder)
                folder["archived_at"] = iso_now()
                folder["archived_by"] = username
                project["updated_at"] = iso_now()
                self._save(self.projects_path, data)
                return dict(folder)
            raise ValueError("Folder not found")
        raise ValueError("Project not found")

    def validate_project_folder(self, username: str, project_id: str, folder_id: str, *, admin: bool = False) -> None:
        if not project_id:
            return
        project = self.get_project(username, project_id, admin=admin)
        if not project:
            raise ValueError("Project not found")
        folder = folder_id or "root"
        if not any(item.get("id") == folder for item in project.get("folders", [])):
            raise ValueError("Folder not found")

    def list_templates(self, username: str, *, admin: bool = False) -> list[dict[str, Any]]:
        self.ensure_default_template(username)
        data = self._load(self.templates_path, {"templates": []})
        rows = data.get("templates", [])
        if admin:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows if row.get("owner") == username or row.get("shared")]

    def get_template(self, username: str, template_id: str, *, admin: bool = False) -> dict[str, Any] | None:
        for row in self.list_templates(username, admin=admin):
            if row.get("id") == template_id:
                return row
        return None

    def create_template(
        self,
        username: str,
        name: str,
        params: dict[str, Any],
        *,
        description: str = "",
        shared: bool = False,
        admin: bool = False,
    ) -> dict[str, Any]:
        clean = clean_name(name, default="Untitled Template")
        row = {
            "id": f"tpl_{uuid.uuid4().hex[:12]}",
            "owner": username,
            "name": clean,
            "description": description[:500],
            "shared": bool(shared and admin),
            "params": sanitize_template_params(params),
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
        data = self._load(self.templates_path, {"templates": []})
        data.setdefault("templates", []).append(row)
        self._save(self.templates_path, data)
        return row

    def delete_template(self, username: str, template_id: str, *, admin: bool = False) -> dict[str, Any]:
        data = self._load(self.templates_path, {"templates": []})
        kept = []
        deleted: dict[str, Any] | None = None
        for row in data.get("templates", []):
            if row.get("id") == template_id:
                if not admin and row.get("owner") != username:
                    raise ValueError("Template not found")
                deleted = row
                continue
            kept.append(row)
        if not deleted:
            raise ValueError("Template not found")
        data["templates"] = kept
        self._save(self.templates_path, data)
        return deleted

    def ensure_default_template(self, username: str) -> dict[str, Any] | None:
        if not username:
            return None
        data = self._load(self.templates_path, {"templates": []})
        for row in data.get("templates", []):
            if row.get("owner") == username:
                return row
        row = {
            "id": f"tpl_{uuid.uuid4().hex[:12]}",
            "owner": username,
            "name": "Best Quality Mandarin",
            "description": "Default high-quality Chinese lecture localization settings.",
            "shared": False,
            "params": {
                "source_language": "auto",
                "target_subtitle_language": "zh-CN",
                "target_tts_language": "zh-CN",
                "quality_mode": "best_quality",
                "style": "natural_chinese_lecture",
                "tts_speed": 1.0,
                "tts_emotion": "clear_engaged_teaching",
                "tts_end_gap_seconds": 0.2,
                "tts_min_audio_gap_seconds": 0.08,
                "tts_speaker_gender": "auto",
                "mux_keep_original_audio": False,
                "mux_original_audio_volume": 0.08,
                "mux_hard_subtitle": True,
                "mux_soft_subtitle": True,
                "max_subtitle_line_chars": 22,
            },
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
        data.setdefault("templates", []).append(row)
        self._save(self.templates_path, data)
        return row

    def ensure_default_project(self, username: str) -> dict[str, Any] | None:
        if not username:
            return None
        existing = self.list_projects(username)
        if existing:
            return existing[0]
        return self.create_project(username, "Default")

    def user_upload_dir(self, username: str) -> Path:
        upload_root = ensure_dir(self.config.get("webui", {}).get("upload_dir") or Path(self.config["output_dir"]) / "uploads")
        return ensure_dir(upload_root / safe_id(username))

    def quota_status(self, username: str) -> dict[str, Any]:
        user = self.get_user(username) or {}
        local_quota = int(user.get("quota_local_bytes") or gb_to_bytes(500))
        remote_quota = int(user.get("quota_remote_bytes") or gb_to_bytes(10))
        remote_used = self.remote_usage_bytes(username)
        local_used, local_source = self.local_usage_bytes()
        return {
            "user": username,
            "local_used_bytes": local_used,
            "local_quota_bytes": local_quota,
            "local_remaining_bytes": max(0, local_quota - local_used),
            "local_percent": round((local_used / local_quota) * 100, 2) if local_quota else 0,
            "local_usage_source": local_source,
            "remote_used_bytes": remote_used,
            "remote_quota_bytes": remote_quota,
            "remote_remaining_bytes": max(0, remote_quota - remote_used),
            "remote_percent": round((remote_used / remote_quota) * 100, 2) if remote_quota else 0,
        }

    def can_store(self, username: str, incoming_bytes: int) -> bool:
        quota = self.quota_status(username)
        return int(quota["remote_used_bytes"]) + incoming_bytes <= int(quota["remote_quota_bytes"])

    def remote_usage_bytes(self, username: str) -> int:
        total = directory_size(self.user_upload_dir(username))
        webui = self.config.get("webui", {})
        manifest = Path(str(webui.get("preview_manifest") or Path(webui.get("preview_dir") or Path(self.config["output_dir"]) / "previews") / "preview_manifest.json"))
        if manifest.exists():
            try:
                data = read_json(manifest)
                rows = data.get("previews", data) if isinstance(data, dict) else data
            except Exception:
                rows = []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and (not row.get("owner") or str(row.get("owner")) == username):
                        total += file_size(row.get("preview_path") or row.get("path"))
                        total += file_size(row.get("thumbnail_path"))
        return total

    def local_usage_bytes(self) -> tuple[int, str]:
        metrics = self.worker_status().get("metrics", {})
        if isinstance(metrics, dict):
            local_storage = metrics.get("local_storage")
            if isinstance(local_storage, dict):
                managed = local_storage.get("managed_bytes")
                if isinstance(managed, (int, float)) and managed >= 0:
                    return int(managed), "worker_heartbeat"
        webui = self.config.get("webui", {}) if isinstance(self.config.get("webui"), dict) else {}
        if str(webui.get("execution_mode", "local_subprocess")) == "worker_queue":
            return 0, "worker_heartbeat_unavailable"
        total = 0
        for key in ["output_dir", "work_dir"]:
            value = self.config.get(key)
            if value:
                total += directory_size(Path(str(value)))
        return total, "local_filesystem"

    def default_project_quota_gb(self) -> float:
        web = self.config.get("webui", {})
        return float(web.get("default_project_quota_gb", web.get("default_local_quota_gb", 500)) or 500)

    def record_worker_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        previous = self._load(self.worker_path, {})
        capabilities = sanitize_worker_capabilities(payload.get("capabilities"))
        if not capabilities and isinstance(previous, dict):
            capabilities = previous.get("capabilities") if isinstance(previous.get("capabilities"), dict) else {}
        media_refs = sanitize_worker_media_refs(payload.get("media_refs"))
        if not isinstance(payload.get("media_refs"), list) and isinstance(previous, dict):
            media_refs = previous.get("media_refs") if isinstance(previous.get("media_refs"), list) else []
        max_concurrent_jobs = worker_max_concurrent_jobs(payload, previous if isinstance(previous, dict) else {})
        row = {
            "status": str(payload.get("status") or "online"),
            "worker_id": str(payload.get("worker_id") or "local-windows-worker"),
            "version": str(payload.get("version") or ""),
            "max_concurrent_jobs": max_concurrent_jobs,
            "message": sanitize_remote_text(payload.get("message") or ""),
            "metrics": sanitize_metrics(payload.get("metrics")) if isinstance(payload.get("metrics"), dict) else {},
            "media_refs": media_refs,
            "capabilities": capabilities,
            "updated_at": iso_now(),
            "updated_at_epoch": int(time.time()),
        }
        self._save(self.worker_path, row)
        return row

    def worker_status(self, *, offline_after_seconds: int | None = None) -> dict[str, Any]:
        row = self._load(self.worker_path, {})
        if not row:
            return {"status": "local", "message": "No remote worker heartbeat recorded", "updated_at": None}
        if offline_after_seconds is None:
            webui = self.config.get("webui", {}) if isinstance(self.config.get("webui"), dict) else {}
            try:
                offline_after_seconds = int(webui.get("worker_offline_after_seconds", 180) or 180)
            except (TypeError, ValueError):
                offline_after_seconds = 180
        offline_after_seconds = max(30, int(offline_after_seconds))
        age = int(time.time()) - int(row.get("updated_at_epoch", 0) or 0)
        if age > offline_after_seconds:
            row = dict(row)
            row["status"] = "offline"
            row["age_seconds"] = age
        else:
            row["age_seconds"] = age
        return row

    def _load(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return read_json(path)
        except Exception:
            return default

    def _save(self, path: Path, data: Any) -> None:
        ensure_dir(path.parent)
        write_json(path, data)

    def _normalize_project(self, project: dict[str, Any]) -> bool:
        changed = False
        if not isinstance(project.get("folders"), list):
            project["folders"] = []
            changed = True
        if not any(folder.get("id") == "root" for folder in project["folders"]):
            project["folders"].insert(0, {"id": "root", "name": "Root"})
            changed = True
        for folder in project["folders"]:
            if folder.get("id") == "root" and not folder.get("name"):
                folder["name"] = "Root"
                changed = True
        if not project.get("quota_project_bytes"):
            project["quota_project_bytes"] = gb_to_bytes(self.default_project_quota_gb())
            changed = True
        return changed


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_raw, salt_raw, digest_raw = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        salt = base64.urlsafe_b64decode(salt_raw + "=" * (-len(salt_raw) % 4))
        expected = base64.urlsafe_b64decode(digest_raw + "=" * (-len(digest_raw) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in user.items() if k != "password_hash"}
    row.setdefault("role", "user")
    row.setdefault("disabled", False)
    return row


def project_public_view(project: dict[str, Any], *, include_archived: bool = False) -> dict[str, Any]:
    row = dict(project)
    row["folders"] = [
        dict(folder)
        for folder in project.get("folders", [])
        if include_archived or not folder.get("archived_at")
    ]
    return row


def sanitize_worker_media_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value[:1000]:
        if not isinstance(item, dict) or not item.get("ref_id"):
            continue
        rows.append(
            {
                "ref_id": str(item.get("ref_id") or "")[:80],
                "name": safe_worker_media_name(item.get("name") or "worker-media"),
                "size": int(max(0, coerce_float(item.get("size")))),
                "mtime": max(0, coerce_float(item.get("mtime"))),
                "media_type": str(item.get("media_type") or "application/octet-stream")[:120],
            }
        )
    return rows


def safe_worker_media_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "worker-media"
    windows_name = PureWindowsPath(text).name
    posix_name = Path(text).name
    if "\\" in text or re.match(r"^[A-Za-z]:", text):
        candidate = windows_name or posix_name
    else:
        candidate = posix_name or windows_name
    return clean_name(sanitize_remote_text(candidate), default="worker-media")


def sanitize_worker_capabilities(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned = sanitize_remote_value(value)
    return cleaned if isinstance(cleaned, dict) else {}


def validate_username(username: str) -> None:
    if not USERNAME_RE.fullmatch(username or ""):
        raise ValueError("Username must be 2-64 characters using letters, numbers, dot, dash, or underscore")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._-")
    return cleaned[:64] or uuid.uuid4().hex[:12]


def clean_name(value: str, *, default: str) -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", value or "").strip()
    return re.sub(r"\s+", " ", text)[:120] or default


def sanitize_template_params(params: dict[str, Any]) -> dict[str, Any]:
    raw = params if isinstance(params, dict) else {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in TEMPLATE_PARAM_KEYS:
            continue
        if key in {"tts_speed", "tts_end_gap_seconds", "tts_min_audio_gap_seconds", "mux_original_audio_volume"}:
            out[key] = coerce_float(value)
        elif key in {"mux_keep_original_audio", "mux_hard_subtitle", "mux_soft_subtitle"}:
            out[key] = coerce_bool(value)
        elif key == "max_subtitle_line_chars":
            out[key] = int(max(12, min(42, coerce_float(value))))
        else:
            out[key] = str(value or "").strip()
    return out


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def worker_max_concurrent_jobs(payload: dict[str, Any], previous: dict[str, Any]) -> int:
    raw = payload.get("max_concurrent_jobs")
    if raw is None and isinstance(payload.get("worker"), dict):
        raw = payload["worker"].get("max_concurrent_jobs")
    if raw is None:
        raw = previous.get("max_concurrent_jobs")
    return max(1, min(8, coerce_int(raw, 1)))


def gb_to_bytes(value: float) -> int:
    return int(max(0, value) * 1024 * 1024 * 1024)


def directory_size(path: str | Path) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for item in root.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def file_size(path: Any) -> int:
    if not path:
        return 0
    try:
        target = Path(str(path))
        return target.stat().st_size if target.exists() and target.is_file() else 0
    except OSError:
        return 0


def iso_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
