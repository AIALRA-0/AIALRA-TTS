from __future__ import annotations

import logging
import json
import re
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ffmpeg_utils import audio_duration
from .llm_local import LocalLLMClient
from .speaker_gender import resolve_tts_speaker
from .subtitle_io import Segment
from .translation_quality import target_uses_compact_script, translation_target_language
from .utils import PROJECT_ROOT, ensure_dir, run_cmd, write_json


@dataclass
class TTSUnit:
    id: int
    start: float
    end: float
    text: str
    segment_ids: list[int]

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def tts_health(config: dict) -> dict[str, Any]:
    tts = config.get("tts", {})
    piper = Path(tts.get("piper_exe", ""))
    model = Path(tts.get("piper_model", ""))
    cosy_python = Path(tts.get("cosyvoice_python", ""))
    cosy_root = Path(tts.get("cosyvoice_root", ""))
    cosy_model = Path(tts.get("cosyvoice_model_dir", ""))
    cosy_available = cosy_python.exists() and cosy_root.exists() and cosy_model.exists()
    return {
        "cosyvoice_python": str(cosy_python),
        "cosyvoice_python_exists": cosy_python.exists(),
        "cosyvoice_root": str(cosy_root),
        "cosyvoice_root_exists": cosy_root.exists(),
        "cosyvoice_model": str(cosy_model),
        "cosyvoice_model_exists": cosy_model.exists(),
        "piper_exe": str(piper),
        "piper_exe_exists": piper.exists(),
        "piper_model": str(model),
        "piper_model_exists": model.exists(),
        "backend": "cosyvoice_sft" if cosy_available else ("piper" if piper.exists() and model.exists() else "ffmpeg_tone_fallback"),
    }


def synthesize(
    text: str,
    voice: str,
    dialect: str,
    speed: float,
    emotion: str,
    out_wav: str | Path,
    config: dict,
    logger: logging.Logger | None = None,
) -> str:
    health = tts_health(config)
    if health["backend"] == "piper":
        try:
            synthesize_piper(text, speed, out_wav, config, logger)
            return "piper"
        except Exception as exc:
            if logger:
                logger.warning("Piper TTS failed, falling back to tone placeholder: %s", exc)
    synthesize_tone_placeholder(text, out_wav, logger)
    return "ffmpeg_tone_fallback"


def synthesize_piper(
    text: str,
    speed: float,
    out_wav: str | Path,
    config: dict,
    logger: logging.Logger | None = None,
) -> None:
    tts = config.get("tts", {})
    piper = str(Path(tts["piper_exe"]))
    model = str(Path(tts["piper_model"]))
    cmd = [
        piper,
        "--model",
        model,
        "--output_file",
        str(out_wav),
        "--length_scale",
        f"{1.0 / max(speed, 0.2):.3f}",
        "--noise_scale",
        f"{float(tts.get('piper_noise_scale', 0.45)):.3f}",
        "--noise_w",
        f"{float(tts.get('piper_noise_w', 0.65)):.3f}",
        "--sentence_silence",
        f"{float(tts.get('piper_sentence_silence', 0.08)):.3f}",
        "--quiet",
    ]
    proc = run_cmd(cmd, input_text=text + "\n", logger=logger, check=False, timeout=120)
    if proc.returncode != 0:
        cmd = [piper, "--model", model, "--output_file", str(out_wav)]
        run_cmd(cmd, input_text=text + "\n", logger=logger, timeout=120)
    if not Path(out_wav).exists() or Path(out_wav).stat().st_size < 1000:
        raise RuntimeError("Piper produced no usable wav")


def synthesize_tone_placeholder(text: str, out_wav: str | Path, logger: logging.Logger | None = None) -> None:
    duration = max(0.35, min(8.0, len(text) / 5.0))
    run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration:.3f}",
            "-af",
            "volume=0.03",
            "-ac",
            "1",
            "-ar",
            "22050",
            str(out_wav),
        ],
        logger=logger,
    )


