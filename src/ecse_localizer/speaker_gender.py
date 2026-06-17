from __future__ import annotations

import logging
import math
import statistics
import wave
from pathlib import Path
from typing import Any

from .utils import ensure_dir, run_cmd, write_json


def resolve_tts_speaker(
    source_video: str | Path | None,
    work_dir: str | Path,
    config: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    tts = config.get("tts", {})
    mode = str(tts.get("speaker_gender", "auto")).lower()
    male = str(tts.get("male_speaker", "中文男"))
    female = str(tts.get("female_speaker", "中文女"))
    unknown = str(tts.get("unknown_speaker", female))

    if mode in {"male", "man", "m"}:
        return {"gender": "male", "speaker": male, "confidence": 1.0, "method": "configured"}
    if mode in {"female", "woman", "f"}:
        return {"gender": "female", "speaker": female, "confidence": 1.0, "method": "configured"}
    if mode not in {"auto", "detect", "detected"} or not tts.get("auto_detect_speaker_gender", True):
        speaker = str(tts.get("cosyvoice_speaker", unknown))
        return {"gender": "configured", "speaker": speaker, "confidence": 1.0, "method": "configured"}

    if not source_video:
        speaker = str(tts.get("cosyvoice_speaker", unknown))
        return {"gender": "unknown", "speaker": speaker, "confidence": 0.0, "method": "no_source_video"}

    result = detect_speaker_gender(source_video, work_dir, config, logger)
    gender = result.get("gender", "unknown")
    if gender == "male":
        speaker = male
    elif gender == "female":
        speaker = female
    else:
        speaker = unknown
    result["speaker"] = speaker
    return result


def detect_speaker_gender(
    source_video: str | Path,
    work_dir: str | Path,
    config: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    work = ensure_dir(work_dir)
    tts = config.get("tts", {})
    sample_seconds = float(tts.get("speaker_gender_sample_seconds", 180.0))
    wav_path = work / "speaker_gender_sample.wav"
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
            str(source_video),
            "-t",
            f"{sample_seconds:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-af",
            "highpass=f=70,lowpass=f=3500,afftdn=nf=-25",
            str(wav_path),
        ],
        logger=logger,
    )
    pitches = estimate_pitch_track(wav_path)
    if len(pitches) < 12:
        result = {
            "gender": "unknown",
            "confidence": 0.0,
            "median_f0_hz": None,
            "voiced_frames": len(pitches),
            "method": "local_autocorrelation_f0",
        }
    else:
        median_f0 = statistics.median(pitches)
        if median_f0 < 165.0:
            gender = "male"
            confidence = min(0.98, max(0.55, (180.0 - median_f0) / 70.0))
        elif median_f0 > 185.0:
            gender = "female"
            confidence = min(0.98, max(0.55, (median_f0 - 170.0) / 90.0))
        else:
            gender = "unknown"
            confidence = 0.45
        result = {
            "gender": gender,
            "confidence": round(float(confidence), 3),
            "median_f0_hz": round(float(median_f0), 2),
            "voiced_frames": len(pitches),
            "method": "local_autocorrelation_f0",
        }
    write_json(work / "speaker_gender.json", result)
    return result


def estimate_pitch_track(wav_path: str | Path) -> list[float]:
    samples, sample_rate = read_mono_pcm16(wav_path)
    if not samples:
        return []
    frame = int(sample_rate * 0.04)
    hop = int(sample_rate * 0.02)
    if frame <= 0 or hop <= 0 or len(samples) < frame:
        return []
    rms_values: list[float] = []
    frames: list[list[float]] = []
    for start in range(0, len(samples) - frame, hop):
        chunk = samples[start : start + frame]
        rms = math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk)))
        rms_values.append(rms)
        frames.append(chunk)
    if not rms_values:
        return []
    sorted_rms = sorted(rms_values)
    noise_floor = sorted_rms[int(len(sorted_rms) * 0.55)]
    loud_ref = sorted_rms[int(len(sorted_rms) * 0.90)]
    threshold = max(220.0, noise_floor * 1.8, loud_ref * 0.18)

    pitches: list[float] = []
    for chunk, rms in zip(frames, rms_values):
        if rms < threshold:
            continue
        f0 = estimate_frame_f0(chunk, sample_rate)
        if 75.0 <= f0 <= 300.0:
            pitches.append(f0)
        if len(pitches) >= 260:
            break
    return reject_outliers(pitches)


def estimate_frame_f0(chunk: list[float], sample_rate: int) -> float:
    mean = sum(chunk) / max(1, len(chunk))
    x = [v - mean for v in chunk]
    energy = sum(v * v for v in x)
    if energy <= 1e-6:
        return 0.0
    min_lag = int(sample_rate / 300.0)
    max_lag = int(sample_rate / 75.0)
    best_lag = 0
    best_corr = 0.0
    for lag in range(min_lag, min(max_lag, len(x) - 2)):
        a = x[:-lag]
        b = x[lag:]
        num = sum(u * v for u, v in zip(a, b))
        den = math.sqrt(sum(u * u for u in a) * sum(v * v for v in b)) or 1.0
        corr = num / den
        if corr > best_corr:
            best_corr = corr
            best_lag = lag
    if best_corr < 0.35 or best_lag <= 0:
        return 0.0
    return sample_rate / best_lag


def read_mono_pcm16(wav_path: str | Path) -> tuple[list[float], int]:
    with wave.open(str(wav_path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise RuntimeError(f"Expected 16-bit PCM wav for gender detection: {wav_path}")
    values = []
    for i in range(0, len(raw), 2 * channels):
        total = 0
        for ch in range(channels):
            offset = i + ch * 2
            total += int.from_bytes(raw[offset : offset + 2], "little", signed=True)
        values.append(float(total / max(1, channels)))
    return values, sample_rate


def reject_outliers(values: list[float]) -> list[float]:
    if len(values) < 8:
        return values
    med = statistics.median(values)
    deviations = [abs(x - med) for x in values]
    mad = statistics.median(deviations) or 1.0
    return [x for x in values if abs(x - med) <= 2.8 * mad]
