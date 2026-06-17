from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ffmpeg_utils import audio_duration, media_duration
from .mux import hardsub_video, mux_video
from .qa import run_qa
from .repair import load_glossary
from .report import write_video_report
from .subtitle_io import (
    Segment,
    bilingual_segments,
    normalize_segments,
    to_dicts,
    write_bilingual_ass,
    write_srt,
    write_vtt,
)
from .translate import TranslationTrace, target_limit
from .tts import (
    TTSUnit,
    clip_filter_for_backend,
    final_audio_filter_for_speaker,
    make_tts_units,
    overlay_pcm,
    spoken_text_len,
    synthesize_cosyvoice_batch,
    write_pcm,
)
from .speaker_gender import resolve_tts_speaker
from .utils import ensure_dir, now_id, read_json, run_cmd, slugify, write_json


@dataclass
class ScheduledUnit:
    unit: TTSUnit
    pcm: Path
    duration: float
    original_start: float
    scheduled_start: float
    scheduled_end: float
    advance: float


def compact_rerender_from_report(
    report_json: str | Path,
    config: dict[str, Any],
    *,
    tag: str = "final7",
    run_dir: str | Path | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    report_path = Path(report_json)
    report = read_json(report_path)
    source_run = Path(run_dir) if run_dir else Path(config["work_dir"]) / str(report["run_id"])
    en_segments = [Segment(int(x["id"]), float(x["start"]), float(x["end"]), str(x["text"])) for x in report["segments"]["en"]]
    zh_segments = [Segment(int(x["id"]), float(x["start"]), float(x["end"]), str(x["text"])) for x in report["segments"]["zh"]]
    units = make_tts_units(zh_segments, config)

    output_dir = ensure_dir(config["output_dir"])
    work_video = Path(report.get("work_video") or report.get("source_video"))
    video_duration = media_duration(work_video)
    base = final_tag_name(str(report.get("name", report_path.stem.replace("_report", ""))), tag)
    compact_run = ensure_dir(Path(config["work_dir"]) / now_id(slugify(base, 44) + "_compact"))

    source_tts_dir = source_run / "tts_segments"
    if not source_tts_dir.exists():
        raise RuntimeError(f"TTS cache not found: {source_tts_dir}")
    speaker_info = resolve_tts_speaker(work_video, compact_run, config, logger)
    tts_flags: list[dict[str, Any]] = []
    if config.get("tts", {}).get("compact_resynthesize_tts", False):
        tts_dir = ensure_dir(compact_run / "tts_segments")
        tts_flags = synthesize_compact_pcm(units, tts_dir, config, speaker_info, logger)
    else:
        tts_dir = source_tts_dir

    scheduled = schedule_compact_units(units, tts_dir, video_duration, config)
    compact_en, compact_zh = retime_segments_to_schedule(en_segments, zh_segments, scheduled, video_duration, config)

    en_srt = output_dir / f"{base}_en.srt"
    en_vtt = output_dir / f"{base}_en.vtt"
    zh_srt = output_dir / f"{base}_zh.srt"
    zh_vtt = output_dir / f"{base}_zh.vtt"
    bilingual_srt = output_dir / f"{base}_bilingual.srt"
    bilingual_ass = output_dir / f"{base}_bilingual.ass"
    zh_wav = output_dir / f"{base}_zh_dub.wav"
    zh_mp4 = output_dir / f"{base}_zh_dub.mp4"
    hard_mp4 = output_dir / f"{base}_zh_dub_bilingual_hardsub.mp4"

    line_limit = int(config["translation"]["max_zh_chars_per_subtitle_line"])
    write_srt(en_srt, compact_en)
    write_vtt(en_vtt, compact_en)
    write_srt(zh_srt, compact_zh, cjk=True, line_limit=line_limit)
    write_vtt(zh_vtt, compact_zh, cjk=True, line_limit=line_limit)
    write_srt(bilingual_srt, bilingual_segments(compact_en, compact_zh), cjk=False)
    write_bilingual_ass(bilingual_ass, compact_en, compact_zh)

    tts_info = build_compact_dub(scheduled, video_duration, zh_wav, config, speaker_info=speaker_info, logger=logger)
    tts_info["flags"] = tts_flags
    tts_info["speaker"] = speaker_info.get("speaker")
    tts_info["speaker_gender"] = speaker_info
    tts_info["source_run_id"] = source_run.name
    tts_info["source_report"] = str(report_path)
    tts_info["compact_stats"] = compact_stats(scheduled)
    tts_info["source_overrun_count"] = count_source_overruns(report)

    mux_video(work_video, zh_wav, zh_mp4, config, logger)
    hard_ok = hardsub_video(zh_mp4, bilingual_ass, hard_mp4, logger) if config.get("mux", {}).get("hard_subtitle", True) else False

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

    glossary = load_glossary(Path(config["output_dir"]) / "glossary.json")
    traces = [
        TranslationTrace(
            segment_id=en.id,
            original_text=en.text,
            zh_literal=zh.text,
            zh_lecture=zh.text,
            duration=en.duration,
            target_char_limit=target_limit(en, config),
            flags=[],
        )
        for en, zh in zip(compact_en, compact_zh)
    ]
    qa = run_qa(outputs, compact_en, compact_zh, glossary, traces, tts_info, video_duration, config)

    result = {
        "name": base,
        "run_id": compact_run.name,
        "mode": "compact_timeline_rerender",
        "source_video": report.get("source_video"),
        "work_video": str(work_video),
        "source_report": str(report_path),
        "source_run_id": source_run.name,
        "subtitle_source": report.get("subtitle_source"),
        "asr_backend": report.get("asr_backend"),
        "translation_backend": report.get("translation_backend"),
        "audio_enhancement": report.get("audio_enhancement"),
        "tts": tts_info,
        "outputs": outputs,
        "qa": qa,
        "report_md": str(output_dir / f"{base}_report.md"),
        "report_json": str(output_dir / f"{base}_report.json"),
        "segments": {"en": to_dicts(compact_en), "zh": to_dicts(compact_zh)},
    }
    write_json(compact_run / "result.json", result)
    write_json(compact_run / "schedule.json", {"units": [schedule_to_dict(x) for x in scheduled]})
    write_video_report(result["report_md"], result["report_json"], result)
    return result


def final_tag_name(name: str, tag: str) -> str:
    for old in ("final10", "final9", "final8", "final7", "final6", "final5", "final4", "final3", "final2", "final1"):
        if old in name:
            return name.replace(old, tag)
    return f"{name}_{tag}"


def synthesize_compact_pcm(
    units: list[TTSUnit],
    tts_dir: Path,
    config: dict[str, Any],
    speaker_info: dict[str, Any],
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    tts = config.get("tts", {})
    flags: list[dict[str, Any]] = []
    synthesize_cosyvoice_batch(units, tts_dir, config, speaker=speaker_info.get("speaker"), logger=logger)
    end_gap = float(tts.get("end_gap_seconds", 0.2))
    min_fill = float(tts.get("min_fill_ratio", 0.70))
    target_fill = float(tts.get("target_fill_ratio", 0.94))
    min_slow_factor = float(tts.get("min_slow_factor", 0.78))
    max_speed = float(tts.get("max_speed", 1.25))
    absolute_max = float(tts.get("absolute_max_speed", 1.35))

    for unit in units:
        raw_clip = tts_dir / f"seg_{unit.id:05d}_cosy.wav"
        if not raw_clip.exists():
            raise RuntimeError(f"CosyVoice clip missing for unit {unit.id}: {raw_clip}")
        clip = raw_clip
        clip_dur = audio_duration(clip)
        target = max(0.25, unit.duration - end_gap)
        if clip_dur < target * min_fill:
            desired_duration = max(clip_dur, target * target_fill)
            factor = max(min_slow_factor, min(0.999, clip_dur / max(desired_duration, 0.01)))
            if factor < 0.999:
                stretched = tts_dir / f"seg_{unit.id:05d}_slow.wav"
                run_cmd(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-nostdin",
                        "-nostats",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(clip),
                        "-filter:a",
                        f"atempo={factor:.3f}",
                        str(stretched),
                    ],
                    logger=logger,
                )
                flags.append(
                    {
                        "segment_id": unit.id,
                        "segment_ids": unit.segment_ids,
                        "type": "tts_short_stretched",
                        "original_duration": round(clip_dur, 3),
                        "target_duration": round(target, 3),
                        "applied_atempo": round(factor, 3),
                    }
                )
                clip = stretched
                clip_dur = audio_duration(clip)
        if clip_dur > target * 1.04:
            needed = clip_dur / max(target, 0.01)
            factor = min(needed, absolute_max)
            if needed > max_speed:
                flags.append(
                    {
                        "segment_id": unit.id,
                        "segment_ids": unit.segment_ids,
                        "type": "tts_overrun",
                        "needed_speed": round(needed, 3),
                        "applied_speed": round(factor, 3),
                        "max_speed": max_speed,
                        "absolute_max_speed": absolute_max,
                    }
                )
            compressed = tts_dir / f"seg_{unit.id:05d}_speed.wav"
            run_cmd(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-nostats",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(clip),
                    "-filter:a",
                    f"atempo={factor:.3f}",
                    str(compressed),
                ],
                logger=logger,
            )
            clip = compressed

        pcm = tts_dir / f"seg_{unit.id:05d}_pcm.wav"
        run_cmd(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-nostats",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(clip),
                "-af",
                clip_filter_for_backend("cosyvoice_sft", config),
                "-ac",
                "1",
                "-ar",
                "22050",
                "-sample_fmt",
                "s16",
                str(pcm),
            ],
            logger=logger,
        )
    return flags