def build_aligned_dub(
    segments: list[Segment],
    video_duration: float,
    out_wav: str | Path,
    work_dir: str | Path,
    config: dict,
    source_video: str | Path | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    work = ensure_dir(Path(work_dir) / "tts_segments")
    sample_rate = 22050
    total_samples = int((video_duration + 0.25) * sample_rate)
    mix = array("h", [0]) * total_samples
    tts_cfg = config.get("tts", {})
    max_speed = float(tts_cfg.get("max_speed", 1.25))
    absolute_max = float(tts_cfg.get("absolute_max_speed", 1.35))
    end_gap = float(tts_cfg.get("end_gap_seconds", 0.2))
    min_fill = float(tts_cfg.get("min_fill_ratio", 0.58))
    target_fill = float(tts_cfg.get("target_fill_ratio", 0.88))
    min_slow_factor = float(tts_cfg.get("min_slow_factor", 0.82))
    prevent_overlap = bool(tts_cfg.get("prevent_audio_overlap", True))
    min_audio_gap = float(tts_cfg.get("min_audio_gap_seconds", 0.08))
    flags: list[dict[str, Any]] = []
    audio_delays: list[float] = []
    placements: list[dict[str, Any]] = []
    audio_overlap_count = 0
    truncated_audio_count = 0
    previous_audio_end: float | None = None
    backend_used = None
    prefer_cosyvoice = select_tts_backend(config) == "cosyvoice_sft"
    units = make_tts_units(segments, config)
    speaker_info = resolve_tts_speaker(source_video, work, config, logger) if prefer_cosyvoice else {}

    if prefer_cosyvoice:
        try:
            synthesize_cosyvoice_batch(units, work, config, speaker=speaker_info.get("speaker"), logger=logger)
            backend_used = "cosyvoice_sft"
        except Exception as exc:
            prefer_cosyvoice = False
            backend_used = None
            if logger:
                logger.warning("CosyVoice batch synthesis failed; falling back to Piper per segment: %s", exc)

    for index, unit in enumerate(units):
        if not unit.text.strip():
            continue
        clip = work / (f"seg_{unit.id:05d}_cosy.wav" if prefer_cosyvoice else f"seg_{unit.id:05d}.wav")
        if not clip.exists() or clip.stat().st_size < 1000:
            backend_used = synthesize(
                unit.text,
                tts_cfg.get("default_voice", "neutral_chinese_teacher"),
                tts_cfg.get("dialect", "mandarin"),
                float(tts_cfg.get("speed", 1.0)),
                tts_cfg.get("emotion", "calm_teaching"),
                clip,
                config,
                logger,
            )
        else:
            backend_used = backend_used or select_tts_backend(config)

        clip_dur = audio_duration(clip)
        next_unit = units[index + 1] if index + 1 < len(units) else None
        unconstrained_target = tts_unconstrained_target_duration(unit, video_duration, config)
        static_target = tts_target_duration(unit, next_unit, video_duration, config)
        scheduled_start_hint = tts_scheduled_start(unit, previous_audio_end, video_duration, config)
        target = tts_dynamic_target_duration(unit, next_unit, previous_audio_end, video_duration, config)
        if target < unconstrained_target - 0.01:
            flags.append(
                {
                    "segment_id": unit.id,
                    "segment_ids": unit.segment_ids,
                    "type": "tts_slot_constrained",
                    "unconstrained_target_duration": round(unconstrained_target, 3),
                    "target_duration": round(target, 3),
                    "next_start": round(next_unit.start, 3) if next_unit else None,
                }
            )
        if target < static_target - 0.01:
            flags.append(
                {
                    "segment_id": unit.id,
                    "segment_ids": unit.segment_ids,
                    "type": "tts_slot_constrained_by_delayed_start",
                    "static_target_duration": round(static_target, 3),
                    "target_duration": round(target, 3),
                    "scheduled_start": round(scheduled_start_hint, 3),
                    "original_start": round(unit.start, 3),
                }
            )
        final_clip = clip
        if clip_dur < target * min_fill:
            desired_duration = max(clip_dur, target * target_fill)
            factor = max(min_slow_factor, min(0.999, clip_dur / max(desired_duration, 0.01)))
            if factor < 0.999:
                stretched = work / f"seg_{unit.id:05d}_slow.wav"
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
                        "original_duration": clip_dur,
                        "target_duration": target,
                        "applied_atempo": factor,
                    }
                )
                clip = stretched
                clip_dur = audio_duration(clip)
                final_clip = stretched
        if clip_dur > target * 1.04:
            compact_text = None if prefer_cosyvoice else compress_text_for_tts(unit.text, target, clip_dur, config, logger)
            if compact_text and compact_text != unit.text:
                compact_clip = work / f"seg_{unit.id:05d}_compact.wav"
                backend_used = synthesize(
                    compact_text,
                    tts_cfg.get("default_voice", "neutral_chinese_teacher"),
                    tts_cfg.get("dialect", "mandarin"),
                    float(tts_cfg.get("speed", 1.0)),
                    tts_cfg.get("emotion", "calm_teaching"),
                    compact_clip,
                    config,
                    logger,
                )
                compact_dur = audio_duration(compact_clip)
                if compact_dur < clip_dur:
                    flags.append(
                        {
                            "segment_id": unit.id,
                            "segment_ids": unit.segment_ids,
                            "type": "tts_text_compressed",
                            "original_chars": len(unit.text),
                            "compressed_chars": len(compact_text),
                            "original_duration": clip_dur,
                            "compressed_duration": compact_dur,
                        }
                    )
                    clip = compact_clip
                    clip_dur = compact_dur
                    final_clip = compact_clip
            if clip_dur > target * 1.04:
                needed = clip_dur / target
                factor = min(needed, absolute_max)
                if needed > max_speed:
                    flags.append(
                        {
                            "segment_id": unit.id,
                            "segment_ids": unit.segment_ids,
                            "type": "tts_overrun",
                            "needed_speed": needed,
                            "applied_speed": factor,
                            "max_speed": max_speed,
                            "absolute_max_speed": absolute_max,
                        }
                    )
                compressed = work / f"seg_{unit.id:05d}_speed.wav"
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
                final_clip = compressed

        final_clip = enforce_tts_slot_limit(final_clip, target, work, unit, flags, config, logger)

        pcm = work / f"seg_{unit.id:05d}_pcm.wav"
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
                str(final_clip),
                "-af",
                clip_filter_for_backend(backend_used, config),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-sample_fmt",
                "s16",
                str(pcm),
            ],
            logger=logger,
        )
        scheduled_start = tts_scheduled_start(unit, previous_audio_end, video_duration, config)
        pcm_dur = audio_duration(pcm)
        would_overlap = previous_audio_end is not None and unit.start < previous_audio_end + min_audio_gap
        if would_overlap:
            audio_overlap_count += 1
        delay = max(0.0, scheduled_start - unit.start)
        if delay > 0.005:
            audio_delays.append(delay)
        overlay_pcm(mix, pcm, int(scheduled_start * sample_rate))
        scheduled_end = scheduled_start + pcm_dur
        clipped_end = min(video_duration, scheduled_end)
        truncated = max(0.0, scheduled_end - video_duration)
        if truncated > 0.01:
            truncated_audio_count += 1
            flags.append(
                {
                    "segment_id": unit.id,
                    "segment_ids": unit.segment_ids,
                    "type": "tts_audio_truncated_at_video_end",
                    "scheduled_start": round(scheduled_start, 3),
                    "pcm_duration": round(pcm_dur, 3),
                    "truncated_seconds": round(truncated, 3),
                }
            )
        placements.append(
            {
                "segment_id": unit.id,
                "segment_ids": unit.segment_ids,
                "original_start": round(unit.start, 3),
                "original_end": round(unit.end, 3),
                "scheduled_start": round(scheduled_start, 3),
                "scheduled_end": round(clipped_end, 3),
                "pcm_duration": round(pcm_dur, 3),
                "delay_seconds": round(delay, 3),
                "truncated_seconds": round(truncated, 3),
            }
        )
        previous_audio_end = clipped_end

    final_out = Path(out_wav)
    raw_mix = final_out.with_name(final_out.stem + "_rawmix.wav")
    write_pcm(raw_mix, mix, sample_rate)
    if config.get("tts", {}).get("clean_final_audio", True):
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
    else:
        raw_mix.replace(final_out)
    return {
        "backend": backend_used or select_tts_backend(config),
        "duration": audio_duration(final_out),
        "segment_count": len(segments),
        "utterance_count": len(units),
        "alignment_mode": str(config.get("tts", {}).get("align_mode", "segment")),
        "flags": flags,
        "speaker": speaker_info.get("speaker"),
        "speaker_gender": speaker_info,
        "prevent_audio_overlap": prevent_overlap,
        "audio_overlap_count": 0 if prevent_overlap else audio_overlap_count,
        "would_overlap_without_prevention_count": audio_overlap_count if prevent_overlap else None,
        "audio_delay_count": len(audio_delays),
        "max_audio_delay_seconds": round(max(audio_delays) if audio_delays else 0.0, 3),
        "truncated_audio_count": truncated_audio_count,
        "placements": placements[:200],
    }


