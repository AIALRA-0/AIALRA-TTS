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
                "previewable": path.suffix.lower() in {".mp4", ".wav", ".mp3", ".m4a"},
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            }
    return sorted(records.values(), key=lambda row: float(row.get("mtime") or 0), reverse=True)


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
    roots = [Path(config["work_dir"]).resolve()]
    deleted: list[dict[str, Any]] = []
    candidates: list[Path] = []
    for root in roots:
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    candidates.append(path)
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
