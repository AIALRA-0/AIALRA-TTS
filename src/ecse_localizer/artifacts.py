from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

from .utils import read_json, write_json


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


def artifact_catalog(config: dict[str, Any], jobs: list[dict[str, Any]] | None = None, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    output_dir = Path(config["output_dir"]).resolve()
    records: dict[str, dict[str, Any]] = {}
    reports = sorted(output_dir.glob("*_report.json"), key=lambda p: p.stat().st_mtime, reverse=True) if output_dir.exists() else []
    job_by_report = {}
    job_rows = jobs or []
    deleted_job_ids = {str(job.get("id")) for job in job_rows if job_is_deleted(job) and job.get("id")}
    deleted_report_keys: set[str] = set()
    for job in job_rows:
        report = str(job.get("result_report") or "")
        if report:
            report_key = str(Path(report).resolve()).lower()
            if job_is_deleted(job):
                deleted_report_keys.add(report_key)
                if include_deleted:
                    job_by_report[report_key] = job
            else:
                job_by_report[report_key] = job

    for report_path in reports:
        try:
            report = read_json(report_path)
        except Exception:
            continue
        report_key = str(report_path.resolve()).lower()
        if not include_deleted and (report_key in deleted_report_keys or row_references_deleted_job(report, deleted_job_ids)):
            continue
        job = job_by_report.get(report_key)
        source_deleted = bool(job and job_is_deleted(job)) or report_key in deleted_report_keys or row_references_deleted_job(report, deleted_job_ids)
        metadata = job.get("metadata") if job and isinstance(job.get("metadata"), dict) else {}
        owner = job.get("user") if job else report.get("user")
        project_id = metadata.get("project_id") if job else report.get("project_id")
        folder_id = metadata.get("folder_id", "root") if job else report.get("folder_id", "root")
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
            "folder_id": folder_id,
            "job_id": job.get("id") if job else None,
            "report": str(report_path),
            "outputs": len([p for p in bundle_paths if p.exists()]),
            "previewable": False,
            "display_path": str(report_path),
            "source_deleted": source_deleted,
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
                "folder_id": folder_id,
                "job_id": job.get("id") if job else None,
                "report": str(report_path),
                "previewable": path.suffix.lower() in PREVIEWABLE_SUFFIXES,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "display_path": str(path),
                "source_deleted": source_deleted,
            }
        for key, preview in (report.get("previews") or {}).items():
            add_preview_record(
                records,
                preview,
                config=config,
                owner=owner,
                project_id=project_id,
                folder_id=folder_id,
                job_id=job.get("id") if job else None,
                report=str(report_path),
                source_output_key=str(key),
                source_deleted=source_deleted,
            )
    for preview in load_preview_manifest(config):
        source_deleted = row_references_deleted_job(preview, deleted_job_ids)
        if not include_deleted and source_deleted:
            continue
        add_preview_record(records, preview, config=config, source_deleted=source_deleted)
    add_worker_artifact_records(records, job_rows, include_deleted=include_deleted)
    return sorted(records.values(), key=lambda row: float(row.get("mtime") or 0), reverse=True)


def job_is_deleted(job: dict[str, Any]) -> bool:
    return str(job.get("status") or "").strip().lower() == "deleted"


def row_references_deleted_job(row: dict[str, Any], deleted_job_ids: set[str]) -> bool:
    if not deleted_job_ids:
        return False
    for key in ["job_id", "source_job_id", "cache_job_id"]:
        if str(row.get(key) or "") in deleted_job_ids:
            return True
    return False


def add_worker_artifact_records(records: dict[str, dict[str, Any]], jobs: list[dict[str, Any]], *, include_deleted: bool = False) -> None:
    for job in jobs:
        source_deleted = job_is_deleted(job)
        if source_deleted and not include_deleted:
            continue
        artifacts = job.get("worker_artifacts")
        if not isinstance(artifacts, list):
            continue
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        for raw in artifacts:
            if not isinstance(raw, dict) or not raw.get("ref_id"):
                continue
            ref_id = safe_ref_id(str(raw["ref_id"]))
            aid = f"worker_artifact_{ref_id}"
            if aid in records and records[aid].get("path"):
                continue
            records.setdefault(
                aid,
                {
                    "id": aid,
                    "kind": str(raw.get("source_output_key") or "worker_full_artifact"),
                    "name": str(raw.get("name") or f"{ref_id}.bin"),
                    "size": int(raw.get("size", 0) or 0),
                    "mtime": float(raw.get("mtime", 0) or 0),
                    "owner": job.get("user"),
                    "project_id": metadata.get("project_id"),
                    "folder_id": metadata.get("folder_id", "root"),
                    "job_id": job.get("id"),
                    "source_job_id": job.get("id"),
                    "source_output_key": raw.get("source_output_key"),
                    "media_type": raw.get("media_type") or mimetypes.guess_type(str(raw.get("name") or ""))[0] or "application/octet-stream",
                    "previewable": False,
                    "remote_worker_artifact": True,
                    "download_requestable": True,
                    "artifact_ref_id": ref_id,
                    "full_available": True,
                    "display_path": f"Windows worker: {raw.get('name') or ref_id}",
                    "source_deleted": source_deleted,
                },
            )


