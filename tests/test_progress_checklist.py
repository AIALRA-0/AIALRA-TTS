import json
import os
from pathlib import Path

import yaml

from ecse_localizer.cli import main
from ecse_localizer.progress_checklist import (
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_VALIDATION,
    build_progress_checklist,
    latest_batch_background,
    write_progress_checklist,
)
from ecse_localizer.utils import read_json


def config(tmp_path: Path) -> dict:
    return {
        "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"),
        "work_dir": str(tmp_path / "runs"),
        "privacy": {
            "allow_cloud_api": False,
            "allow_upload_media": False,
            "allow_voice_clone_without_consent": False,
        },
    }


def write_platform_report(tmp_path: Path, *, passed: bool = True) -> Path:
    path = tmp_path / "runs" / "platform_check" / "platform_check_report.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "pass": passed,
                "summary": {"checked_gates": 6, "failed_gates": [] if passed else ["webui_api_smoke"]},
                "gates": {
                    "release_check": {"pass": True},
                    "translation_sample": {"pass": True},
                    "remote_smoke": {"pass": True},
                    "webui_api_smoke": {"pass": passed},
                    "deploy_template_guard": {"pass": True},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_smoke_report(tmp_path: Path, *, passed: bool = True) -> Path:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "en_srt": output / "sample_smoke_90s_en.srt",
        "zh_srt": output / "sample_smoke_90s_zh.srt",
        "bilingual_srt": output / "sample_smoke_90s_bilingual.srt",
        "bilingual_ass": output / "sample_smoke_90s_bilingual.ass",
        "zh_dub_wav": output / "sample_smoke_90s_zh_dub.wav",
        "zh_dub_mp4": output / "sample_smoke_90s_zh_dub.mp4",
    }
    for path in paths.values():
        path.write_text("ok", encoding="utf-8")
    path = output / "sample_smoke_90s_report.json"
    path.write_text(
        json.dumps(
            {
                "name": "sample_smoke_90s",
                "mode": "smoke",
                "source_video": str(tmp_path / "input" / "sample.mp4"),
                "asr_backend": "existing_subtitle",
                "translation_backend": "local_llm",
                "tts": {"backend": "cosyvoice_sft", "segment_count": 12},
                "outputs": {key: str(value) for key, value in paths.items()},
                "qa": {"pass": passed, "issues": []},
            }
        ),
        encoding="utf-8",
    )
    return path


def write_full_report(tmp_path: Path, *, passed: bool = True) -> Path:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "en_srt": output / "sample_full_en.srt",
        "zh_srt": output / "sample_full_zh.srt",
        "bilingual_srt": output / "sample_full_bilingual.srt",
        "bilingual_ass": output / "sample_full_bilingual.ass",
        "zh_dub_wav": output / "sample_full_zh_dub.wav",
        "zh_dub_mp4": output / "sample_full_zh_dub.mp4",
    }
    for path in paths.values():
        path.write_text("ok", encoding="utf-8")
    path = output / "sample_full_report.json"
    path.write_text(
        json.dumps(
            {
                "name": "sample_full",
                "mode": "compact_timeline_rerender",
                "source_video": str(tmp_path / "input" / "sample.mp4"),
                "asr_backend": "existing_subtitle",
                "translation_backend": "local_llm",
                "tts": {"backend": "cosyvoice_sft", "duration": 5316.7, "segment_count": 949},
                "outputs": {key: str(value) for key, value in paths.items()},
                "qa": {"pass": passed, "issues": []},
            }
        ),
        encoding="utf-8",
    )
    return path


def write_no_speech_report(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "en_srt": output / "sample_no_speech_en.srt",
        "zh_srt": output / "sample_no_speech_zh.srt",
        "bilingual_srt": output / "sample_no_speech_bilingual.srt",
        "bilingual_ass": output / "sample_no_speech_bilingual.ass",
        "zh_dub_wav": output / "sample_no_speech_zh_dub.wav",
        "zh_dub_mp4": output / "sample_no_speech_zh_dub.mp4",
    }
    for path in paths.values():
        path.write_text("ok", encoding="utf-8")
    path = output / "sample_no_speech_report.json"
    path.write_text(
        json.dumps(
            {
                "name": "sample_no_speech",
                "mode": "full",
                "source_video": str(tmp_path / "input" / "sample-no-speech.mp4"),
                "asr_backend": "faster_whisper",
                "asr": {"no_speech_detected": True},
                "translation_backend": "none_no_speech",
                "tts": {"backend": "silence_no_speech", "duration": 3.0, "segment_count": 0},
                "outputs": {key: str(value) for key, value in paths.items()},
                "qa": {"pass": True, "issues": [{"type": "no_speech_detected", "severity": "medium"}]},
                "segments": {"en": [], "zh": []},
            }
        ),
        encoding="utf-8",
    )
    return path