def schedule_compact_units(
    units: list[TTSUnit],
    tts_dir: Path,
    video_duration: float,
    config: dict[str, Any],
) -> list[ScheduledUnit]:
    cfg = config.get("tts", {})
    mode = str(cfg.get("compact_schedule_mode", "bounded_distributed"))
    if mode == "source_anchored":
        scheduled = schedule_source_anchored_units(units, tts_dir, video_duration, config)
    elif mode == "bounded_distributed":
        scheduled = schedule_bounded_distributed_units(units, tts_dir, video_duration, config)
    else:
        scheduled = schedule_gap_limited_units(units, tts_dir, video_duration, config)
    return prevent_scheduled_audio_overlap(scheduled, video_duration, config)


def schedule_gap_limited_units(
    units: list[TTSUnit],
    tts_dir: Path,
    video_duration: float,
    config: dict[str, Any],
) -> list[ScheduledUnit]:
    cfg = config.get("tts", {})
    min_gap = float(cfg.get("compact_min_gap_seconds", 0.22))
    max_gap = float(cfg.get("compact_max_gap_seconds", 0.70))
    max_advance = float(cfg.get("compact_max_advance_seconds", 8.0))
    scheduled: list[ScheduledUnit] = []
    previous_end: float | None = None
    for unit in units:
        pcm = tts_dir / f"seg_{unit.id:05d}_pcm.wav"
        if not pcm.exists():
            raise RuntimeError(f"Missing PCM TTS clip for unit {unit.id}: {pcm}")
        duration = audio_duration(pcm)
        start = unit.start
        if previous_end is not None:
            min_start = previous_end + min_gap
            if start - previous_end > max_gap:
                start = max(min_start, unit.start - max_advance)
            elif start < min_start:
                start = min_start
        start = max(0.0, min(start, max(0.0, video_duration - duration)))
        if previous_end is not None and start < previous_end + min_gap:
            start = previous_end + min_gap
        end = min(video_duration, start + duration)
        scheduled.append(
            ScheduledUnit(
                unit=unit,
                pcm=pcm,
                duration=max(0.0, end - start),
                original_start=unit.start,
                scheduled_start=start,
                scheduled_end=end,
                advance=max(0.0, unit.start - start),
            )
        )
        previous_end = end
    return scheduled


