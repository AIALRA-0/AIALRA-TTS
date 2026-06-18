from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

from .completion import completed_report_for
from .scan import find_videos
from .utils import PROJECT_ROOT, ensure_dir, read_json, write_json


STATUS_DONE = "done"
STATUS_IN_PROGRESS = "in_progress"
STATUS_NEEDS_VALIDATION = "needs_real_world_validation"
STATUS_PENDING = "pending"

DETAILS_BY_ID = {
    "core.scan": [
        "Scan only source lecture media and subtitle files; ignore managed output, run, cache, and project folders.",
        "Pair same-name .srt/.vtt/.ass subtitle files with each video when present.",
        "Use ffprobe metadata for duration, resolution, codec, and audio stream reporting.",
        "Keep original lecture files read-only from the pipeline perspective; write all generated files under configured output/work dirs.",
        "Handle Windows paths with spaces and non-ASCII characters in PowerShell and Python.",
    ],
    "core.asr": [
        "Use existing English subtitles first when coverage and timing are usable.",
        "Normalize subtitle timing and text before downstream translation/TTS.",
        "Run local ASR when subtitles are missing, unusable, or insufficient.",
        "Prefer GPU ASR backends when available and keep CPU/quantized fallback paths.",
        "Emit segment-level and word/metadata JSON for later QA, subtitle, and TTS alignment.",
        "Avoid any cloud ASR endpoint or media upload.",
    ],
    "core.audio": [
        "Extract mono 16 kHz WAV with ffmpeg for ASR/TTS preparation.",
        "Apply loudnorm plus highpass/lowpass cleanup locally.",
        "Attempt configured enhancement backend order and fall back to ffmpeg cleanup when optional models are unavailable.",
        "Preserve original video/audio files unchanged.",
        "Record enhancement backend, failures, and fallback decisions in logs/reports.",
    ],
    "core.outputs": [
        "Generate English subtitles: *_en.srt and *_en.vtt when supported.",
        "Generate Chinese subtitles: *_zh.srt and *_zh.vtt when supported.",
        "Generate bilingual subtitles: *_bilingual.srt and styled *_bilingual.ass.",
        "Generate Chinese dubbed audio: *_zh_dub.wav.",
        "Generate Chinese dubbed video: *_zh_dub.mp4.",
        "Optionally generate hard-subtitled MP4 when configured.",
        "Generate per-video report.md/report.json and course glossary.tsv/glossary.json.",
        "Never overwrite source media or source subtitle files.",
    ],
    "translation.best_quality": [
        "Merge subtitle fragments into paragraph-level translation units before translating.",
        "Apply a course style guide for natural Chinese lecture language.",
        "Run literal translation first to preserve technical information.",
        "Run lecture rewrite to improve flow, clarity, tone, and spoken delivery.",
        "Run coherence pass to reduce machine-translation feel across neighboring segments.",
        "Run fidelity repair only on segments failing preservation/consistency checks.",
        "Flag suspicious mistranslation, omission, number mismatch, formula mismatch, and untranslated residue.",
        "Use local LLM JSON responses with repair/retry logging.",
    ],
    "translation.protection": [
        "Protect code blocks and inline code before LLM translation.",
        "Protect formulas, variables, subscripts, superscripts, and complexity notation.",
        "Protect Windows paths, URLs, filenames, versions, and model names.",
        "Protect numbers, units, voltages, frequencies, scientific notation, and course codes.",
        "Restore placeholders after translation and audit any missing or modified protected token.",
        "Cover examples including O(n log n), x_i, C:\\path\\file.py, https://example.com, ResNet-50, ECSE 4961, 3.3V, 5 kHz, and 10^-6.",
    ],
    "tts.sync": [
        "Synthesize each segment independently through the configured local TTS backend.",
        "Detect segment overruns before final mix.",
        "Compress wording first when a segment is too long.",
        "Increase TTS speed only within configured safe bounds.",
        "Use ffmpeg time compression only as a final small correction.",
        "Insert speech according to the original timeline while enforcing no overlap.",
        "Keep configurable short end gaps rather than leaving excessive silence.",
        "Apply gain/normalization so the dubbed voice is clearly audible.",
        "Report any severe overrun or timing compromise.",
    ],
    "tts.voice_policy": [
        "Default to a neutral clear teaching voice.",
        "Do not clone the original lecturer or any user voice by default.",
        "Enable reference voice only when authorized reference audio and consent README are present.",
        "Keep voice cloning controls explicit in config and reports.",
    ],
    "webui.auth_users": [
        "Provide login UI and authenticated API session handling.",
        "Support invite-style/admin-created users instead of public registration by default.",
        "Support admin user enable/disable operations.",
        "Associate projects, jobs, quotas, and artifacts with the authenticated user.",
        "Reject unauthorized access to other users' jobs and artifacts.",
    ],
    "webui.projects_templates": [
        "Create and list projects.",
        "Create and list folders within projects.",
        "Submit localization jobs with source/target language and style parameters.",
        "Store reusable parameter templates for quality, subtitles, TTS, timing, and muxing.",
        "Show job history with retry/delete/restore lifecycle operations.",
        "Represent paused, retrying, failed, done, deleted, and cancelled states.",
    ],
    "webui.metrics_sse": [
        "Expose worker online/offline status to the dashboard.",
        "Show queue and active job status.",
        "Show CPU, GPU, VRAM, disk, and quota metrics.",
        "Use SSE for live updates and polling fallback when SSE is unavailable.",
        "Show sanitized log tail/progress summaries without leaking local paths or secrets.",
    ],
    "worker.outbound": [
        "Run Windows worker as a local service/script.",
        "Worker initiates all network communication to the remote web service.",
        "Support reverse tunnel/VPN deployment without opening a public Windows inbound port.",
        "Keep GPU/CPU inference and full media storage on the Windows machine.",
    ],
    "worker.lifecycle": [
        "Send signed heartbeat and capability reports.",
        "Claim queued jobs safely.",
        "Report running progress and failures.",
        "Honor cancel/control requests from the remote UI.",
        "Recover stale claimed/running jobs into retryable state.",
        "Keep failed jobs isolated so one video does not crash the whole queue.",
    ],
    "storage.quotas": [
        "Track remote web quota separately from Windows local storage quota.",
        "Track user-level and project-level quotas.",
        "Reserve space for active jobs before accepting work.",
        "Reject jobs clearly when quota or disk free-space checks fail.",
        "Keep soft-deleted records until cleanup physically removes files.",
    ],
    "storage.preview_cache": [
        "Store original media and full outputs on Windows by default.",
        "Upload only small thumbnails, metadata, low-bitrate previews, or short-term requested caches to Contabo.",
        "Generate signed temporary links for downloads/previews.",
        "Provide cleanup TTL for temporary caches and intermediate files.",
        "Allow users to delete generated outputs and reclaim quota.",
    ],
    "security.no_cloud": [
        "Default allow_cloud_api=false and allow_upload_media=false.",
        "Fail closed on OpenAI/Google/Azure/DeepL/Baidu/Tencent/Aliyun/ElevenLabs/Rask/HeyGen-style inference endpoints.",
        "Permit only local inference endpoints or explicitly open-source model downloads.",
        "Record model/code license and commercial-use risk in licenses_report.md.",
    ],
    "security.worker_auth": [
        "Authenticate worker calls with HMAC or equivalent signed requests.",
        "Include timestamp and nonce replay protection.",
        "Use different secrets for production and local development.",
        "Never commit real worker tokens or server secrets.",
    ],
    "security.redaction": [
        "Redact Windows usernames, full local paths, private IPs, tokens, and command lines from remote-visible logs.",
        "Return stable artifact references instead of raw local filesystem paths.",
        "Keep .env and other sensitive config ignored by git.",
        "Run release/secret-safety checks before publishing.",
    ],
    "deploy.package": [
        "Provide DEPLOY_CONTABO_PROMPT.md for a server-side deployment agent.",
        "Provide .env.example without secrets.",
        "Provide Docker Compose/service templates for the remote web service.",
        "Provide proxy config examples with SSE no-buffering.",
        "Provide systemd service/timer examples for app and cleanup tasks.",
        "Provide Windows worker start/health scripts.",
        "Document GitHub workflow, tags, and release checks.",
    ],
    "deploy.real_contabo": [
        "Deploy the remote web service on the real Contabo host.",
        "Configure domain, TLS, reverse proxy, and persistent database/storage.",
        "Register the Windows worker through outbound-only connectivity.",
        "Verify heartbeat, queue claim, preview, download, cancellation, and offline recovery from the real browser UI.",
        "Validate storage quotas against the real Contabo disk budget.",
    ],
    "validation.smoke_90s": [
        "Run 60-120 second smoke on a real lecture.",
        "Verify audio extraction and enhancement.",
        "Verify existing subtitle or local ASR path.",
        "Verify glossary extraction and local LLM translation.",
        "Verify lecture rewrite/coherence behavior.",
        "Verify TTS generation, no overlap, bilingual subtitles, muxed MP4, and QA report.",
        "Verify required output files exist and ffprobe can read the MP4.",
    ],
    "validation.first_full_lecture": [
        "Process one complete lecture end to end.",
        "Inspect first 10 subtitle entries for non-empty Chinese, retained English, and non-overlap.",
        "Check TTS duration/timeline drift and serious overrun reporting.",
        "Check final MP4 readability with ffprobe.",
        "Review QA warnings for mistranslation, missing outputs, subtitle issues, and timing issues.",
    ],
    "validation.batch_all": [
        "Process all detected lectures without starting with an unsafe all-at-once run.",
        "Run resumable chunks that can recover from failures.",
        "Skip already-passed videos.",
        "Keep per-video logs and reports.",
        "Continue after per-video fallback paths where possible.",
        "Stop and surface actionable logs on hard failure.",
        "Confirm final batch_report.json covers every detected lecture.",
    ],
    "validation.real_video": [
        "Run the latest code, not stale earlier outputs, on representative real media.",
        "Inspect subtitles, TTS timing, voice clarity, muxed MP4, reports, and glossary.",
        "Ensure latest config changes for voice, volume, timing, and translation quality are reflected.",
    ],
    "validation.visual_ui": [
        "Open the local WebUI in a real browser.",
        "Check login interaction works from the UI, not just API smoke.",
        "Check dashboard, task table, project sidebar, settings, upload form, and status panel layout.",
        "Compare visual density and style against the requested deeeeepwiki/readlayer direction when reference screenshots/pages are available.",
        "Check desktop and mobile/responsive layouts.",
    ],
}


