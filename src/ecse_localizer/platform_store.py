from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .utils import PROJECT_ROOT, ensure_dir, read_json, write_json


PBKDF2_ROUNDS = 210_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")


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
            self.ensure_default_project(str(user.get("username", "")))

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
        return public_user(row)

    def list_projects(self, username: str, *, admin: bool = False) -> list[dict[str, Any]]:
        data = self._load(self.projects_path, {"projects": []})
        rows = data.get("projects", [])
        changed = False
        for row in rows:
            changed = self._normalize_project(row) or changed
        if changed:
            self._save(self.projects_path, data)
        if admin:
            return [dict(row) for row in rows]
        return [dict(p) for p in rows if p.get("owner") == username]

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
            folders = project.setdefault("folders", [{"id": "root", "name": "Root"}])
            if not any(folder.get("id") == parent_id for folder in folders):
                raise ValueError(f"Parent folder not found: {parent_id}")
            if any(folder.get("parent_id", "root") == parent_id and str(folder.get("name", "")).lower() == clean.lower() for folder in folders):
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

    def validate_project_folder(self, username: str, project_id: str, folder_id: str, *, admin: bool = False) -> None:
        if not project_id:
            return
        project = self.get_project(username, project_id, admin=admin)
        if not project:
            raise ValueError("Project not found")
        folder = folder_id or "root"
        if not any(item.get("id") == folder for item in project.get("folders", [])):
            raise ValueError("Folder not found")

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
        quota = int(user.get("quota_local_bytes") or gb_to_bytes(500))
        used = directory_size(self.user_upload_dir(username))
        return {
            "user": username,
            "local_used_bytes": used,
            "local_quota_bytes": quota,
            "local_remaining_bytes": max(0, quota - used),
            "local_percent": round((used / quota) * 100, 2) if quota else 0,
            "remote_used_bytes": 0,
            "remote_quota_bytes": int(user.get("quota_remote_bytes") or gb_to_bytes(10)),
        }

    def can_store(self, username: str, incoming_bytes: int) -> bool:
        quota = self.quota_status(username)
        return int(quota["local_used_bytes"]) + incoming_bytes <= int(quota["local_quota_bytes"])

    def default_project_quota_gb(self) -> float:
        web = self.config.get("webui", {})
        return float(web.get("default_project_quota_gb", web.get("default_local_quota_gb", 500)) or 500)

    def record_worker_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "status": str(payload.get("status") or "online"),
            "worker_id": str(payload.get("worker_id") or "local-windows-worker"),
            "version": str(payload.get("version") or ""),
            "message": str(payload.get("message") or ""),
            "metrics": payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
            "updated_at": iso_now(),
            "updated_at_epoch": int(time.time()),
        }
        self._save(self.worker_path, row)
        return row

    def worker_status(self, *, offline_after_seconds: int = 45) -> dict[str, Any]:
        row = self._load(self.worker_path, {})
        if not row:
            return {"status": "local", "message": "No remote worker heartbeat recorded", "updated_at": None}
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


def validate_username(username: str) -> None:
    if not USERNAME_RE.fullmatch(username or ""):
        raise ValueError("Username must be 2-64 characters using letters, numbers, dot, dash, or underscore")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._-")
    return cleaned[:64] or uuid.uuid4().hex[:12]


def clean_name(value: str, *, default: str) -> str:
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", value or "").strip()
    return re.sub(r"\s+", " ", text)[:120] or default


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


def iso_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
