from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

from .utils import read_json


DOWNLOADABLE_OUTPUTS = {
    "en_srt",
    "en_vtt",
    "zh_srt",
    "zh_vtt",
    "bilingual_srt",
    "bilingual_ass",
    "zh_dub_wav",
    "zh_dub_mp4",
    "zh_dub_bilingual_hardsub_mp4",
}
PREVIEWABLE_SUFFIXES = {".mp4", ".webm", ".wav", ".mp3", ".m4a"}


def artifact_id(path: str | Path) -> str:
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8", errors="ignore")).hexdigest()[:24]


def artifact_catalog(config: dict[str, Any], jobs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    output_dir = Path(config["output_dir"]).resolve()
    records: dict[str, dict[str, Any]] = {}
    reports = sorted(output_dir.glob("*_report.json"), key=lambda p: p.stat().st_mtime, reverse=True) if output_dir.exists() else []
    job_by_report = {}
    for job in jobs or []:
        report = str(job.get("result_report") or "")
        if report:
            job_by_report[str(Path(report).resolve()).lower()] = job

    for report_path in reports:
        try:
            report = read_json(report_path)
        except Exception:
            continue
        job = job_by_report.get(str(report_path.resolve()).lower())
        owner = job.get("user") if job else report.get("user")
        project_id = (job.get("metadata") or {}).get("project_id") if job else report.get("project_id")
        group_id = artifact_id(report_path)
        bundle_paths = [report_path, report_path.with_suffix(".md")]
        for key, value in (report.get("outputs") or {}).items():
            if key in DOWNLOADABLE_OUTPUTS and value:
                bundle_paths.append(Path(value))
        bundle_size = sum(path_size(p) for p in bundle_paths if p.exists())
        records[group_id] = {
            "id": group_id,
            "kind": "report_bundle",
            "name": report.get("name") or report_path.stem,
            "path": str(report_path),
            "size": bundle_size,
            "mtime": report_path.stat().st_mtime,
            "owner": owner,
            "project_id": project_id,
            "job_id": job.get("id") if job else None,
            "report": str(report_path),
            "outputs": len([p for p in bundle_paths if p.exists()]),
            "previewable": False,
            "display_path": str(report_path),
        }
        for key, value in (report.get("outputs") or {}).items():
            if key not in DOWNLOADABLE_OUTPUTS or not value:
                continue
            path = Path(value)
            if not path.exists():
                continue
            aid = artifact_id(path)
            records[aid] = {
                "id": aid,
                "kind": key,
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "mtime": path.stat().st_mtime,
                "owner": owner,
                "project_id": project_id,
                "job_id": job.get("id") if job else None,
                "report": str(report_path),
                "previewable": path.suffix.lower() in PREVIEWABLE_SUFFIXES,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "display_path": str(path),
            }
        for key, preview in (report.get("previews") or {}).items():
            add_preview_record(
                records,
                preview,
                config=config,
                owner=owner,
                project_id=project_id,
                job_id=job.get("id") if job else None,
                report=str(report_path),
                source_output_key=str(key),
            )
    for preview in load_preview_manifest(config):
        add_preview_record(records, preview, config=config)
    return sorted(records.values(), key=lambda row: float(row.get("mtime") or 0), reverse=True)


def add_preview_record(
    records: dict[str, dict[str, Any]],
    preview: str | dict[str, Any],
    *,
    config: dict[str, Any],
    owner: str | None = None,
    project_id: str | None = None,
    job_id: str | None = None,
    report: str | None = None,
    source_output_key: str | None = None,
) -> None:
    row = {"preview_path": preview} if isinstance(preview, str) else dict(preview or {})
    preview_path = Path(str(row.get("preview_path") or row.get("path") or ""))
    if not preview_path.exists() or not preview_path.is_file():
        return
    thumbnail_path = Path(str(row.get("thumbnail_path") or "")) if row.get("thumbnail_path") else None
    thumbnail_ok = bool(thumbnail_path and thumbnail_path.exists() and thumbnail_path.is_file())
    aid = str(row.get("id") or artifact_id(preview_path))
    name = str(row.get("name") or row.get("origin_name") or preview_path.name)
    records[aid] = {
        "id": aid,
        "kind": str(row.get("kind") or "remote_preview"),
        "name": name,
        "path": str(preview_path),
        "display_path": str(row.get("display_path") or f"preview cache: {preview_path.name}"),
        "size": preview_path.stat().st_size,
        "mtime": preview_path.stat().st_mtime,
        "owner": row.get("owner", owner),
        "project_id": row.get("project_id", project_id),
        "job_id": row.get("job_id", job_id),
        "report": row.get("report", report),
        "source_output_key": row.get("source_output_key", source_output_key),
        "previewable": preview_path.suffix.lower() in PREVIEWABLE_SUFFIXES,
        "media_type": row.get("media_type") or mimetypes.guess_type(preview_path.name)[0] or "application/octet-stream",
        "remote_preview": True,
        "full_available": bool(row.get("full_available", False)),
    }
    if thumbnail_ok and thumbnail_path:
        records[aid]["thumbnail_path"] = str(thumbnail_path)
        records[aid]["thumbnail_media_type"] = mimetypes.guess_type(thumbnail_path.name)[0] or "image/jpeg"


def load_preview_manifest(config: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_path = preview_manifest_path(config)
    if not manifest_path.exists():
        return []
    try:
        data = read_json(manifest_path)
    except Exception:
        return []
    rows = data.get("previews", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def preview_manifest_path(config: dict[str, Any]) -> Path:
    webui = config.get("webui", {})
    if webui.get("preview_manifest"):
        return Path(str(webui["preview_manifest"])).resolve()
    return preview_cache_dir(config) / "preview_manifest.json"


def preview_cache_dir(config: dict[str, Any]) -> Path:
    webui = config.get("webui", {})
    if webui.get("preview_dir"):
        return Path(str(webui["preview_dir"])).resolve()
    return Path(config["output_dir"]).resolve() / "previews"


def filter_artifacts_for_user(artifacts: list[dict[str, Any]], username: str, *, admin: bool) -> list[dict[str, Any]]:
    if admin:
        return artifacts
    return [row for row in artifacts if not row.get("owner") or row.get("owner") == username]


def find_artifact(artifacts: list[dict[str, Any]], aid: str) -> dict[str, Any] | None:
    return next((row for row in artifacts if row.get("id") == aid), None)


def sign_artifact_token(secret: str, artifact_id_value: str, username: str, *, ttl_seconds: int = 900) -> str:
    payload = {
        "a": artifact_id_value,
        "u": username,
        "exp": int(time.time()) + max(60, ttl_seconds),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def verify_artifact_token(secret: str, token: str, artifact_id_value: str, username: str) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except Exception:
        return False
    if not hmac.compare_digest(actual, expected):
        return False
    if int(payload.get("exp", 0) or 0) < int(time.time()):
        return False
    return payload.get("a") == artifact_id_value and payload.get("u") == username


def safe_delete_artifact(path: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    target = Path(path).resolve()
    allowed_roots = [
        Path(config["output_dir"]).resolve(),
        Path(config["work_dir"]).resolve(),
    ]
    webui = config.get("webui", {})
    if webui.get("upload_dir"):
        allowed_roots.append(Path(webui["upload_dir"]).resolve())
    if webui.get("preview_dir"):
        allowed_roots.append(Path(webui["preview_dir"]).resolve())
    if not target.exists():
        return {"deleted": False, "path": str(target), "reason": "missing"}
    if not any(is_relative_to(target, root) for root in allowed_roots):
        raise ValueError(f"Refusing to delete outside managed roots: {target}")
    if target.is_dir():
        raise ValueError("Directory deletion is not allowed through artifact API")
    size = target.stat().st_size
    target.unlink()
    return {"deleted": True, "path": str(target), "bytes": size}


def safe_delete_artifact_record(row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if row.get("remote_preview"):
        paths = [Path(str(row["path"]))]
        if row.get("thumbnail_path"):
            paths.append(Path(str(row["thumbnail_path"])))
        deleted = []
        for path in paths:
            try:
                deleted.append(safe_delete_artifact(path, config))
            except ValueError as exc:
                deleted.append({"deleted": False, "path": str(path), "error": str(exc)})
        return {"items": deleted, "bytes": sum(int(item.get("bytes", 0) or 0) for item in deleted)}
    if row.get("kind") != "report_bundle":
        return {"items": [safe_delete_artifact(row["path"], config)]}
    report_path = Path(str(row.get("report") or row.get("path")))
    paths = [report_path, report_path.with_suffix(".md")]
    if report_path.exists():
        try:
            report = read_json(report_path)
            for key, value in (report.get("outputs") or {}).items():
                if key in DOWNLOADABLE_OUTPUTS and value:
                    paths.append(Path(value))
        except Exception:
            pass
    seen: set[str] = set()
    deleted = []
    for path in paths:
        key = str(Path(path).resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            deleted.append(safe_delete_artifact(path, config))
        except ValueError as exc:
            deleted.append({"deleted": False, "path": str(path), "error": str(exc)})
    return {"items": deleted, "bytes": sum(int(item.get("bytes", 0) or 0) for item in deleted)}


def cleanup_expired_files(config: dict[str, Any], *, older_than_days: int = 7, dry_run: bool = True) -> dict[str, Any]:
    cutoff = time.time() - max(1, older_than_days) * 86400
    roots = [Path(config["work_dir"]).resolve(), preview_cache_dir(config)]
    deleted: list[dict[str, Any]] = []
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if root.exists():
            for path in root.rglob("*"):
                key = str(path.resolve()).lower()
                if key not in seen and path.is_file() and path.stat().st_mtime < cutoff:
                    candidates.append(path)
                    seen.add(key)
    for path in candidates:
        row = {"path": str(path), "bytes": path.stat().st_size}
        if not dry_run:
            path.unlink(missing_ok=True)
            row["deleted"] = True
        else:
            row["deleted"] = False
        deleted.append(row)
    return {"dry_run": dry_run, "older_than_days": older_than_days, "count": len(deleted), "bytes": sum(int(x["bytes"]) for x in deleted), "items": deleted[:500]}


def with_signed_urls(
    artifacts: list[dict[str, Any]],
    *,
    secret: str,
    username: str,
    ttl_seconds: int,
) -> list[dict[str, Any]]:
    rows = []
    for row in artifacts:
        item = dict(row)
        if item.get("kind") != "report_bundle":
            token = sign_artifact_token(secret, str(item["id"]), username, ttl_seconds=ttl_seconds)
            item["download_url"] = f"/api/artifacts/{item['id']}/download?token={token}&download=1"
            if item.get("previewable"):
                item["preview_url"] = f"/api/artifacts/{item['id']}/download?token={token}"
            if item.get("thumbnail_path"):
                item["thumbnail_url"] = f"/api/artifacts/{item['id']}/download?token={token}&variant=thumbnail"
        rows.append(item)
    return rows


def path_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