def write_batch_report(tmp_path: Path, *, passed: bool = True) -> Path:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    path = output / "batch_report.json"
    results = [{"video": "one.mp4", "pass": True, "skipped": False}]
    if not passed:
        results.append({"video": "two.mp4", "pass": False, "error": "failed"})
    path.write_text(json.dumps({"results": results}), encoding="utf-8")
    return path


def write_batch_background_state(tmp_path: Path, *, done: bool = False, exit_code: int = 0) -> Path:
    run_dir = tmp_path / "runs" / "batch_background"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = "batch_chunk_20260617_200000"
    state_path = run_dir / f"{run_id}.json"
    done_path = run_dir / f"{run_id}_done.json"
    state_path.write_text(
        json.dumps(
            {
                "kind": "batch_chunk",
                "run_id": run_id,
                "pid": 12345,
                "started_at": "2026-06-17T20:00:00",
                "limit": 1,
                "shortest_first": True,
                "stdout_log": str(tmp_path / "logs" / f"{run_id}.out.log"),
                "stderr_log": str(tmp_path / "logs" / f"{run_id}.err.log"),
                "done_marker": str(done_path),
            }
        ),
        encoding="utf-8",
    )
    if done:
        done_path.write_text(
            json.dumps({"run_id": run_id, "exit_code": exit_code, "completed_at": "2026-06-17T21:00:00"}),
            encoding="utf-8",
        )
    return state_path


def test_progress_checklist_tracks_objective_and_latest_platform_gate(tmp_path):
    write_platform_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_platform_check"]["pass"] is True
    assert checklist["summary"]["total"] >= 20
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["translation.best_quality"]["status"] == STATUS_DONE
    assert rows["worker.lifecycle"]["status"] == STATUS_DONE
    assert rows["storage.preview_cache"]["status"] == STATUS_DONE
    assert rows["deploy.real_contabo"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.smoke_90s"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.first_full_lecture"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.batch_all"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.real_video"]["status"] == STATUS_NEEDS_VALIDATION


def test_progress_checklist_marks_real_smoke_done_but_full_validation_in_progress(tmp_path):
    write_platform_report(tmp_path, passed=True)
    smoke_path = write_smoke_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_real_video_smoke"]["pass"] is True
    assert checklist["latest_real_video_smoke"]["path"] == str(smoke_path)
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["validation.smoke_90s"]["status"] == STATUS_DONE
    assert rows["validation.real_video"]["status"] == STATUS_IN_PROGRESS
    assert rows["validation.first_full_lecture"]["status"] == STATUS_NEEDS_VALIDATION
    assert rows["validation.batch_all"]["status"] == STATUS_NEEDS_VALIDATION


def test_progress_checklist_marks_full_lecture_done_when_report_outputs_exist(tmp_path):
    write_platform_report(tmp_path, passed=True)
    write_smoke_report(tmp_path, passed=True)
    full_path = write_full_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_full_lecture"]["pass"] is True
    assert checklist["latest_full_lecture"]["path"] == str(full_path)
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["validation.smoke_90s"]["status"] == STATUS_DONE
    assert rows["validation.first_full_lecture"]["status"] == STATUS_DONE
    assert rows["validation.real_video"]["status"] == STATUS_DONE
    assert rows["validation.batch_all"]["status"] == STATUS_NEEDS_VALIDATION


def test_progress_checklist_does_not_use_no_speech_clip_as_representative_full_lecture(tmp_path):
    write_platform_report(tmp_path, passed=True)
    write_smoke_report(tmp_path, passed=True)
    full_path = write_full_report(tmp_path, passed=True)
    no_speech_path = write_no_speech_report(tmp_path)
    newer = full_path.stat().st_mtime + 5
    os.utime(no_speech_path, (newer, newer))

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_full_lecture"]["path"] == str(full_path)
    assert checklist["latest_full_lecture"]["segment_count"] == 949
    assert checklist["latest_full_lecture"]["no_speech"] is False