def build_progress_checklist(config: dict[str, Any]) -> dict[str, Any]:
    platform_report = latest_platform_report(config)
    smoke_report = latest_smoke_report(config)
    full_report = latest_full_video_report(config)
    batch_report = latest_batch_report(config)
    batch_readiness = batch_readiness_summary(config)
    batch_background = latest_batch_background(config)
    items = checklist_items(platform_report, smoke_report, full_report, batch_report, batch_readiness, batch_background)
    counts = Counter(str(item["status"]) for item in items)
    detailed_items = expand_detailed_items(items)
    detail_counts = Counter(str(item["status"]) for item in detailed_items)
    return {
        "mode": "aialra_progress_checklist",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total": len(items),
            STATUS_DONE: counts.get(STATUS_DONE, 0),
            STATUS_IN_PROGRESS: counts.get(STATUS_IN_PROGRESS, 0),
            STATUS_NEEDS_VALIDATION: counts.get(STATUS_NEEDS_VALIDATION, 0),
            STATUS_PENDING: counts.get(STATUS_PENDING, 0),
        },
        "detail_summary": {
            "total": len(detailed_items),
            STATUS_DONE: detail_counts.get(STATUS_DONE, 0),
            STATUS_IN_PROGRESS: detail_counts.get(STATUS_IN_PROGRESS, 0),
            STATUS_NEEDS_VALIDATION: detail_counts.get(STATUS_NEEDS_VALIDATION, 0),
            STATUS_PENDING: detail_counts.get(STATUS_PENDING, 0),
        },
        "latest_platform_check": platform_report_summary(platform_report),
        "latest_real_video_smoke": smoke_report_summary(smoke_report),
        "latest_full_lecture": video_report_summary(full_report),
        "latest_batch_process": batch_report_summary(batch_report),
        "latest_batch_background": batch_background,
        "batch_readiness": batch_readiness,
        "items": items,
        "detailed_items": detailed_items,
    }


