import json
import os
import time
from pathlib import Path

import pytest

from ecse_localizer.artifacts import (
    artifact_catalog,
    artifact_id,
    cleanup_expired_files,
    filter_artifact_records,
    filter_artifacts_for_user,
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


def old_iso(days: int = 10) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))


def make_stale(path: Path, *, days: int = 10) -> None:
    old_time = time.time() - days * 86400
    os.utime(path, (old_time, old_time))


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


def test_artifact_records_filter_by_project_folder_job_and_kind(tmp_path):
    config = make_config(tmp_path)
    out = Path(config["output_dir"])
    video = out / "lecture_zh_dub.mp4"
    video.write_bytes(b"mp4")
    report = out / "lecture_report.json"
    report.write_text(
        json.dumps({"name": "lecture", "outputs": {"zh_dub_mp4": str(video)}}),
        encoding="utf-8",
    )
    jobs = [
        {
            "id": "job-1",
            "user": "student",
            "result_report": str(report),
            "metadata": {"project_id": "course", "folder_id": "week_1"},
        }
    ]

    rows = artifact_catalog(config, jobs)

    mp4 = next(row for row in rows if row["kind"] == "zh_dub_mp4")
    assert mp4["project_id"] == "course"
    assert mp4["folder_id"] == "week_1"
    assert mp4["job_id"] == "job-1"
    assert [row["id"] for row in filter_artifact_records(rows, project_id="course", folder_id="week_1", job_id="job-1", kind="zh_dub_mp4")] == [mp4["id"]]
    assert filter_artifact_records(rows, project_id="other") == []
    assert filter_artifact_records(rows, folder_id="week_2") == []
    assert filter_artifact_records(rows, job_id="job-2") == []
    assert filter_artifact_records(rows, kind="bilingual_srt") == []


def test_artifact_catalog_hides_deleted_job_artifacts_by_default(tmp_path):
    config = make_config(tmp_path)
    out = Path(config["output_dir"])
    video = out / "lecture_zh_dub.mp4"
    video.write_bytes(b"mp4")
    report = out / "lecture_report.json"
    report.write_text(json.dumps({"name": "lecture", "outputs": {"zh_dub_mp4": str(video)}}), encoding="utf-8")
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    preview = preview_dir / "lecture_preview.mp4"
    preview.write_bytes(b"preview")
    (preview_dir / "preview_manifest.json").write_text(
        json.dumps({"previews": [{"id": "preview-1", "job_id": "job-1", "preview_path": str(preview)}]}),
        encoding="utf-8",
    )
    jobs = [
        {
            "id": "job-1",
            "status": "deleted",
            "user": "student",
            "result_report": str(report),
            "metadata": {"project_id": "course", "folder_id": "week_1"},
            "worker_artifacts": [{"ref_id": "ref1", "name": "lecture_zh_dub.mp4"}],
        }
    ]

    visible = artifact_catalog(config, jobs)
    deleted_view = artifact_catalog(config, jobs, include_deleted=True)

    assert visible == []
    assert {row["kind"] for row in deleted_view} >= {"report_bundle", "zh_dub_mp4", "remote_preview"}
    assert any(row.get("remote_worker_artifact") for row in deleted_view)
    assert all(row.get("source_deleted") is True for row in deleted_view)

    signed = with_signed_urls(deleted_view, secret="secret", username="student", ttl_seconds=60)
    exposed = [row for row in signed if row["kind"] != "report_bundle"]
    assert exposed
    assert all("download_url" not in row for row in exposed)
    assert all("preview_url" not in row for row in exposed)
    assert all("request_cache_url" not in row for row in exposed)
    assert all(row["download_disabled_reason"] == "source_job_deleted" for row in exposed)


def test_artifact_filter_hides_ownerless_rows_from_non_admin():
    rows = [
        {"id": "student", "owner": "student.one"},
        {"id": "other", "owner": "student.two"},
        {"id": "legacy-ownerless"},
    ]

    visible = filter_artifacts_for_user(rows, "student.one", admin=False)

    assert [row["id"] for row in visible] == ["student"]
    assert [row["id"] for row in filter_artifacts_for_user(rows, "admin", admin=True)] == ["student", "other", "legacy-ownerless"]


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
    other = preview_dir / "other_preview.mp4"
    preview.write_bytes(b"small mp4")
    thumbnail.write_bytes(b"jpg")
    other.write_bytes(b"other")
    manifest = preview_dir / "preview_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "id": "preview-1",
                        "name": "demo",
                        "preview_path": str(preview),
                        "thumbnail_path": str(thumbnail),
                    },
                    {"id": "preview-2", "name": "other", "preview_path": str(other)},
                ]
            }
        ),
        encoding="utf-8",
    )
    row = {
        "id": "preview-1",
        "remote_preview": True,
        "path": str(preview),
        "thumbnail_path": str(thumbnail),
    }

    result = safe_delete_artifact_record(row, config)

    assert result["bytes"] == len(b"small mp4") + len(b"jpg")
    assert not preview.exists()
    assert not thumbnail.exists()
    assert result["manifest"]["removed"] == 1
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert [item["id"] for item in saved["previews"]] == ["preview-2"]
    assert other.exists()


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
    make_stale(stale)
    result = cleanup_expired_files(config, older_than_days=7, dry_run=True)
    assert result["count"] == 1
    assert stale.exists()