def test_progress_checklist_marks_batch_done_from_batch_report(tmp_path):
    write_platform_report(tmp_path, passed=True)
    write_smoke_report(tmp_path, passed=True)
    write_full_report(tmp_path, passed=True)
    batch_path = write_batch_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_batch_process"]["pass"] is True
    assert checklist["latest_batch_process"]["path"] == str(batch_path)
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["validation.batch_all"]["status"] == STATUS_DONE


def test_progress_checklist_does_not_mark_partial_batch_done(tmp_path):
    write_platform_report(tmp_path, passed=True)
    write_smoke_report(tmp_path, passed=True)
    write_full_report(tmp_path, passed=True)
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    batch_path = output / "batch_report.json"
    batch_path.write_text(
        json.dumps(
            {
                "total": 3,
                "processed": 1,
                "skipped": 1,
                "failed": 0,
                "deferred": 1,
                "complete_all": False,
                "results": [
                    {"video": "done.mp4", "pass": True, "skipped": True},
                    {"video": "processed.mp4", "pass": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    checklist = build_progress_checklist(config(tmp_path))

    assert checklist["latest_batch_process"]["pass"] is False
    assert checklist["latest_batch_process"]["deferred"] == 1
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["validation.batch_all"]["status"] == STATUS_NEEDS_VALIDATION


def test_progress_checklist_marks_background_batch_in_progress(tmp_path):
    write_platform_report(tmp_path, passed=True)
    write_smoke_report(tmp_path, passed=True)
    write_full_report(tmp_path, passed=True)
    state_path = write_batch_background_state(tmp_path, done=False)

    checklist = build_progress_checklist(config(tmp_path))

    background = checklist["latest_batch_background"]
    assert background["available"] is True
    assert background["status"] == "running_or_unknown"
    assert background["path"] == str(state_path)
    rows = {item["id"]: item for item in checklist["items"]}
    assert rows["validation.batch_all"]["status"] == STATUS_IN_PROGRESS
    assert "Background batch chunk" in rows["validation.batch_all"]["evidence"]


def test_latest_batch_background_reads_done_marker(tmp_path):
    state_path = write_batch_background_state(tmp_path, done=True, exit_code=0)

    background = latest_batch_background(config(tmp_path))

    assert background["available"] is True
    assert background["status"] == "completed"
    assert background["exit_code"] == 0
    assert background["path"] == str(state_path)


def test_progress_checklist_reports_batch_readiness_counts(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "sample.mp4").write_bytes(b"placeholder")
    (input_dir / "pending.mp4").write_bytes(b"placeholder")
    write_platform_report(tmp_path, passed=True)
    write_full_report(tmp_path, passed=True)

    checklist = build_progress_checklist(config(tmp_path))

    readiness = checklist["batch_readiness"]
    assert readiness["available"] is True
    assert readiness["video_count"] == 2
    assert readiness["completed_count"] == 1
    assert readiness["pending_count"] == 1
    assert readiness["pending"] == ["pending.mp4"]
    rows = {item["id"]: item for item in checklist["items"]}
    assert "1/2" in rows["validation.batch_all"]["evidence"]


def test_write_progress_checklist_creates_markdown_and_json(tmp_path):
    write_platform_report(tmp_path, passed=True)
    out = tmp_path / "checklist"

    result = write_progress_checklist(out, config(tmp_path))

    json_path = Path(result["json"])
    md_path = Path(result["markdown"])
    assert json_path.exists()
    assert md_path.exists()
    assert read_json(json_path)["mode"] == "aialra_progress_checklist"
    text = md_path.read_text(encoding="utf-8")
    assert "AIALRA Local Video Localizer Progress Checklist" in text
    assert "deploy.real_contabo" in text
    assert "validation.smoke_90s" in text
    assert "validation.real_video" in text


def test_progress_checklist_cli_writes_reports(tmp_path, capsys):
    cfg = config(tmp_path)
    Path(cfg["input_dir"]).mkdir()
    Path(cfg["output_dir"]).mkdir()
    Path(cfg["work_dir"]).mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "out"

    rc = main(["--config", str(config_path), "progress-checklist", "--output", str(out), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["mode"] == "aialra_progress_checklist"
    assert Path(payload["json"]).exists()
    assert Path(payload["markdown"]).exists()
