from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import cleanup_expired_files
from .asr import transcribe_audio
from .audio_enhance import enhance_audio
from .capabilities import language_capabilities
from .compact import compact_rerender_from_report
from .config import load_config, privacy_guard
from .deploy_check import check_deploy_config
from .ffmpeg_utils import cut_video, extract_audio, media_duration
from .fidelity import run_fidelity_audit
from .glossary import GlossaryTerm, extract_from_title, extract_glossary, write_glossary_json, write_glossary_tsv
from .llm_local import LocalLLMClient
from .metrics import collect_system_metrics
from .mux import hardsub_video, mux_video
from .platform_store import PlatformStore
from .qa import run_qa
from .repair import repair_from_fidelity
from .report import write_audit_report, write_index_report, write_video_report
from .scan import audit_input, find_videos, select_existing_subtitle
from .subtitle_io import (
    Segment,
    bilingual_segments,
    normalize_segments,
    read_subtitles,
    split_long_segments,
    to_dicts,
    write_bilingual_ass,
    write_srt,
    write_vtt,
)
from .translate import translate_segments
from .tts import build_aligned_dub, tts_health
from .utils import PROJECT_ROOT, copy_text, ensure_dir, now_id, setup_logger, slugify, write_json
from .worker_client import collect_worker_media_refs, poll_loop, poll_once, post_worker_heartbeat
from .worker_health import assess_worker_health


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ecse_localizer")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("audit")
    p.add_argument("--input", required=True)

    p = sub.add_parser("smoke")
    p.add_argument("--input", required=True)
    p.add_argument("--seconds", type=int, default=90)

    p = sub.add_parser("process-one")
    p.add_argument("--video", required=True)

    p = sub.add_parser("process-all")
    p.add_argument("--input", required=True)
    p.add_argument("--force", action="store_true", help="Reprocess videos even when a passing report already exists.")

    p = sub.add_parser("resume")
    p.add_argument("--run-id", required=True)

    p = sub.add_parser("report")
    p.add_argument("--output", required=True)

    p = sub.add_parser("fidelity-audit")
    p.add_argument("--report", required=True)

    p = sub.add_parser("repair-fidelity")
    p.add_argument("--report", required=True)
    p.add_argument("--fidelity-report")
    p.add_argument("--max-score", type=int, default=3)
    p.add_argument("--skip-high", action="store_true")

    p = sub.add_parser("compact-rerender")
    p.add_argument("--report", required=True)
    p.add_argument("--run-dir")
    p.add_argument("--tag", default="final7")

    sub.add_parser("tts-health")
    p = sub.add_parser("worker-status")
    p.add_argument("--worker-id", default="local-windows-worker")
    p = sub.add_parser("worker-health")
    p.add_argument("--remote-base-url")
    p.add_argument("--worker-token")
    p.add_argument("--worker-id", default="local-windows-worker")
    p.add_argument("--skip-remote", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("cleanup")
    p.add_argument("--older-than-days", type=int, default=7)
    p.add_argument("--apply", action="store_true", help="Delete files. Without this flag cleanup is a dry run.")
    p = sub.add_parser("worker-poll")
    p.add_argument("--remote-base-url", required=True)
    p.add_argument("--worker-token", required=True)
    p.add_argument("--worker-id", default="local-windows-worker")
    p.add_argument("--interval-seconds", type=int, default=15)
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("deploy-check")
    p.add_argument("--mode", choices=["remote"], default="remote")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = load_config(args.config)
    try:
        if args.command not in {"deploy-check", "worker-health"}:
            privacy_guard(config)
        if args.command == "audit":
            return cmd_audit(args, config)
        if args.command == "smoke":
            return cmd_smoke(args, config)
        if args.command == "process-one":
            return cmd_process_one(args, config)
        if args.command == "process-all":
            return cmd_process_all(args, config)
        if args.command == "resume":
            return cmd_resume(args, config)
        if args.command == "report":
            return cmd_report(args, config)
        if args.command == "fidelity-audit":
            return cmd_fidelity_audit(args, config)
        if args.command == "repair-fidelity":
            return cmd_repair_fidelity(args, config)
        if args.command == "compact-rerender":
            return cmd_compact_rerender(args, config)
        if args.command == "tts-health":
            print(json.dumps(tts_health(config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "worker-status":
            return cmd_worker_status(args, config)
        if args.command == "worker-health":
            return cmd_worker_health(args, config)
        if args.command == "cleanup":
            return cmd_cleanup(args, config)
        if args.command == "worker-poll":
            return cmd_worker_poll(args, config)
        if args.command == "deploy-check":
            return cmd_deploy_check(args, config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 1


def cmd_audit(args: argparse.Namespace, config: dict[str, Any]) -> int:
    output_dir = ensure_dir(config["output_dir"])
    log_dir = ensure_dir(PROJECT_ROOT / "logs")
    logger = setup_logger("audit", log_dir / "audit.log")
    audit = audit_input(args.input, logger)
    audit["environment"] = environment_snapshot()
    write_audit_report(output_dir, audit)
    refresh_license_report(output_dir)
    print(json.dumps({"videos": audit["video_count"], "subtitles": audit["subtitle_count"], "report": str(output_dir / "audit_report.md")}, ensure_ascii=False, indent=2))
    return 0


def cmd_smoke(args: argparse.Namespace, config: dict[str, Any]) -> int:
    videos = find_videos(args.input)
    if not videos:
        raise RuntimeError(f"No videos found in {args.input}")
    video = videos[0]
    result = process_video(video, config, mode="smoke", seconds=max(60, min(120, args.seconds)), input_dir=args.input)
    print(json.dumps({"smoke": result["qa"]["pass"], "report": result["report_md"], "video": result["outputs"].get("zh_dub_mp4")}, ensure_ascii=False, indent=2))
    return 0 if result["qa"]["pass"] else 2


def cmd_process_one(args: argparse.Namespace, config: dict[str, Any]) -> int:
    result = process_video(Path(args.video), config, mode="full", input_dir=str(Path(args.video).parent))
    print(json.dumps({"pass": result["qa"]["pass"], "report": result["report_md"], "video": result["outputs"].get("zh_dub_mp4")}, ensure_ascii=False, indent=2))
    return 0 if result["qa"]["pass"] else 2


def cmd_process_all(args: argparse.Namespace, config: dict[str, Any]) -> int:
    output_dir = ensure_dir(config["output_dir"])
    log_dir = ensure_dir(PROJECT_ROOT / "logs")
    logger = setup_logger("process_all", log_dir / "process_all.log")
    results = []
    for video in find_videos(args.input):
        try:
            if not args.force:
                existing = completed_report_for(video, output_dir)
                if existing:
                    logger.info("Skipping already-passed video: %s", video)
                    results.append({"video": str(video), "pass": True, "report": str(existing), "skipped": True})
                    continue
            result = process_video(video, config, mode="full", input_dir=args.input)
            results.append({"video": str(video), "pass": result["qa"]["pass"], "report": result["report_md"]})
        except Exception as exc:
            logger.exception("Failed processing %s", video)
            results.append({"video": str(video), "pass": False, "error": str(exc)})
    write_json(output_dir / "batch_report.json", {"results": results})
    failed = [r for r in results if not r.get("pass")]
    skipped = [r for r in results if r.get("skipped")]
    print(
        json.dumps(
            {
                "processed": len(results) - len(skipped),
                "skipped": len(skipped),
                "failed": len(failed),
                "batch_report": str(output_dir / "batch_report.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failed else 2


def completed_report_for(video: Path, output_dir: Path) -> Path | None:
    canonical = output_dir / f"{slugify(video.name)}_report.json"
    candidates = sorted(
        {canonical, *output_dir.glob("*_report.json")},
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    seen: set[Path] = set()
    for report in candidates:
        if report in seen or not report.exists():
            continue
        seen.add(report)
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except Exception:
            continue
        source_raw = str(data.get("source_video", ""))
        if source_raw:
            if Path(source_raw).resolve() != video.resolve():
                continue
        elif report != canonical:
            continue
        if not data.get("qa", {}).get("pass"):
            continue
        outputs = data.get("outputs", {})
        required = ["en_srt", "zh_srt", "bilingual_srt", "bilingual_ass", "zh_dub_wav", "zh_dub_mp4"]
        if not all(outputs.get(k) and Path(outputs[k]).exists() for k in required):
            continue
        return report
    return None


def cmd_resume(args: argparse.Namespace, config: dict[str, Any]) -> int:
    state = Path(config["work_dir"]) / args.run_id / "state.json"
    if not state.exists():
        raise RuntimeError(f"Run state not found: {state}")
    data = json.loads(state.read_text(encoding="utf-8"))
    result = process_video(Path(data["source_video"]), config, mode=data.get("mode", "full"), seconds=data.get("seconds"), input_dir=data.get("input_dir"))
    print(json.dumps({"pass": result["qa"]["pass"], "report": result["report_md"]}, ensure_ascii=False, indent=2))
    return 0 if result["qa"]["pass"] else 2


def cmd_report(args: argparse.Namespace, config: dict[str, Any]) -> int:
    index = write_index_report(args.output)
    print(json.dumps({"index": str(index), "tts": tts_health(config)}, ensure_ascii=False, indent=2))
    return 0


def build_worker_status_payload(config: dict[str, Any], *, worker_id: str = "local-windows-worker") -> dict[str, Any]:
    store = PlatformStore(config)
    store.bootstrap()
    llm = LocalLLMClient(config).status()
    tts = tts_health(config)
    return {
        "worker_id": worker_id,
        "version": __version__,
        "privacy": config.get("privacy", {}),
        "metrics": collect_system_metrics(config),
        "media_refs": collect_worker_media_refs(config),
        "tts": tts,
        "llm": llm.__dict__,
        "capabilities": language_capabilities(config, llm_status=llm, tts_status=tts),
        "worker": store.worker_status(),
    }


def cmd_worker_status(args: argparse.Namespace, config: dict[str, Any]) -> int:
    payload = build_worker_status_payload(config, worker_id=args.worker_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_worker_health(args: argparse.Namespace, config: dict[str, Any]) -> int:
    payload = build_worker_status_payload(config, worker_id=args.worker_id)
    remote_checked = False
    remote_ok = False
    remote_error_type = ""
    if not args.skip_remote and (args.remote_base_url or args.worker_token):
        remote_checked = True
        if not args.remote_base_url or not args.worker_token:
            remote_error_type = "MissingRemoteBaseUrlOrWorkerToken"
        else:
            try:
                response = post_worker_heartbeat(args.remote_base_url, args.worker_token, payload)
                remote_ok = bool(response.get("ok", True))
            except Exception as exc:
                remote_error_type = type(exc).__name__
    result = assess_worker_health(payload, remote_checked=remote_checked, remote_ok=remote_ok, remote_error_type=remote_error_type)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["pass"] else "FAIL"
        remote = result["remote"]
        remote_label = "skipped" if not remote["checked"] else "ok" if remote["ok"] else "failed"
        print(f"{status}: {result['errors']} error(s), {result['warnings']} warning(s), remote heartbeat {remote_label}")
        for finding in result["findings"]:
            level = finding["level"].upper()
            print(f"{level} [{finding['code']}] {finding['path']}: {finding['message']}")
    return 0 if result["pass"] else 2


def cmd_deploy_check(args: argparse.Namespace, config: dict[str, Any]) -> int:
    result = check_deploy_config(config, mode=args.mode)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["pass"] else "FAIL"
        print(f"{status}: {result['errors']} error(s), {result['warnings']} warning(s)")
        for finding in result["findings"]:
            level = finding["level"].upper()
            print(f"{level} [{finding['code']}] {finding['path']}: {finding['message']}")
    return 0 if result["pass"] else 2


def cmd_cleanup(args: argparse.Namespace, config: dict[str, Any]) -> int:
    result = cleanup_expired_files(config, older_than_days=args.older_than_days, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_worker_poll(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if args.once or args.dry_run:
        result = poll_once(
            remote_base_url=args.remote_base_url,
            worker_token=args.worker_token,
            worker_id=args.worker_id,
            config=config,
            config_path=args.config,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    poll_loop(
        remote_base_url=args.remote_base_url,
        worker_token=args.worker_token,
        worker_id=args.worker_id,
        config=config,
        config_path=args.config,
        interval_seconds=args.interval_seconds,
    )
    return 0


def cmd_fidelity_audit(args: argparse.Namespace, config: dict[str, Any]) -> int:
    log_dir = ensure_dir(PROJECT_ROOT / "logs")
    logger = setup_logger("fidelity_audit", log_dir / "fidelity_audit.log")
    audit = run_fidelity_audit(args.report, config, logger=logger)
    print(
        json.dumps(
            {
                "pass": audit["pass"],
                "model": audit["model"],
                "reviewed": audit["reviewed_count"],
                "segments": audit["segment_count"],
                "average_score": audit["average_score"],
                "high_issues": len([i for i in audit["issues"] if i.get("severity") == "high"]),
                "report": str(Path(args.report).with_name(Path(args.report).stem.replace("_report", "") + "_fidelity_report.md")),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if audit["pass"] else 2


def cmd_repair_fidelity(args: argparse.Namespace, config: dict[str, Any]) -> int:
    log_dir = ensure_dir(PROJECT_ROOT / "logs")
    logger = setup_logger("repair_fidelity", log_dir / "repair_fidelity.log")
    result = repair_from_fidelity(
        args.report,
        args.fidelity_report,
        config,
        max_score=args.max_score,
        include_high=not args.skip_high,
        logger=logger,
    )
    print(
        json.dumps(
            {
                "pass": result["qa"]["pass"],
                "repairs": len(result.get("repairs", [])),
                "report": result["report_md"],
                "video": result["outputs"].get("zh_dub_mp4"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["qa"]["pass"] else 2


def cmd_compact_rerender(args: argparse.Namespace, config: dict[str, Any]) -> int:
    log_dir = ensure_dir(PROJECT_ROOT / "logs")
    logger = setup_logger("compact_rerender", log_dir / "compact_rerender.log")
    result = compact_rerender_from_report(args.report, config, tag=args.tag, run_dir=args.run_dir, logger=logger)
    print(
        json.dumps(
            {
                "pass": result["qa"]["pass"],
                "report": result["report_md"],
                "video": result["outputs"].get("zh_dub_mp4"),
                "hard_sub": result["outputs"].get("zh_dub_bilingual_hardsub_mp4"),
                "compact_stats": result["tts"].get("compact_stats", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["qa"]["pass"] else 2


def process_video(
    video: Path,
    config: dict[str, Any],
    *,
    mode: str,
    seconds: int | None = None,
    input_dir: str | None = None,
) -> dict[str, Any]:
    output_dir = ensure_dir(config["output_dir"])
    work_root = ensure_dir(config["work_dir"])
    run_id = now_id(slugify(video.name, 44))
    run_dir = ensure_dir(work_root / run_id)
    log_dir = ensure_dir(PROJECT_ROOT / "logs")
    logger = setup_logger(run_id, log_dir / f"{run_id}.log")
    state = {"run_id": run_id, "source_video": str(video), "mode": mode, "seconds": seconds, "input_dir": input_dir}
    write_json(run_dir / "state.json", state)

    base = slugify(video.name)
    if mode == "smoke":
        base = f"{base}_smoke_{seconds}s"
        work_video = run_dir / f"{base}.mp4"
        if not work_video.exists():
            cut_video(video, work_video, int(seconds or 90), logger)
        process_duration = media_duration(work_video)
    else:
        work_video = video
        process_duration = media_duration(video)

    raw_audio = run_dir / f"{base}_raw.wav"
    enhanced_audio = run_dir / f"{base}_enhanced.wav"
    extract_audio(work_video, raw_audio, logger)
    enhancement_backend = enhance_audio(raw_audio, enhanced_audio, config, logger=logger)

    subtitle_source = select_existing_subtitle(video, media_duration(video))
    asr_backend = "existing_subtitle"
    if subtitle_source:
        en_segments = read_subtitles(subtitle_source)
        en_segments = normalize_segments(en_segments, max_end=process_duration if mode == "smoke" else None)
        en_segments = split_long_segments(en_segments)
        logger.info("Using existing subtitles: %s (%d segments)", subtitle_source, len(en_segments))
    else:
        en_segments, asr_backend = transcribe_audio(enhanced_audio, config, logger)
        en_segments = normalize_segments(en_segments, max_end=process_duration if mode == "smoke" else None)
        en_segments = split_long_segments(en_segments)
        subtitle_source = Path("ASR")
    if not en_segments:
        raise RuntimeError(f"No usable English subtitle/ASR segments for {video}")

    glossary = build_course_glossary(input_dir or str(video.parent), output_dir, logger)
    glossary = extract_from_title(video.stem, video.name, glossary)
    glossary = extract_glossary(en_segments, video.name, glossary)
    write_glossary_tsv(output_dir / "glossary.tsv", glossary)
    write_glossary_json(output_dir / "glossary.json", glossary)

    en_srt = output_dir / f"{base}_en.srt"
    en_vtt = output_dir / f"{base}_en.vtt"
    write_srt(en_srt, en_segments)
    write_vtt(en_vtt, en_segments)

    trace_path = run_dir / f"{base}_translation_trace.json"
    zh_segments, traces, translation_backend = translate_segments(en_segments, glossary, config, trace_path, logger)
    zh_srt = output_dir / f"{base}_zh.srt"
    zh_vtt = output_dir / f"{base}_zh.vtt"
    write_srt(zh_srt, zh_segments, cjk=True, line_limit=int(config["translation"]["max_zh_chars_per_subtitle_line"]))
    write_vtt(zh_vtt, zh_segments, cjk=True, line_limit=int(config["translation"]["max_zh_chars_per_subtitle_line"]))

    bilingual_srt = output_dir / f"{base}_bilingual.srt"
    bilingual_ass = output_dir / f"{base}_bilingual.ass"
    write_srt(bilingual_srt, bilingual_segments(en_segments, zh_segments), cjk=False)
    write_bilingual_ass(bilingual_ass, en_segments, zh_segments)

    zh_wav = output_dir / f"{base}_zh_dub.wav"
    tts_info = build_aligned_dub(zh_segments, process_duration, zh_wav, run_dir, config, source_video=work_video, logger=logger)

    zh_mp4 = output_dir / f"{base}_zh_dub.mp4"
    mux_video(work_video, zh_wav, zh_mp4, config, logger)

    hard_mp4 = output_dir / f"{base}_zh_dub_bilingual_hardsub.mp4"
    hard_ok = False
    if config.get("mux", {}).get("hard_subtitle", True):
        hard_ok = hardsub_video(zh_mp4, bilingual_ass, hard_mp4, logger)

    outputs = {
        "en_srt": str(en_srt),
        "en_vtt": str(en_vtt),
        "zh_srt": str(zh_srt),
        "zh_vtt": str(zh_vtt),
        "bilingual_srt": str(bilingual_srt),
        "bilingual_ass": str(bilingual_ass),
        "zh_dub_wav": str(zh_wav),
        "zh_dub_mp4": str(zh_mp4),
    }
    if hard_ok:
        outputs["zh_dub_bilingual_hardsub_mp4"] = str(hard_mp4)

    qa = run_qa(outputs, en_segments, zh_segments, glossary, traces, tts_info, process_duration, config)
    report_md = output_dir / f"{base}_report.md"
    report_json = output_dir / f"{base}_report.json"
    result = {
        "name": base,
        "run_id": run_id,
        "mode": mode,
        "source_video": str(video),
        "work_video": str(work_video),
        "subtitle_source": str(subtitle_source),
        "asr_backend": asr_backend,
        "translation_backend": translation_backend,
        "audio_enhancement": enhancement_backend,
        "tts": tts_info,
        "outputs": outputs,
        "qa": qa,
        "report_md": str(report_md),
        "report_json": str(report_json),
        "segments": {"en": to_dicts(en_segments), "zh": to_dicts(zh_segments)},
    }
    write_video_report(report_md, report_json, result)
    write_json(run_dir / "result.json", result)
    refresh_license_report(output_dir)
    return result


def build_course_glossary(input_dir: str, output_dir: Path, logger=None) -> dict[str, GlossaryTerm]:
    terms: dict[str, GlossaryTerm] = {}
    for video in find_videos(input_dir):
        try:
            terms = extract_from_title(video.stem, video.name, terms)
            duration = media_duration(video)
            sub = select_existing_subtitle(video, duration)
            if sub:
                segments = read_subtitles(sub)
                terms = extract_glossary(segments, video.name, terms)
        except Exception as exc:
            if logger:
                logger.warning("Glossary extraction skipped for %s: %s", video, exc)
    write_glossary_tsv(output_dir / "glossary.tsv", terms)
    write_glossary_json(output_dir / "glossary.json", terms)
    return terms


def environment_snapshot() -> dict[str, Any]:
    import subprocess

    def capture(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout.strip()
        except Exception as exc:
            return f"unavailable: {exc}"

    return {
        "python": sys.version,
        "ffmpeg": capture(["ffmpeg", "-version"]).splitlines()[0] if capture(["ffmpeg", "-version"]) else "",
        "ffprobe": capture(["ffprobe", "-version"]).splitlines()[0] if capture(["ffprobe", "-version"]) else "",
        "git": capture(["git", "--version"]),
        "nvidia_smi": capture(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]),
        "tts": tts_health(load_config()),
    }


def refresh_license_report(output_dir: Path) -> None:
    src = PROJECT_ROOT / "licenses_report.md"
    if src.exists():
        copy_text(src, output_dir / "licenses_report.md")


if __name__ == "__main__":
    raise SystemExit(main())
