import json
from pathlib import Path

import pytest

from ecse_localizer.artifacts import (
    artifact_catalog,
    artifact_id,
    cleanup_expired_files,
    safe_delete_artifact,
    safe_delete_artifact_record,
    sign_artifact_token,
    verify_artifact_token,
    with_signed_urls,
)


def make_config(tmp_path: Path) -> dict:
    out = tmp_path / "out"
    work = tmp_path / "runs"
    upload = tmp_path / "uploads"
    out.mkdir()
    work.mkdir()
    upload.mkdir()
    return {
        "output_dir": str(out),
        "work_dir": str(work),
        "webui": {"upload_dir": str(upload), "preview_dir": str(out / "previews")},
    }


def test_artifact_catalog_and_signed_token(tmp_path):
    config = make_config(tmp_path)
    out = Path(config["output_dir"])
    video = out / "demo_zh_dub.mp4"
    video.write_bytes(b"mp4")
    report = out / "demo_report.json"
    report.write_text(
        json.dumps({"name": "demo", "outputs": {"zh_dub_mp4": str(video)}}),
        encoding="utf-8",
    )

    rows = artifact_catalog(config)
    ids = {row["id"] for row in rows}
    assert artifact_id(video) in ids
    assert artifact_id(report) in ids

    token = sign_artifact_token("secret", artifact_id(video), "admin", ttl_seconds=60)
    assert verify_artifact_token("secret", token, artifact_id(video), "admin")
    assert not verify_artifact_token("secret", token, artifact_id(report), "admin")


def test_preview_manifest_catalog_uses_preview_cache_without_source_path(tmp_path):
    config = make_config(tmp_path)
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    preview = preview_dir / "demo_preview.mp4"
    thumbnail = preview_dir / "demo_thumb.jpg"
    preview.write_bytes(b"small mp4")
    thumbnail.write_bytes(b"jpg")
    manifest = preview_dir / "preview_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "name": "demo_zh_dub.mp4",
                        "preview_path": str(preview),
                        "thumbnail_path": str(thumbnail),
                        "source_path": r"C:\private\full\demo_zh_dub.mp4",
                        "owner": "student",
                        "project_id": "course",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    row = next(item for item in artifact_catalog(config) if item["remote_preview"])
    assert row["name"] == "demo_zh_dub.mp4"
    assert row["path"] == str(preview)
    assert row["display_path"] == "preview cache: demo_preview.mp4"
    assert "source_path" not in row
    assert "private" not in json.dumps(row)

    signed = with_signed_urls([row], secret="secret", username="student", ttl_seconds=60)[0]
    assert "preview_url" in signed
    assert "variant=thumbnail" in signed["thumbnail_url"]


def test_worker_artifact_refs_are_requestable_without_worker_paths(tmp_path):
    config = make_config(tmp_path)
    jobs = [
        {
            "id": "job-1",
            "user": "student",
            "metadata": {"project_id": "course", "folder_id": "week_1"},
            "worker_artifacts": [
                {
                    "ref_id": "abc123",
                    "source_output_key": "zh_dub_mp4",
                    "name": "lecture_zh_dub.mp4",
                    "size": 1234,
                    "mtime": 42,
                    "media_type": "video/mp4",
                }
            ],
        }
    ]

    row = next(item for item in artifact_catalog(config, jobs) if item.get("remote_worker_artifact"))
    assert row["id"] == "worker_artifact_abc123"
    assert row["artifact_ref_id"] == "abc123"
    assert row["display_path"] == "Windows worker: lecture_zh_dub.mp4"
    assert "path" not in row
    assert "worker-local" not in json.dumps(row)

    signed = with_signed_urls([row], secret="secret", username="student", ttl_seconds=60)[0]
    assert signed["request_cache_url"] == "/api/artifacts/worker_artifact_abc123/request-cache"
    assert "download_url" not in signed


def test_remote_cache_manifest_overrides_worker_ref_for_download(tmp_path):
    config = make_config(tmp_path)
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    cached = preview_dir / "lecture_zh_dub.mp4"
    cached.write_bytes(b"full cache")
    (preview_dir / "preview_manifest.json").write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "id": "worker_artifact_abc123",
                        "kind": "zh_dub_mp4",
                        "name": "lecture_zh_dub.mp4",
                        "preview_path": str(cached),
                        "remote_cache": True,
                        "full_available": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    jobs = [{"id": "job-1", "worker_artifacts": [{"ref_id": "abc123", "name": "lecture_zh_dub.mp4"}]}]

    row = next(item for item in artifact_catalog(config, jobs) if item["id"] == "worker_artifact_abc123")
    assert row["path"] == str(cached)
    assert row["remote_cache"] is True
    assert row["remote_preview"] is True
    signed = with_signed_urls([row], secret="secret", username="student", ttl_seconds=60)[0]
    assert "download_url" in signed
    assert "request_cache_url" not in signed


def test_delete_preview_manifest_record_removes_thumbnail(tmp_path):
    config = make_config(tmp_path)
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    preview = preview_dir / "demo_preview.mp4"
    thumbnail = preview_dir / "demo_thumb.jpg"
    preview.write_bytes(b"small mp4")
    thumbnail.write_bytes(b"jpg")
    row = {
        "remote_preview": True,
        "path": str(preview),
        "thumbnail_path": str(thumbnail),
    }

    result = safe_delete_artifact_record(row, config)

    assert result["bytes"] == len(b"small mp4") + len(b"jpg")
    assert not preview.exists()
    assert not thumbnail.exists()


def test_safe_delete_stays_inside_managed_roots(tmp_path):
    config = make_config(tmp_path)
    out_file = Path(config["output_dir"]) / "delete_me.txt"
    out_file.write_text("x", encoding="utf-8")

    result = safe_delete_artifact(out_file, config)
    assert result["deleted"]
    assert not out_file.exists()

    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        safe_delete_artifact(outside, config)


def test_report_bundle_delete_removes_outputs(tmp_path):
    config = make_config(tmp_path)
    out = Path(config["output_dir"])
    video = out / "demo_zh_dub.mp4"
    video.write_bytes(b"mp4")
    report = out / "demo_report.json"
    report_md = out / "demo_report.md"
    report.write_text(json.dumps({"name": "demo", "outputs": {"zh_dub_mp4": str(video)}}), encoding="utf-8")
    report_md.write_text("report", encoding="utf-8")

    bundle = next(row for row in artifact_catalog(config) if row["kind"] == "report_bundle")
    result = safe_delete_artifact_record(bundle, config)

    assert result["bytes"] >= 3
    assert not video.exists()
    assert not report.exists()
    assert not report_md.exists()


def test_cleanup_expired_files_dry_run(tmp_path):
    config = make_config(tmp_path)
    stale = Path(config["work_dir"]) / "old.tmp"
    stale.write_text("old", encoding="utf-8")
    old_time = stale.stat().st_mtime - 10 * 86400
    import os

    os.utime(stale, (old_time, old_time))
    result = cleanup_expired_files(config, older_than_days=7, dry_run=True)
    assert result["count"] == 1
    assert stale.exists()


def test_cleanup_expired_files_includes_preview_cache(tmp_path):
    config = make_config(tmp_path)
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    stale = preview_dir / "old_preview.mp4"
    stale.write_bytes(b"old")
    old_time = stale.stat().st_mtime - 10 * 86400
    import os

    os.utime(stale, (old_time, old_time))
    result = cleanup_expired_files(config, older_than_days=7, dry_run=True)
    assert any(item["path"] == str(stale.resolve()) for item in result["items"])