def make_tts_units(segments: list[Segment], config: dict) -> list[TTSUnit]:
    tts_cfg = config.get("tts", {})
    if tts_cfg.get("align_mode", "segment") != "grouped":
        return [TTSUnit(seg.id, seg.start, seg.end, seg.text, [seg.id]) for seg in segments]

    max_group_duration = float(tts_cfg.get("max_group_duration", 12.0))
    min_group_duration = float(tts_cfg.get("min_group_duration", 7.0))
    max_group_chars = int(tts_cfg.get("max_group_chars", 110))
    merge_gap = float(tts_cfg.get("merge_gap_seconds", 0.35))
    estimated_cps = float(tts_cfg.get("estimated_zh_chars_per_second", 5.2))
    min_estimated_fill = float(tts_cfg.get("group_min_estimated_fill_ratio", 0.72))
    end_gap = float(tts_cfg.get("end_gap_seconds", 0.2))

    units: list[TTSUnit] = []
    i = 0
    while i < len(segments):
        current = segments[i]
        ids = [current.id]
        text = current.text.strip()
        start = current.start
        end = current.end
        i += 1
        while i < len(segments):
            nxt = segments[i]
            gap = nxt.start - end
            candidate_duration = nxt.end - start
            candidate_text = join_tts_text(text, nxt.text)
            if gap > merge_gap or candidate_duration > max_group_duration or spoken_text_len(candidate_text) > max_group_chars:
                break
            target = max(0.25, end - start - end_gap)
            estimated = estimate_spoken_duration(text, estimated_cps)
            too_sparse = estimated < target * min_estimated_fill
            too_short = (end - start) < min_group_duration
            if not (too_sparse or too_short):
                break
            ids.append(nxt.id)
            text = candidate_text
            end = nxt.end
            i += 1
        units.append(TTSUnit(ids[0], start, end, text, ids))
    return units