def schedule_source_anchored_units(
    units: list[TTSUnit],
    tts_dir: Path,
    video_duration: float,
    config: dict[str, Any],
) -> list[ScheduledUnit]:
    scheduled: list[ScheduledUnit] = []
    for unit in units:
        pcm = tts_dir / f"seg_{unit.id:05d}_pcm.wav"
        if not pcm.exists():
            raise RuntimeError(f"Missing PCM TTS clip for unit {unit.id}: {pcm}")
        duration = audio_duration(pcm)
        start = max(0.0, min(unit.start, max(0.0, video_duration - duration)))
        end = min(video_duration, start + duration)
        scheduled.append(
            ScheduledUnit(
                unit=unit,
                pcm=pcm,
                duration=max(0.0, end - start),
                original_start=unit.start,
                scheduled_start=start,
                scheduled_end=end,
                advance=max(0.0, unit.start - start),
            )
        )
    return scheduled


def schedule_bounded_distributed_units(
    units: list[TTSUnit],
    tts_dir: Path,
    video_duration: float,
    config: dict[str, Any],
) -> list[ScheduledUnit]:
    cfg = config.get("tts", {})
    min_gap = float(cfg.get("compact_min_gap_seconds", 0.22))
    max_gap = float(cfg.get("compact_distributed_max_gap_seconds", 4.0))
    lead_in = float(cfg.get("compact_lead_in_seconds", 0.0))
    clips: list[tuple[TTSUnit, Path, float]] = []
    for unit in units:
        pcm = tts_dir / f"seg_{unit.id:05d}_pcm.wav"
        if not pcm.exists():
            raise RuntimeError(f"Missing PCM TTS clip for unit {unit.id}: {pcm}")
        clips.append((unit, pcm, audio_duration(pcm)))

    if not clips:
        return []
    gap_count = max(0, len(clips) - 1)
    speech_total = sum(duration for _, _, duration in clips)
    total_gap = max(0.0, video_duration - lead_in - speech_total)
    if gap_count == 0:
        gaps: list[float] = []
    elif total_gap <= min_gap * gap_count:
        gaps = [total_gap / gap_count] * gap_count
    else:
        base = [min_gap] * gap_count
        extra = total_gap - min_gap * gap_count
        caps = [max(0.0, max_gap - min_gap)] * gap_count
        original_gaps = [
            max(0.0, clips[i + 1][0].start - clips[i][0].end)
            for i in range(gap_count)
        ]
        weights = [gap + 0.35 for gap in original_gaps]
        extras = distribute_capped(extra, weights, caps)
        gaps = [base_gap + extra_gap for base_gap, extra_gap in zip(base, extras)]

    scheduled: list[ScheduledUnit] = []
    cursor = max(0.0, lead_in)
    for idx, (unit, pcm, duration) in enumerate(clips):
        start = min(cursor, max(0.0, video_duration - duration))
        end = min(video_duration, start + duration)
        scheduled.append(
            ScheduledUnit(
                unit=unit,
                pcm=pcm,
                duration=max(0.0, end - start),
                original_start=unit.start,
                scheduled_start=start,
                scheduled_end=end,
                advance=max(0.0, unit.start - start),
            )
        )
        cursor = end + (gaps[idx] if idx < len(gaps) else 0.0)
    return scheduled