def write_progress_checklist(output_dir: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    out = ensure_dir(output_dir)
    checklist = build_progress_checklist(config)
    json_path = out / "progress_checklist.json"
    md_path = out / "progress_checklist.md"
    write_json(json_path, checklist)
    md_path.write_text(render_progress_markdown(checklist), encoding="utf-8")
    checklist["json"] = str(json_path)
    checklist["markdown"] = str(md_path)
    return checklist


def latest_platform_report(config: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        Path(config.get("work_dir") or PROJECT_ROOT / "runs") / "platform_check" / "platform_check_report.json",
        PROJECT_ROOT / "runs" / "platform_check" / "platform_check_report.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = read_json(path)
            except Exception:
                continue
            if isinstance(data, dict):
                data["_path"] = str(path)
                return data
    return None


def latest_smoke_report(config: dict[str, Any]) -> dict[str, Any] | None:
    output_dir = Path(config.get("output_dir") or PROJECT_ROOT.parent / "_localizer_output")
    if not output_dir.exists():
        return None
    candidates = sorted(output_dir.glob("*smoke*_report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            data["_path"] = str(path)
            return data
    return None


def latest_full_video_report(config: dict[str, Any]) -> dict[str, Any] | None:
    output_dir = Path(config.get("output_dir") or PROJECT_ROOT.parent / "_localizer_output")
    if not output_dir.exists():
        return None
    candidates = sorted(output_dir.glob("*_report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        if "_smoke" in path.stem.lower():
            continue
        try:
            data = read_json(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("mode") or "").lower() == "smoke":
            continue
        if is_no_speech_report(data):
            continue
        data["_path"] = str(path)
        summary = video_report_summary(data)
        if summary.get("pass"):
            return data
    return None


def latest_batch_report(config: dict[str, Any]) -> dict[str, Any] | None:
    output_dir = Path(config.get("output_dir") or PROJECT_ROOT.parent / "_localizer_output")
    path = output_dir / "batch_report.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    if isinstance(data, dict):
        data["_path"] = str(path)
        return data
    return None


def latest_batch_background(config: dict[str, Any]) -> dict[str, Any]:
    configured_work_dir = config.get("work_dir")
    roots = [Path(configured_work_dir) / "batch_background"] if configured_work_dir else [PROJECT_ROOT / "runs" / "batch_background"]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("batch_chunk_*.json"):
            name = path.name.lower()
            if name.endswith("_done.json") or name.endswith("_stop.json"):
                continue
            candidates.append(path)
    if not candidates:
        return {"available": False, "status": "not_started", "path": ""}
    state_path = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        state = read_json(state_path)
    except Exception as exc:
        return {"available": True, "status": "unreadable", "path": str(state_path), "error": str(exc)}
    if not isinstance(state, dict):
        return {"available": True, "status": "unreadable", "path": str(state_path), "error": "state is not an object"}

    done_path = Path(str(state.get("done_marker") or state_path.with_name(f"{state_path.stem}_done.json")))
    stop_path = Path(str(state.get("stop_marker") or state_path.with_name(f"{state_path.stem}_stop.json")))
    done: dict[str, Any] = {}
    if done_path.exists():
        try:
            loaded = read_json(done_path)
            if isinstance(loaded, dict):
                done = loaded
        except Exception as exc:
            done = {"error": str(exc)}

    if done:
        try:
            exit_code = int(done.get("exit_code"))
        except (TypeError, ValueError):
            exit_code = None
        status = "completed" if exit_code == 0 else "failed"
    elif stop_path.exists():
        status = "stop_requested"
        exit_code = None
    else:
        status = "running_or_unknown"
        exit_code = None

    return {
        "available": True,
        "status": status,
        "path": str(state_path),
        "run_id": str(state.get("run_id") or state_path.stem),
        "pid": state.get("pid"),
        "started_at": str(state.get("started_at") or ""),
        "completed_at": str(done.get("completed_at") or ""),
        "exit_code": exit_code,
        "limit": int(state.get("limit") or 0),
        "shortest_first": bool(state.get("shortest_first")),
        "stdout_log": str(state.get("stdout_log") or ""),
        "stderr_log": str(state.get("stderr_log") or ""),
        "done_marker": str(done_path),
        "stop_marker": str(stop_path),
    }


def platform_report_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {"available": False, "pass": None, "path": "", "failed_gates": []}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {
        "available": True,
        "pass": bool(report.get("pass")),
        "path": str(report.get("_path") or ""),
        "failed_gates": summary.get("failed_gates", []),
        "checked_gates": summary.get("checked_gates", 0),
    }


def required_video_outputs() -> list[str]:
    return ["en_srt", "zh_srt", "bilingual_srt", "bilingual_ass", "zh_dub_wav", "zh_dub_mp4"]


def missing_video_outputs(report: dict[str, Any]) -> list[str]:
    outputs = report.get("outputs") if isinstance(report.get("outputs"), dict) else {}
    return [key for key in required_video_outputs() if not outputs.get(key) or not Path(str(outputs.get(key))).exists()]


def is_no_speech_report(report: dict[str, Any]) -> bool:
    asr = report.get("asr") if isinstance(report.get("asr"), dict) else {}
    tts = report.get("tts") if isinstance(report.get("tts"), dict) else {}
    return bool(asr.get("no_speech_detected")) or str(tts.get("backend") or "") == "silence_no_speech"


def report_segment_count(report: dict[str, Any], tts: dict[str, Any]) -> int:
    if "segment_count" in tts:
        try:
            return int(tts.get("segment_count") or 0)
        except (TypeError, ValueError):
            return 0
    segments = report.get("segments")
    if isinstance(segments, dict):
        rows = segments.get("en") if isinstance(segments.get("en"), list) else segments.get("zh")
        return len(rows) if isinstance(rows, list) else 0
    if isinstance(segments, list):
        return len(segments)
    return 0


def smoke_report_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {
            "available": False,
            "pass": None,
            "path": "",
            "mode": "",
            "segment_count": 0,
            "issue_count": 0,
            "outputs_present": False,
        }
    missing_outputs = missing_video_outputs(report)
    qa = report.get("qa") if isinstance(report.get("qa"), dict) else {}
    tts = report.get("tts") if isinstance(report.get("tts"), dict) else {}
    return {
        "available": True,
        "pass": bool(qa.get("pass")) and not missing_outputs,
        "path": str(report.get("_path") or ""),
        "mode": str(report.get("mode") or ""),
        "name": str(report.get("name") or ""),
        "source_video": str(report.get("source_video") or ""),
        "asr_backend": str(report.get("asr_backend") or ""),
        "translation_backend": str(report.get("translation_backend") or ""),
        "tts_backend": str(tts.get("backend") or ""),
        "segment_count": report_segment_count(report, tts),
        "issue_count": len(qa.get("issues") or []),
        "missing_outputs": missing_outputs,
        "outputs_present": not missing_outputs,
    }


def video_report_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {
            "available": False,
            "pass": None,
            "path": "",
            "mode": "",
            "segment_count": 0,
            "issue_count": 0,
            "outputs_present": False,
        }
    missing_outputs = missing_video_outputs(report)
    qa = report.get("qa") if isinstance(report.get("qa"), dict) else {}
    tts = report.get("tts") if isinstance(report.get("tts"), dict) else {}
    return {
        "available": True,
        "pass": bool(qa.get("pass")) and not missing_outputs,
        "path": str(report.get("_path") or ""),
        "mode": str(report.get("mode") or ""),
        "name": str(report.get("name") or ""),
        "source_video": str(report.get("source_video") or ""),
        "asr_backend": str(report.get("asr_backend") or ""),
        "translation_backend": str(report.get("translation_backend") or ""),
        "tts_backend": str(tts.get("backend") or ""),
        "duration": float(tts.get("duration") or 0),
        "segment_count": report_segment_count(report, tts),
        "no_speech": is_no_speech_report(report),
        "issue_count": len(qa.get("issues") or []),
        "missing_outputs": missing_outputs,
        "outputs_present": not missing_outputs,
    }


def batch_report_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {"available": False, "pass": None, "path": "", "total": 0, "failed": 0, "skipped": 0, "deferred": 0}
    results = report.get("results") if isinstance(report.get("results"), list) else []
    failed = [row for row in results if not row.get("pass")]
    skipped = [row for row in results if row.get("skipped")]
    total = int(report.get("total") or len(results))
    deferred = int(report.get("deferred") or 0)
    complete_all = bool(report.get("complete_all")) if "complete_all" in report else bool(results) and not failed
    return {
        "available": True,
        "pass": complete_all and not failed,
        "path": str(report.get("_path") or ""),
        "total": total,
        "failed": len(failed),
        "skipped": int(report.get("skipped") if "skipped" in report else len(skipped)),
        "deferred": deferred,
        "complete_all": complete_all,
    }


def batch_readiness_summary(config: dict[str, Any]) -> dict[str, Any]:
    input_dir = config.get("input_dir")
    output_dir = Path(config.get("output_dir") or PROJECT_ROOT.parent / "_localizer_output")
    if not input_dir:
        return {"available": False, "video_count": 0, "completed_count": 0, "pending_count": 0, "pending": []}
    try:
        videos = find_videos(str(input_dir))
    except Exception as exc:
        return {
            "available": False,
            "video_count": 0,
            "completed_count": 0,
            "pending_count": 0,
            "pending": [],
            "error": str(exc),
        }
    rows: list[dict[str, Any]] = []
    for video in videos:
        report = completed_report_for(video, output_dir)
        rows.append({"video": video.name, "completed": bool(report), "report": str(report) if report else ""})
    pending = [row["video"] for row in rows if not row["completed"]]
    return {
        "available": True,
        "video_count": len(rows),
        "completed_count": len(rows) - len(pending),
        "pending_count": len(pending),
        "pending": pending[:25],
        "pending_truncated": len(pending) > 25,
    }


def gate_status(report: dict[str, Any] | None, gate: str) -> str:
    if not report:
        return STATUS_NEEDS_VALIDATION
    gates = report.get("gates") if isinstance(report.get("gates"), dict) else {}
    row = gates.get(gate) if isinstance(gates.get(gate), dict) else {}
    return STATUS_DONE if row.get("pass") else STATUS_NEEDS_VALIDATION


def smoke_status(report: dict[str, Any] | None) -> str:
    return STATUS_DONE if smoke_report_summary(report).get("pass") else STATUS_NEEDS_VALIDATION


def real_video_status(report: dict[str, Any] | None) -> str:
    return STATUS_IN_PROGRESS if smoke_status(report) == STATUS_DONE else STATUS_NEEDS_VALIDATION


def full_video_status(report: dict[str, Any] | None) -> str:
    return STATUS_DONE if video_report_summary(report).get("pass") else STATUS_NEEDS_VALIDATION


def batch_status(report: dict[str, Any] | None, background: dict[str, Any] | None = None) -> str:
    if background and background.get("status") == "running_or_unknown":
        return STATUS_IN_PROGRESS
    return STATUS_DONE if batch_report_summary(report).get("pass") else STATUS_NEEDS_VALIDATION


def checklist_items(
    platform_report: dict[str, Any] | None,
    smoke_report: dict[str, Any] | None,
    full_report: dict[str, Any] | None,
    batch_report: dict[str, Any] | None,
    batch_readiness: dict[str, Any] | None = None,
    batch_background: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    smoke = smoke_report_summary(smoke_report)
    full = video_report_summary(full_report)
    batch = batch_report_summary(batch_report)
    readiness = batch_readiness or {"available": False, "video_count": 0, "completed_count": 0, "pending_count": 0}
    background = batch_background or {"available": False, "status": "not_started"}
    smoke_evidence = (
        f"Latest smoke PASS: {smoke.get('name')} ({smoke.get('segment_count')} segments, "
        f"ASR={smoke.get('asr_backend')}, LLM={smoke.get('translation_backend')}, "
        f"TTS={smoke.get('tts_backend')}); report: {smoke.get('path')}"
        if smoke.get("pass")
        else "No passing real-video smoke report found in output_dir."
    )
    full_evidence = (
        f"Latest full lecture PASS: {full.get('name')} ({full.get('duration'):.1f}s, "
        f"{full.get('segment_count')} segments, ASR={full.get('asr_backend')}, "
        f"LLM={full.get('translation_backend')}, TTS={full.get('tts_backend')}); "
        f"report: {full.get('path')}"
        if full.get("pass")
        else "No passing full-lecture report found in output_dir."
    )
    if batch.get("pass"):
        batch_evidence = f"Latest batch PASS: {batch.get('total')} jobs, skipped={batch.get('skipped')}; report: {batch.get('path')}"
    elif background.get("status") == "running_or_unknown":
        batch_evidence = (
            f"Background batch chunk is running or awaiting completion marker: "
            f"{background.get('run_id')} pid={background.get('pid')} limit={background.get('limit')} "
            f"shortest_first={background.get('shortest_first')}; current completed videos: "
            f"{readiness.get('completed_count', 0)}/{readiness.get('video_count', 0)}, "
            f"pending={readiness.get('pending_count', 0)}."
        )
    else:
        batch_evidence = (
            f"No complete passing batch_report.json found; current completed videos: "
            f"{readiness.get('completed_count', 0)}/{readiness.get('video_count', 0)}, "
            f"pending={readiness.get('pending_count', 0)}, "
            f"last deferred={batch.get('deferred', 0)}."
            if readiness.get("available")
            else "No passing batch_report.json found in output_dir."
        )
    return [
        item(
            "core.scan",
            "Local Pipeline",
            "Scan lecture videos, existing subtitles, durations, resolution, and audio tracks without modifying originals.",
            STATUS_DONE,
            "Implemented in scan/audit CLI; original media stays outside managed output.",
            "Run: python -m ecse_localizer audit --input <video_root>",
        ),
        item(
            "core.asr",
            "Local Pipeline",
            "Prefer existing English subtitles and fall back to local ASR when captions are missing or insufficient.",
            STATUS_DONE,
            "Subtitle selection and local ASR backends are implemented; no cloud ASR is configured.",
            "Revalidate on a real lecture without subtitles.",
        ),
        item(
            "core.audio",
            "Local Pipeline",
            "Enhance weak/noisy audio locally before ASR and keep fallback behavior if enhancement hurts quality.",
            STATUS_DONE,
            "Audio enhancement and ffmpeg fallback path are present.",
            "Revalidate on a noisy real lecture sample.",
        ),
        item(
            "core.outputs",
            "Local Pipeline",
            "Generate en/zh/bilingual SRT/VTT/ASS, zh_dub.wav, zh_dub.mp4, optional hard-sub MP4, QA report, and glossary.",
            STATUS_DONE,
            "Subtitle, TTS, mux, QA, and report modules are wired into process-one/process-all.",
            "Run a fresh real-video smoke/full lecture after model/runtime changes.",
        ),
        item(
            "translation.best_quality",
            "Translation Quality",
            "Use paragraph reconstruction, style guide, literal translation, lecture rewrite, coherence pass, and fidelity repair.",
            gate_status(platform_report, "translation_sample"),
            "translation-sample gate covers literal/lecture/coherence/repair stages.",
            "Manually review representative real lecture segments.",
        ),
        item(
            "translation.protection",
            "Translation Quality",
            "Preserve formulas, code, variables, URLs, filenames, numbers, units, people, papers, acronyms, and model names.",
            STATUS_DONE,
            "Text protection and translation quality tests cover protected tokens and placeholders.",
            "Spot-check course-specific formulas and code snippets.",
        ),
        item(
            "tts.sync",
            "TTS And Timing",
            "Prevent overlapping dubbed audio, keep controlled end gaps, adjust speed safely, and keep volume audible.",
            STATUS_DONE,
            "TTS alignment, gap, trim, gain, and speed controls are implemented and exposed in templates.",
            "Listen to a real full lecture render for pacing and voice quality.",
        ),
        item(
            "tts.voice_policy",
            "TTS And Timing",
            "Avoid voice cloning unless explicit consent reference files are present.",
            STATUS_DONE,
            "Config defaults disallow unauthorized voice cloning.",
            "Confirm any future reference voice folder includes consent README before enabling cloning.",
        ),
        item(
            "webui.auth_users",
            "WebUI Platform",
            "Support login, invite-style users, admin create/disable, and per-user quota settings.",
            gate_status(platform_report, "webui_api_smoke"),
            "WebUI smoke creates a throwaway user and verifies authenticated API access.",
            "Click through the browser UI on the deployment target.",
        ),
        item(
            "webui.projects_templates",
            "WebUI Platform",
            "Support projects, folders, history, parameter templates, task submission, retry, delete, and restore.",
            gate_status(platform_report, "webui_api_smoke"),
            "WebUI smoke creates project/folder/template records and queues worker jobs.",
            "Use the UI to create a real project/folder/template set.",
        ),
        item(
            "webui.metrics_sse",
            "WebUI Platform",
            "Show live worker, queue, GPU/CPU/VRAM/disk, quota, and log-tail status with SSE plus polling fallback.",
            STATUS_DONE,
            "Metrics, /api/events, dashboard rail, and proxy no-buffering rules are implemented.",
            "Verify live updates through the real reverse proxy.",
        ),
        item(
            "worker.outbound",
            "Windows Worker",
            "Run Windows worker as an outbound-only service; Contabo never connects to a public Windows port.",
            gate_status(platform_report, "remote_smoke"),
            "remote-smoke and worker scripts cover heartbeat, claim, stale recovery, and local-only execution.",
            "Validate with the real reverse tunnel/VPN on Contabo.",
        ),
        item(
            "worker.lifecycle",
            "Windows Worker",
            "Support queued/claimed/running/paused/retrying/done/failed/cancelled/deleted states and remote cancellation.",
            gate_status(platform_report, "webui_api_smoke"),
            "Platform smoke verifies claim, cancel, control poll, and cancelled status update.",
            "Cancel a real long-running worker job from the browser UI.",
        ),
        item(
            "storage.quotas",
            "Storage And Quota",
            "Separate remote quota, local worker quota, project quota, active reservations, and worker disk free-space guard.",
            STATUS_DONE,
            "Quota APIs and job preflight checks include remote/local/project/reserved/committed fields.",
            "Tune production quota values before public use.",
        ),
        item(
            "storage.preview_cache",
            "Storage And Quota",
            "Keep full outputs on Windows; upload only low-bitrate previews/thumbnails and temporary requested full caches.",
            gate_status(platform_report, "webui_api_smoke"),
            "Platform smoke verifies artifact ref, request-cache, worker cache upload, and signed cached download.",
            "Verify preview playback and full download on real generated outputs.",
        ),
        item(
            "security.no_cloud",
            "Security",
            "Reject cloud inference APIs and public/private inference endpoints in remote deployment config.",
            gate_status(platform_report, "deploy_template_guard"),
            "deploy-check fails closed on cloud/non-local inference endpoint configuration.",
            "Run deploy-check on final Contabo config before exposing the service.",
        ),
        item(
            "security.worker_auth",
            "Security",
            "Use HMAC worker auth with timestamp/nonce replay protection for heartbeat/status/control/preview/cache calls.",
            STATUS_DONE,
            "Worker auth tests and platform smoke use signed worker requests.",
            "Rotate worker token in production if it is ever exposed.",
        ),
        item(
            "security.redaction",
            "Security",
            "Do not expose Windows paths, usernames, secrets, tokens, private IPs, command lines, or raw logs remotely.",
            STATUS_DONE,
            "Redaction is applied to worker metrics, media refs, log tails, command summaries, and public artifacts.",
            "Run secret scan before every commit and inspect public API responses in staging.",
        ),
        item(
            "deploy.package",
            "Contabo Deployment",
            "Provide deployment prompt, .env example, Docker Compose, proxy examples, systemd units, cleanup timer, and worker scripts.",
            gate_status(platform_report, "release_check"),
            "release-check verifies required deployment files and git safety rules.",
            "Run bootstrap on the real server with server-local secrets.",
        ),
        item(
            "deploy.real_contabo",
            "Contabo Deployment",
            "Validate the real Contabo web service, reverse proxy, tunnel/VPN, worker heartbeat, and browser workflows.",
            STATUS_NEEDS_VALIDATION,
            "Cannot be proven from this local workspace without server access.",
            "Deploy with DEPLOY_CONTABO_PROMPT.md, then run deploy-check, platform-check, worker-health, and browser workflow tests on the server.",
        ),
        item(
            "validation.smoke_90s",
            "Final Validation",
            "Run a 60-120 second real lecture smoke test and verify subtitles, dubbed audio, muxed MP4, report, and glossary outputs.",
            smoke_status(smoke_report),
            smoke_evidence,
            "If this regresses, rerun: .\\01_smoke_test.ps1 -Seconds 90",
        ),
        item(
            "validation.first_full_lecture",
            "Final Validation",
            "Process the first complete lecture end to end, then inspect subtitle timing, TTS overlap, voice clarity, QA report, and ffprobe readability.",
            full_video_status(full_report),
            full_evidence,
            "Run: .\\02_process_one.ps1, then review the generated report and first 10 subtitles.",
        ),
        item(
            "validation.batch_all",
            "Final Validation",
            "Process all detected lectures with resumable jobs and confirm failures are isolated, logged, and recoverable.",
            batch_status(batch_report, batch_background),
            batch_evidence,
            "Run in chunks: .\\15_manage_batch_chunk.ps1 -Action Start -Limit 1 -ShortestFirst, then .\\15_manage_batch_chunk.ps1 -Action Status.",
        ),
        item(
            "validation.real_video",
            "Final Validation",
            "Run latest code on a real lecture smoke/full render and inspect subtitles, TTS timing, voice clarity, muxed MP4, reports, and glossary.",
            STATUS_DONE if full.get("pass") else real_video_status(smoke_report),
            full_evidence if full.get("pass") else smoke_evidence if smoke.get("pass") else "Code gates pass, but current final acceptance still needs fresh real media output review.",
            "Run 01_smoke_test.ps1, then 02_process_one.ps1 on the first lecture, then inspect outputs.",
        ),
        item(
            "validation.visual_ui",
            "Final Validation",
            "Compare the WebUI visually against the intended deeeeepwiki/readlayer-style layout and fix any usability issues.",
            STATUS_NEEDS_VALIDATION,
            "Static UI exists; final visual calibration needs browser screenshots or reachable reference pages.",
            "Open the local/remote WebUI in a browser and capture desktop/mobile screenshots.",
        ),
    ]


def item(identifier: str, area: str, requirement: str, status: str, evidence: str, next_step: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "area": area,
        "requirement": requirement,
        "status": status,
        "evidence": evidence,
        "next_step": next_step,
        "subgoals": DETAILS_BY_ID.get(identifier, []),
    }


def expand_detailed_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    detailed: list[dict[str, Any]] = []
    for row in items:
        subgoals = row.get("subgoals") if isinstance(row.get("subgoals"), list) else []
        if not subgoals:
            detailed.append(
                {
                    "id": str(row.get("id") or ""),
                    "parent_id": str(row.get("id") or ""),
                    "area": str(row.get("area") or ""),
                    "requirement": str(row.get("requirement") or ""),
                    "status": str(row.get("status") or STATUS_PENDING),
                    "evidence": str(row.get("evidence") or ""),
                }
            )
            continue
        for index, subgoal in enumerate(subgoals, start=1):
            detailed.append(
                {
                    "id": f"{row.get('id')}.{index:02d}",
                    "parent_id": str(row.get("id") or ""),
                    "area": str(row.get("area") or ""),
                    "requirement": str(subgoal),
                    "status": str(row.get("status") or STATUS_PENDING),
                    "evidence": str(row.get("evidence") or ""),
                }
            )
    return detailed


def render_progress_markdown(checklist: dict[str, Any]) -> str:
    summary = checklist.get("summary", {})
    detail_summary = checklist.get("detail_summary", {})
    platform = checklist.get("latest_platform_check", {})
    smoke = checklist.get("latest_real_video_smoke", {})
    full = checklist.get("latest_full_lecture", {})
    batch = checklist.get("latest_batch_process", {})
    background = checklist.get("latest_batch_background", {})
    readiness = checklist.get("batch_readiness", {})
    lines = [
        "# AIALRA Local Video Localizer Progress Checklist",
        "",
        f"Generated: {checklist.get('generated_at', '')}",
        "",
        "## Summary",
        "",
        f"- High-level items: {summary.get('total', 0)}",
        f"- High-level done: {summary.get(STATUS_DONE, 0)}",
        f"- High-level in progress: {summary.get(STATUS_IN_PROGRESS, 0)}",
        f"- High-level needs real-world validation: {summary.get(STATUS_NEEDS_VALIDATION, 0)}",
        f"- High-level pending: {summary.get(STATUS_PENDING, 0)}",
        f"- Detailed acceptance items: {detail_summary.get('total', 0)}",
        f"- Detailed done: {detail_summary.get(STATUS_DONE, 0)}",
        f"- Detailed in progress: {detail_summary.get(STATUS_IN_PROGRESS, 0)}",
        f"- Detailed needs real-world validation: {detail_summary.get(STATUS_NEEDS_VALIDATION, 0)}",
        f"- Detailed pending: {detail_summary.get(STATUS_PENDING, 0)}",
        f"- Latest platform-check: {'PASS' if platform.get('pass') else 'not passing or unavailable'}",
        f"- Platform-check report: {platform.get('path') or 'not found'}",
        f"- Latest real-video smoke: {'PASS' if smoke.get('pass') else 'not passing or unavailable'}",
        f"- Smoke report: {smoke.get('path') or 'not found'}",
        f"- Latest full lecture: {'PASS' if full.get('pass') else 'not passing or unavailable'}",
        f"- Full lecture report: {full.get('path') or 'not found'}",
        f"- Latest batch process: {'PASS' if batch.get('pass') else 'not passing or unavailable'}",
        f"- Batch report: {batch.get('path') or 'not found'}",
        f"- Latest background batch: {background.get('status', 'not_started')} {background.get('run_id', '')}".rstrip(),
        f"- Batch readiness: {readiness.get('completed_count', 0)}/{readiness.get('video_count', 0)} videos complete; pending {readiness.get('pending_count', 0)}",
        "",
        "## Checklist",
        "",
    ]
    current_area = ""
    for row in checklist.get("items", []):
        area = str(row.get("area") or "Other")
        if area != current_area:
            current_area = area
            lines.extend([f"### {area}", ""])
        status = str(row.get("status") or STATUS_PENDING)
        mark = status_mark(status)
        lines.append(f"- {mark} `{row.get('id')}` {row.get('requirement')}")
        lines.append(f"  - Status: `{status}`")
        lines.append(f"  - Evidence: {row.get('evidence')}")
        lines.append(f"  - Next: {row.get('next_step')}")
        subgoals = row.get("subgoals") if isinstance(row.get("subgoals"), list) else []
        if subgoals:
            lines.append("  - Detailed acceptance:")
            for index, subgoal in enumerate(subgoals, start=1):
                lines.append(f"    - {mark} {index}. {subgoal}")
    return "\n".join(lines).rstrip() + "\n"


def status_mark(status: str) -> str:
    if status == STATUS_DONE:
        return "[x]"
    if status == STATUS_IN_PROGRESS:
        return "[~]"
    return "[ ]"