def join_tts_text(left: str, right: str) -> str:
    left = (left or "").strip()
    right = (right or "").strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1] in "。！？!?；;：:":
        return f"{left}{right}"
    if left[-1] in "，、,":
        return f"{left}{right}"
    return f"{left}。{right}"


def spoken_text_len(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or "")) + len(re.findall(r"[A-Za-z0-9]+", text or ""))


def estimate_spoken_duration(text: str, chars_per_second: float) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    latin = sum(max(1, len(token) // 4) for token in re.findall(r"[A-Za-z0-9_.+-]+", text or ""))
    punctuation_pause = 0.12 * len(re.findall(r"[。！？!?；;]", text or ""))
    return (cjk + latin) / max(chars_per_second, 0.1) + punctuation_pause


def tts_unconstrained_target_duration(unit: TTSUnit, video_duration: float, config: dict) -> float:
    tts_cfg = config.get("tts", {})
    end_gap = float(tts_cfg.get("end_gap_seconds", 0.2))
    available = max(0.0, min(unit.end, video_duration) - max(0.0, unit.start))
    return max(0.25, available - end_gap)


def tts_target_duration(unit: TTSUnit, next_unit: TTSUnit | None, video_duration: float, config: dict) -> float:
    target = tts_unconstrained_target_duration(unit, video_duration, config)
    tts_cfg = config.get("tts", {})
    if not bool(tts_cfg.get("prevent_audio_overlap", True)) or next_unit is None:
        return target
    min_gap = float(tts_cfg.get("min_audio_gap_seconds", 0.08))
    next_start = min(max(0.0, next_unit.start), video_duration)
    slot = next_start - max(0.0, unit.start) - max(0.0, min_gap)
    if slot <= 0:
        return 0.25
    return max(0.25, min(target, slot))


def tts_scheduled_start(unit: TTSUnit, previous_audio_end: float | None, video_duration: float, config: dict) -> float:
    tts_cfg = config.get("tts", {})
    start = max(0.0, min(unit.start, video_duration))
    if bool(tts_cfg.get("prevent_audio_overlap", True)) and previous_audio_end is not None:
        min_gap = float(tts_cfg.get("min_audio_gap_seconds", 0.08))
        start = max(start, previous_audio_end + min_gap)
    return max(0.0, min(start, video_duration))


def tts_dynamic_target_duration(
    unit: TTSUnit,
    next_unit: TTSUnit | None,
    previous_audio_end: float | None,
    video_duration: float,
    config: dict,
) -> float:
    target = tts_target_duration(unit, next_unit, video_duration, config)
    tts_cfg = config.get("tts", {})
    scheduled_start = tts_scheduled_start(unit, previous_audio_end, video_duration, config)
    if bool(tts_cfg.get("shrink_delayed_slots_to_original_timeline", True)):
        end_gap = float(tts_cfg.get("end_gap_seconds", 0.2))
        same_segment_slot = min(unit.end, video_duration) - scheduled_start - max(0.0, end_gap)
        target = min(target, max(0.25, same_segment_slot))
    if bool(tts_cfg.get("prevent_audio_overlap", True)) and next_unit is not None:
        min_gap = float(tts_cfg.get("min_audio_gap_seconds", 0.08))
        next_start = min(max(0.0, next_unit.start), video_duration)
        next_slot = next_start - scheduled_start - max(0.0, min_gap)
        target = min(target, max(0.25, next_slot) if next_slot > 0 else 0.25)
    remaining_video = video_duration - scheduled_start
    if remaining_video > 0:
        target = min(target, max(0.25, remaining_video))
    return max(0.25, target)


def enforce_tts_slot_limit(
    clip: str | Path,
    target_duration: float,
    work: str | Path,
    unit: TTSUnit,
    flags: list[dict[str, Any]],
    config: dict,
    logger: logging.Logger | None = None,
) -> Path:
    tts_cfg = config.get("tts", {})
    if not bool(tts_cfg.get("trim_overlong_audio_to_slot", True)):
        return Path(clip)

    tolerance = float(tts_cfg.get("slot_trim_tolerance_seconds", 0.03))
    target = max(0.05, float(target_duration))
    duration = audio_duration(clip)
    if duration <= target + tolerance:
        return Path(clip)

    fade = max(0.0, float(tts_cfg.get("slot_trim_fade_seconds", 0.06)))
    fade = min(fade, max(0.0, target / 3.0))
    trim_path = Path(work) / f"seg_{unit.id:05d}_slot.wav"
    filters = [f"atrim=0:{target:.3f}", "asetpts=PTS-STARTPTS"]
    if fade >= 0.01:
        filters.append(f"afade=t=out:st={max(0.0, target - fade):.3f}:d={fade:.3f}")
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
            ",".join(filters),
            str(trim_path),
        ],
        logger=logger,
    )
    flags.append(
        {
            "segment_id": unit.id,
            "segment_ids": unit.segment_ids,
            "type": "tts_slot_trimmed",
            "pre_trim_duration": round(duration, 3),
            "target_duration": round(target, 3),
            "trimmed_seconds": round(max(0.0, duration - target), 3),
            "fade_seconds": round(fade, 3),
        }
    )
    return trim_path