def prevent_scheduled_audio_overlap(
    scheduled: list[ScheduledUnit],
    video_duration: float,
    config: dict[str, Any],
) -> list[ScheduledUnit]:
    cfg = config.get("tts", {})
    if not cfg.get("prevent_audio_overlap", True):
        return scheduled
    min_gap = float(cfg.get("min_audio_gap_seconds", cfg.get("compact_min_gap_seconds", 0.08)))
    out: list[ScheduledUnit] = []
    previous_end: float | None = None
    for item in scheduled:
        start = item.scheduled_start
        if previous_end is not None:
            start = max(start, previous_end + min_gap)
        start = max(0.0, min(start, video_duration))
        end = min(video_duration, start + item.duration)
        out.append(
            ScheduledUnit(
                unit=item.unit,
                pcm=item.pcm,
                duration=max(0.0, end - start),
                original_start=item.original_start,
                scheduled_start=start,
                scheduled_end=end,
                advance=max(0.0, item.original_start - start),
            )
        )
        previous_end = end
    return out


def distribute_capped(total: float, weights: list[float], caps: list[float]) -> list[float]:
    extras = [0.0] * len(weights)
    remaining = max(0.0, total)
    active = {i for i, cap in enumerate(caps) if cap > 0}
    while remaining > 1e-6 and active:
        weight_sum = sum(max(0.0, weights[i]) for i in active) or float(len(active))
        changed = False
        for i in list(active):
            share = remaining * ((max(0.0, weights[i]) or 1.0) / weight_sum)
            room = caps[i] - extras[i]
            add = min(room, share)
            if add > 0:
                extras[i] += add
                changed = True
            if extras[i] >= caps[i] - 1e-6:
                active.remove(i)
        used = sum(extras)
        remaining = max(0.0, total - used)
        if not changed:
            break
    return extras