def add_preview_record(
    records: dict[str, dict[str, Any]],
    preview: str | dict[str, Any],
    *,
    config: dict[str, Any],
    owner: str | None = None,
    project_id: str | None = None,
    folder_id: str | None = None,
    job_id: str | None = None,
    report: str | None = None,
    source_output_key: str | None = None,
    source_deleted: bool = False,
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
        "folder_id": row.get("folder_id", folder_id),
        "job_id": row.get("job_id", job_id),
        "report": row.get("report", report),
        "source_output_key": row.get("source_output_key", source_output_key),
        "previewable": preview_path.suffix.lower() in PREVIEWABLE_SUFFIXES,
        "media_type": row.get("media_type") or mimetypes.guess_type(preview_path.name)[0] or "application/octet-stream",
        "remote_preview": True,
        "remote_cache": bool(row.get("remote_cache", False)),
        "full_available": bool(row.get("full_available", False)),
        "source_deleted": bool(row.get("source_deleted", source_deleted)),
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
    return [row for row in artifacts if row.get("owner") == username]


def filter_artifact_records(
    artifacts: list[dict[str, Any]],
    *,
    project_id: str = "",
    folder_id: str = "",
    job_id: str = "",
    kind: str = "",
) -> list[dict[str, Any]]:
    project = str(project_id or "").strip()
    folder = str(folder_id or "").strip()
    job = str(job_id or "").strip()
    artifact_kind = str(kind or "").strip()
    out: list[dict[str, Any]] = []
    for row in artifacts:
        if project and str(row.get("project_id") or "") != project:
            continue
        if folder and folder != "all" and str(row.get("folder_id") or "root") != folder:
            continue
        if job and str(row.get("job_id") or row.get("source_job_id") or "") != job:
            continue
        if artifact_kind and str(row.get("kind") or "") != artifact_kind:
            continue
        out.append(row)
    return out


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
    target = validate_managed_file(path, config)
    if not target.exists():
        return {"deleted": False, "path": str(target), "reason": "missing"}
    size = target.stat().st_size
    target.unlink()
    return {"deleted": True, "path": str(target), "bytes": size}


def managed_roots(config: dict[str, Any]) -> list[Path]:
    roots = [
        Path(config["output_dir"]).resolve(),
        Path(config["work_dir"]).resolve(),
    ]
    webui = config.get("webui", {}) if isinstance(config.get("webui"), dict) else {}
    for key in ["upload_dir", "preview_dir", "job_dir"]:
        if webui.get(key):
            roots.append(Path(str(webui[key])).resolve())
    unique: dict[str, Path] = {}
    for root in roots:
        unique[str(root).lower()] = root
    return list(unique.values())


def validate_managed_file(path: str | Path, config: dict[str, Any]) -> Path:
    target = Path(path).resolve()
    allowed_roots = managed_roots(config)
    if not target.exists():
        if not any(is_relative_to(target, root) for root in allowed_roots):
            raise ValueError(f"Refusing to delete outside managed roots: {target}")
        return target
    if not any(is_relative_to(target, root) for root in allowed_roots):
        raise ValueError(f"Refusing to delete outside managed roots: {target}")
    if target.is_dir():
        raise ValueError("Directory deletion is not allowed through artifact API")
    return target


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
        manifest = prune_preview_manifest_record(config, row)
        return {"items": deleted, "bytes": sum(int(item.get("bytes", 0) or 0) for item in deleted), "manifest": manifest}
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


def prune_preview_manifest_record(config: dict[str, Any], deleted_row: dict[str, Any]) -> dict[str, Any]:
    manifest_path = preview_manifest_path(config)
    if not manifest_path.exists():
        return {"removed": 0, "manifest": str(manifest_path)}
    try:
        data = read_json(manifest_path)
    except Exception as exc:
        return {"removed": 0, "manifest": str(manifest_path), "error": str(exc)}
    raw_rows = data.get("previews", data) if isinstance(data, dict) else data
    if not isinstance(raw_rows, list):
        return {"removed": 0, "manifest": str(manifest_path)}

    target_ids, target_paths = preview_manifest_match_keys(deleted_row)
    kept: list[dict[str, Any]] = []
    removed = 0
    for raw in raw_rows:
        if isinstance(raw, dict) and preview_manifest_row_matches(raw, target_ids=target_ids, target_paths=target_paths):
            removed += 1
            continue
        if isinstance(raw, dict):
            kept.append(raw)
    if removed:
        if isinstance(data, dict):
            updated = dict(data)
            updated["previews"] = kept
            write_json(manifest_path, updated)
        else:
            write_json(manifest_path, kept)
    return {"removed": removed, "manifest": str(manifest_path)}


def preview_manifest_match_keys(row: dict[str, Any]) -> tuple[set[str], set[str]]:
    ids = {str(row.get("id") or "").strip()} - {""}
    paths: set[str] = set()
    for key in ["path", "preview_path", "thumbnail_path"]:
        value = row.get(key)
        if value:
            paths.add(normalized_manifest_path(value))
    return ids, paths


def preview_manifest_row_matches(row: dict[str, Any], *, target_ids: set[str], target_paths: set[str]) -> bool:
    row_id = str(row.get("id") or "").strip()
    if row_id and row_id in target_ids:
        return True
    _, row_paths = preview_manifest_match_keys(row)
    return bool(row_paths & target_paths)


def normalized_manifest_path(value: Any) -> str:
    try:
        return str(Path(str(value)).resolve()).lower()
    except OSError:
        return str(value).strip().lower()


def cleanup_expired_files(config: dict[str, Any], *, older_than_days: int = 7, dry_run: bool = True) -> dict[str, Any]:
    cutoff = time.time() - max(1, older_than_days) * 86400
    roots = [Path(config["work_dir"]).resolve(), preview_cache_dir(config)]
    deleted: list[dict[str, Any]] = []
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if root.exists():
            for path in root.rglob("*"):
                if path.resolve() == preview_manifest_path(config):
                    continue
                if is_protected_cleanup_metadata(path, config):
                    continue
                key = str(path.resolve()).lower()
                if key not in seen and path.is_file() and path.stat().st_mtime < cutoff:
                    candidates.append(path)
                    seen.add(key)
    for path in candidates:
        deleted.append(cleanup_delete_file(config, path, dry_run=dry_run, reason="expired_file"))
    deleted.extend(cleanup_deleted_job_artifacts(config, cutoff=cutoff, dry_run=dry_run, seen=seen))
    deleted.extend(cleanup_preview_manifest(config, cutoff=cutoff, dry_run=dry_run, seen=seen))
    return {"dry_run": dry_run, "older_than_days": older_than_days, "count": len(deleted), "bytes": sum(int(x["bytes"]) for x in deleted), "items": deleted[:500]}


def cleanup_deleted_job_artifacts(config: dict[str, Any], *, cutoff: float, dry_run: bool, seen: set[str]) -> list[dict[str, Any]]:
    job_dir = webui_job_dir(config)
    if not job_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for job_path in sorted(job_dir.glob("*.json")):
        try:
            job = read_json(job_path)
        except Exception:
            continue
        if not isinstance(job, dict) or str(job.get("status") or "") != "deleted":
            continue
        deleted_at = parse_epoch(job.get("deleted_at")) or job_path.stat().st_mtime
        if deleted_at >= cutoff:
            continue
        report = job.get("result_report") or job.get("report")
        if report:
            for path in report_bundle_paths(report):
                row = cleanup_delete_file(config, path, dry_run=dry_run, reason="deleted_job_bundle")
                rows.append(row)
                seen.add(str(path.resolve()).lower())
        for key in ["result_video", "result_audio", "log"]:
            value = job.get(key)
            if not value:
                continue
            row = cleanup_delete_file(config, Path(str(value)), dry_run=dry_run, reason=f"deleted_job_{key}")
            rows.append(row)
            seen.add(str(Path(str(value)).resolve()).lower())
    return rows


def cleanup_preview_manifest(config: dict[str, Any], *, cutoff: float, dry_run: bool, seen: set[str]) -> list[dict[str, Any]]:
    manifest_path = preview_manifest_path(config)
    if not manifest_path.exists():
        return []
    try:
        data = read_json(manifest_path)
    except Exception:
        return []
    raw_rows = data.get("previews", data) if isinstance(data, dict) else data
    if not isinstance(raw_rows, list):
        return []
    rows = [row for row in raw_rows if isinstance(row, dict)]
    deleted_job_ids = deleted_job_ids_older_than(config, cutoff=cutoff)
    kept: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    changed = False
    for row in rows:
        paths = preview_row_paths(row)
        row_mtime = max([path.stat().st_mtime for path in paths if path.exists()] or [parse_epoch(row.get("updated_at")) or 0])
        linked_deleted_job = any(str(row.get(key) or "") in deleted_job_ids for key in ["job_id", "cache_job_id", "source_job_id"])
        missing_all_files = bool(paths) and not any(path.exists() for path in paths)
        expired = row_mtime and row_mtime < cutoff
        if linked_deleted_job or missing_all_files or expired:
            reason = "deleted_job_preview" if linked_deleted_job else "missing_preview_manifest_files" if missing_all_files else "expired_preview_manifest"
            for path in paths:
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                actions.append(cleanup_delete_file(config, path, dry_run=dry_run, reason=reason))
                seen.add(key)
            changed = True
        else:
            kept.append(row)
    if changed and not dry_run:
        write_json(manifest_path, {"previews": kept})
    return actions


def deleted_job_ids_older_than(config: dict[str, Any], *, cutoff: float) -> set[str]:
    job_dir = webui_job_dir(config)
    if not job_dir.exists():
        return set()
    ids: set[str] = set()
    for job_path in job_dir.glob("*.json"):
        try:
            job = read_json(job_path)
        except Exception:
            continue
        if not isinstance(job, dict) or str(job.get("status") or "") != "deleted":
            continue
        deleted_at = parse_epoch(job.get("deleted_at")) or job_path.stat().st_mtime
        if deleted_at < cutoff and job.get("id"):
            ids.add(str(job["id"]))
    return ids


def cleanup_delete_file(config: dict[str, Any], path: Path, *, dry_run: bool, reason: str) -> dict[str, Any]:
    try:
        target = validate_managed_file(path, config)
    except ValueError as exc:
        return {"path": str(Path(path)), "bytes": 0, "deleted": False, "reason": reason, "error": str(exc)}
    size = path_size(target)
    row = {"path": str(target), "bytes": size, "deleted": False, "reason": reason}
    if not target.exists():
        row["reason"] = "missing"
        return row
    if not dry_run:
        target.unlink(missing_ok=True)
        row["deleted"] = True
    return row


def is_protected_cleanup_metadata(path: Path, config: dict[str, Any]) -> bool:
    target = path.resolve()
    webui = config.get("webui", {}) if isinstance(config.get("webui"), dict) else {}
    protected_roots = []
    for key in ["platform_dir", "job_dir"]:
        if webui.get(key):
            protected_roots.append(Path(str(webui[key])).resolve())
    for root in protected_roots:
        if is_relative_to(target, root):
            return True
    return False


def report_bundle_paths(report: str | Path) -> list[Path]:
    report_path = Path(str(report))
    paths = [report_path, report_path.with_suffix(".md")]
    if report_path.exists():
        try:
            report_data = read_json(report_path)
            if isinstance(report_data, dict):
                for key, value in (report_data.get("outputs") or {}).items():
                    if key in DOWNLOADABLE_OUTPUTS and value:
                        paths.append(Path(str(value)))
        except Exception:
            pass
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def preview_row_paths(row: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in ["preview_path", "path", "thumbnail_path"]:
        value = row.get(key)
        if value:
            path = Path(str(value))
            if path not in paths:
                paths.append(path)
    return paths


def webui_job_dir(config: dict[str, Any]) -> Path:
    webui = config.get("webui", {}) if isinstance(config.get("webui"), dict) else {}
    if webui.get("job_dir"):
        return Path(str(webui["job_dir"])).resolve()
    return Path(config["work_dir"]).resolve() / "webui_jobs"


def parse_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except ValueError:
            continue
    return 0.0


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
            if item.get("source_deleted"):
                item["download_disabled_reason"] = "source_job_deleted"
            elif item.get("download_requestable") and not item.get("path"):
                item["request_cache_url"] = f"/api/artifacts/{item['id']}/request-cache"
            else:
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


def safe_ref_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("._-")
    return cleaned[:120] or hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:24]