def select_tts_backend(config: dict) -> str:
    health = tts_health(config)
    for backend in config.get("tts", {}).get("backend_order", ["cosyvoice", "piper"]):
        if backend == "cosyvoice" and health["cosyvoice_python_exists"] and health["cosyvoice_root_exists"] and health["cosyvoice_model_exists"]:
            return "cosyvoice_sft"
        if backend == "piper" and health["piper_exe_exists"] and health["piper_model_exists"]:
            return "piper"
    return "ffmpeg_tone_fallback"


def clip_filter_for_backend(backend: str | None, config: dict) -> str:
    if backend == "cosyvoice_sft":
        gain = float(config.get("tts", {}).get("cosyvoice_gain", 3.0))
        return f"volume={gain:.3f},alimiter=limit=0.95"
    return "anull"


def final_audio_filter_for_speaker(config: dict, speaker_info: dict[str, Any] | None = None) -> str:
    tts = config.get("tts", {})
    gender = str((speaker_info or {}).get("gender", "")).lower()
    if gender == "male" and tts.get("final_audio_filter_male"):
        return str(tts["final_audio_filter_male"])
    if gender == "female" and tts.get("final_audio_filter_female"):
        return str(tts["final_audio_filter_female"])
    return str(
        tts.get(
            "final_audio_filter",
            "volume=0.90,highpass=f=90,lowpass=f=9500,equalizer=f=260:t=q:w=1.0:g=-2.5,equalizer=f=3200:t=q:w=1.0:g=3.0,equalizer=f=5200:t=q:w=1.0:g=1.5,acompressor=threshold=-18dB:ratio=2.0:attack=8:release=120,alimiter=limit=0.95",
        )
    )