def test_cleanup_expired_files_includes_preview_cache(tmp_path):
    config = make_config(tmp_path)
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    stale = preview_dir / "old_preview.mp4"
    stale.write_bytes(b"old")
    make_stale(stale)
    result = cleanup_expired_files(config, older_than_days=7, dry_run=True)
    assert any(item["path"] == str(stale.resolve()) for item in result["items"])


def test_cleanup_deleted_job_artifacts_dry_run_then_apply(tmp_path):
    config = make_config(tmp_path)
    job_dir = tmp_path / "jobs"
    job_dir.mkdir()
    config["webui"]["job_dir"] = str(job_dir)
    out = Path(config["output_dir"])
    video = out / "demo_zh_dub.mp4"
    report = out / "demo_report.json"
    report_md = out / "demo_report.md"
    log = job_dir / "job.log"
    video.write_bytes(b"mp4")
    report.write_text(json.dumps({"name": "demo", "outputs": {"zh_dub_mp4": str(video)}}), encoding="utf-8")
    report_md.write_text("report", encoding="utf-8")
    log.write_text("log", encoding="utf-8")
    job_record = job_dir / "job-1.json"
    job_record.write_text(
        json.dumps(
            {
                "id": "job-1",
                "status": "deleted",
                "deleted_at": old_iso(),
                "result_report": str(report),
                "log": str(log),
            }
        ),
        encoding="utf-8",
    )

    dry = cleanup_expired_files(config, older_than_days=7, dry_run=True)

    dry_paths = {item["path"] for item in dry["items"]}
    assert str(report.resolve()) in dry_paths
    assert str(video.resolve()) in dry_paths
    assert str(log.resolve()) in dry_paths
    assert report.exists() and report_md.exists() and video.exists() and log.exists()
    assert job_record.exists()

    applied = cleanup_expired_files(config, older_than_days=7, dry_run=False)

    assert applied["count"] >= 4
    assert not report.exists()
    assert not report_md.exists()
    assert not video.exists()
    assert not log.exists()
    assert job_record.exists()


def test_cleanup_deleted_job_prunes_preview_manifest_rows(tmp_path):
    config = make_config(tmp_path)
    job_dir = tmp_path / "jobs"
    job_dir.mkdir()
    config["webui"]["job_dir"] = str(job_dir)
    preview_dir = Path(config["webui"]["preview_dir"])
    preview_dir.mkdir()
    preview = preview_dir / "demo_preview.mp4"
    thumbnail = preview_dir / "demo_thumb.jpg"
    preview.write_bytes(b"preview")
    thumbnail.write_bytes(b"thumb")
    (preview_dir / "preview_manifest.json").write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "id": "job-1-preview",
                        "job_id": "job-1",
                        "preview_path": str(preview),
                        "thumbnail_path": str(thumbnail),
                        "updated_at": old_iso(days=1),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "job-1.json").write_text(
        json.dumps({"id": "job-1", "status": "deleted", "deleted_at": old_iso()}),
        encoding="utf-8",
    )

    result = cleanup_expired_files(config, older_than_days=7, dry_run=False)

    assert any(item["reason"] == "deleted_job_preview" for item in result["items"])
    assert not preview.exists()
    assert not thumbnail.exists()
    manifest = json.loads((preview_dir / "preview_manifest.json").read_text(encoding="utf-8"))
    assert manifest["previews"] == []


def test_cleanup_preserves_platform_and_job_metadata_under_work_dir(tmp_path):
    config = make_config(tmp_path)
    work = Path(config["work_dir"])
    job_dir = work / "webui_jobs"
    platform_dir = work / "platform"
    job_dir.mkdir()
    platform_dir.mkdir()
    config["webui"]["job_dir"] = str(job_dir)
    config["webui"]["platform_dir"] = str(platform_dir)
    job_record = job_dir / "job-1.json"
    users = platform_dir / "users.json"
    temp = work / "old.tmp"
    job_record.write_text(json.dumps({"id": "job-1", "status": "done"}), encoding="utf-8")
    users.write_text(json.dumps({"users": []}), encoding="utf-8")
    temp.write_text("old", encoding="utf-8")
    make_stale(job_record)
    make_stale(users)
    make_stale(temp)

    cleanup_expired_files(config, older_than_days=7, dry_run=False)

    assert job_record.exists()
    assert users.exists()
    assert not temp.exists()