def retime_segments_to_schedule(
    en_segments: list[Segment],
    zh_segments: list[Segment],
    scheduled: list[ScheduledUnit],
    video_duration: float,
    config: dict[str, Any],
) -> tuple[list[Segment], list[Segment]]:
    by_en = {seg.id: seg for seg in en_segments}
    by_zh = {seg.id: seg for seg in zh_segments}
    out_en: list[Segment] = []
    out_zh: list[Segment] = []
    hold = float(config.get("tts", {}).get("compact_subtitle_hold_seconds", 0.18))
    internal_gap = float(config.get("tts", {}).get("compact_subtitle_gap_seconds", 0.04))
    for idx, item in enumerate(scheduled):
        next_start = scheduled[idx + 1].scheduled_start if idx + 1 < len(scheduled) else video_duration
        latest_end = max(item.scheduled_start + 0.05, next_start - max(0.02, internal_gap))
        unit_end = min(video_duration, item.scheduled_end + hold, latest_end)
        ids = [sid for sid in item.unit.segment_ids if sid in by_en and sid in by_zh]
        if not ids:
            continue
        available = max(0.08, unit_end - item.scheduled_start - internal_gap * max(0, len(ids) - 1))
        weights = [max(1.0, float(spoken_text_len(by_zh[sid].text))) for sid in ids]
        total = sum(weights) or 1.0
        cursor = item.scheduled_start
        for pos, sid in enumerate(ids):
            dur = max(0.08, available * weights[pos] / total)
            end = cursor + dur
            if pos == len(ids) - 1:
                end = unit_end
            end = min(video_duration, max(cursor + 0.05, end))
            out_en.append(Segment(sid, cursor, end, by_en[sid].text))
            out_zh.append(Segment(sid, cursor, end, by_zh[sid].text))
            cursor = end + internal_gap
    return normalize_segments(out_en, max_end=video_duration), normalize_segments(out_zh, max_end=video_duration)


def build_compact_dub(
    scheduled: list[ScheduledUnit],
    video_duration: float,
    out_wav: str | Path,
    config: dict[str, Any],
    speaker_info: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    sample_rate = 22050
    total_samples = int((video_duration + 0.25) * sample_rate)
    mix = array("h", [0]) * total_samples
    for item in scheduled:
        overlay_pcm(mix, item.pcm, int(item.scheduled_start * sample_rate))
    final_out = Path(out_wav)
    raw_mix = final_out.with_name(final_out.stem + "_rawmix.wav")
    write_pcm(raw_mix, mix, sample_rate)
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw_mix),
            "-af",
            final_audio_filter_for_speaker(config, speaker_info),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-sample_fmt",
            "s16",
            str(final_out),
        ],
        logger=logger,
    )
    return {
        "backend": "cosyvoice_sft",
        "duration": audio_duration(final_out),
        "segment_count": sum(len(x.unit.segment_ids) for x in scheduled),
        "utterance_count": len(scheduled),
        "alignment_mode": str(config.get("tts", {}).get("compact_schedule_mode", "bounded_distributed")),
        "flags": [],
    }


def compact_stats(scheduled: list[ScheduledUnit]) -> dict[str, Any]:
    gaps = [
        max(0.0, scheduled[i].scheduled_start - scheduled[i - 1].scheduled_end)
        for i in range(1, len(scheduled))
    ]
    overlaps = [
        max(0.0, scheduled[i - 1].scheduled_end - scheduled[i].scheduled_start)
        for i in range(1, len(scheduled))
    ]
    advances = [x.advance for x in scheduled]
    shifts = [x.scheduled_start - x.original_start for x in scheduled]
    return {
        "unit_count": len(scheduled),
        "audio_overlap_count": sum(1 for x in overlaps if x > 0.005),
        "max_audio_overlap_seconds": round(max(overlaps) if overlaps else 0.0, 3),
        "max_gap_seconds": round(max(gaps) if gaps else 0.0, 3),
        "avg_gap_seconds": round(sum(gaps) / max(1, len(gaps)), 3),
        "gap_over_2s": sum(1 for g in gaps if g > 2.0),
        "gap_over_4s": sum(1 for g in gaps if g > 4.0),
        "advanced_units": sum(1 for a in advances if a > 0.01),
        "max_advance_seconds": round(max(advances) if advances else 0.0, 3),
        "max_abs_shift_seconds": round(max((abs(x) for x in shifts), default=0.0), 3),
        "delayed_units": sum(1 for x in shifts if x > 0.01),
        "max_delay_seconds": round(max((x for x in shifts if x > 0), default=0.0), 3),
        "audio_end_seconds": round(max((x.scheduled_end for x in scheduled), default=0.0), 3),
    }


def count_source_overruns(report: dict[str, Any]) -> int:
    return sum(1 for flag in report.get("tts", {}).get("flags", []) if flag.get("type") == "tts_overrun")


def schedule_to_dict(item: ScheduledUnit) -> dict[str, Any]:
    return {
        "unit_id": item.unit.id,
        "segment_ids": item.unit.segment_ids,
        "original_start": round(item.original_start, 3),
        "scheduled_start": round(item.scheduled_start, 3),
        "scheduled_end": round(item.scheduled_end, 3),
        "duration": round(item.duration, 3),
        "advance": round(item.advance, 3),
        "pcm": str(item.pcm),
    }