def synthesize_cosyvoice_batch(
    segments: list[Segment],
    work: str | Path,
    config: dict,
    *,
    suffix: str = "_cosy",
    speaker: str | None = None,
    speed: float | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    tts = config.get("tts", {})
    cosy_python = Path(tts.get("cosyvoice_python", ""))
    cosy_root = Path(tts.get("cosyvoice_root", ""))
    cosy_model = Path(tts.get("cosyvoice_model_dir", ""))
    if not cosy_python.exists() or not cosy_root.exists() or not cosy_model.exists():
        raise RuntimeError("CosyVoice backend is not installed")
    work = ensure_dir(work)
    payload = {
        "output_dir": str(work),
        "segments": [
            {
                "id": seg.id,
                "text": seg.text,
                "out_wav": str(work / f"seg_{seg.id:05d}{suffix}.wav"),
            }
            for seg in segments
            if seg.text.strip()
        ],
    }
    input_json = work / f"cosyvoice_batch{suffix}.json"
    output_json = work / f"cosyvoice_batch{suffix}_result.json"
    write_json(input_json, payload)
    run_cmd(
        [
            str(cosy_python),
            str(PROJECT_ROOT / "tools" / "cosyvoice_batch.py"),
            "--cosyvoice-root",
            str(cosy_root),
            "--model-dir",
            str(cosy_model),
            "--input-json",
            str(input_json),
            "--output-json",
            str(output_json),
            "--speaker",
            str(speaker or tts.get("cosyvoice_speaker", "中文男")),
            "--speed",
            f"{float(speed if speed is not None else tts.get('cosyvoice_speed', tts.get('speed', 1.0))):.3f}",
        ],
        logger=logger,
        timeout=None,
    )
    result = json.loads(output_json.read_text(encoding="utf-8"))
    if result.get("failures"):
        raise RuntimeError(f"CosyVoice failures: {result['failures'][:3]}")
    return result


def compress_text_for_tts(
    text: str,
    target_duration: float,
    current_duration: float,
    config: dict,
    logger: logging.Logger | None = None,
) -> str | None:
    ratio = max(0.35, min(0.95, target_duration / max(current_duration, 0.01) * 0.92))
    char_limit = max(6, min(len(text) - 1, int(len(text) * ratio)))
    if char_limit >= len(text):
        return None

    llm_text = llm_compress_text_for_tts(text, target_duration, char_limit, config, logger)
    if is_valid_tts_compression(text, llm_text, char_limit, config):
        return llm_text

    if target_uses_compact_script(config):
        local_text = local_compact_tts_text(text, char_limit)
        if is_valid_tts_compression(text, local_text, char_limit + 4, config):
            return local_text
    return None


def llm_compress_text_for_tts(
    text: str,
    target_duration: float,
    char_limit: int,
    config: dict,
    logger: logging.Logger | None = None,
) -> str | None:
    try:
        prompt_path = Path(config["project_root"]) / "prompts" / "compression.md"
        system = prompt_path.read_text(encoding="utf-8")
        payload = {
            "text": text,
            "target_language": translation_target_language(config),
            "target_duration": round(target_duration, 3),
            "char_limit": char_limit,
            "instruction": "Compress only for dubbing alignment in the requested target language. Preserve technical meaning, numbers, acronyms, formulas, names, and URLs.",
        }
        client = LocalLLMClient(config)
        status = client.status()
        if not status.available:
            return None
        data = client.json_chat(system, json.dumps(payload, ensure_ascii=False), '{"text":"compressed target-language dubbing text","flags":[]}')
        return normalize_tts_compression_candidate(str(data.get("text", "")), config)
    except Exception as exc:
        if logger:
            logger.warning("LLM TTS compression failed: %s", exc)
        return None


def local_compact_tts_text(text: str, char_limit: int) -> str:
    compact = re.sub(r"\s+", "", text or "")
    replacements = [
        ("一直在从事", "做"),
        ("一直在做", "做"),
        ("从事", "做"),
        ("之前还有", "之前"),
        ("之前有", "之前"),
        ("我们先", "先"),
        ("讨论一下", "讨论"),
        ("简短介绍一下", "简单说"),
        ("如果有我的邮箱", "有我的邮箱的话"),
        ("请有问题随时", "有问题请"),
        ("一定会", "会"),
        ("接下来继续讲", "继续讲"),
        ("那么", ""),
        ("这里", ""),
        ("一下", ""),
        ("就是", ""),
    ]
    for old, new in replacements:
        compact = compact.replace(old, new)
    if len(compact) <= char_limit:
        return ensure_sentence_punctuation(compact)

    clauses = re.split(r"([，。；;])", compact)
    rebuilt = ""
    for i in range(0, len(clauses), 2):
        clause = clauses[i]
        punct = clauses[i + 1] if i + 1 < len(clauses) else ""
        if not clause:
            continue
        candidate = rebuilt + clause + punct
        if len(candidate) <= char_limit:
            rebuilt = candidate
    if len(rebuilt) >= 6 and preserves_numbers(text, rebuilt):
        return ensure_sentence_punctuation(rebuilt)

    protected_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.+-]*|\d+(?:\.\d+)?", compact)
    suffix = ""
    for token in protected_tokens:
        if token not in compact[:char_limit] and token not in suffix:
            suffix += token
    budget = max(1, char_limit - len(suffix) - 1)
    shortened = compact[:budget].rstrip("，、；：。") + suffix
    return ensure_sentence_punctuation(shortened)


def is_valid_tts_compression(original: str, compressed: str | None, char_limit: int, config: dict[str, Any] | None = None) -> bool:
    if not compressed:
        return False
    if len(compressed) >= len(original):
        return False
    if len(compressed) > max(char_limit + 4, int(len(original) * 0.9)):
        return False
    if target_uses_compact_script(config):
        if not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", compressed):
            return False
    elif len(re.sub(r"[\s\W_]+", "", compressed, flags=re.UNICODE)) < 2:
        return False
    return preserves_numbers(original, compressed)


def normalize_tts_compression_candidate(text: str, config: dict[str, Any]) -> str:
    if target_uses_compact_script(config):
        return re.sub(r"\s+", "", text or "").strip()
    return re.sub(r"\s+", " ", text or "").strip()


def preserves_numbers(original: str, compressed: str) -> bool:
    original_nums = re.findall(r"\d+(?:\.\d+)?", original)
    return all(num in compressed for num in original_nums)


def ensure_sentence_punctuation(text: str) -> str:
    text = (text or "").strip("，、；： ")
    if not text:
        return text
    if text[-1] not in "。！？!?":
        return text + "。"
    return text


def overlay_pcm(mix: array, wav_path: Path, start_sample: int) -> None:
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    end = min(len(mix), start_sample + len(samples))
    for i, sample in enumerate(samples[: max(0, end - start_sample)]):
        idx = start_sample + i
        value = mix[idx] + sample
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        mix[idx] = value


def write_pcm(path: str | Path, samples: array, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
