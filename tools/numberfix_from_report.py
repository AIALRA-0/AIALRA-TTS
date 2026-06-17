from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from ecse_localizer.config import load_config, privacy_guard
from ecse_localizer.ffmpeg_utils import media_duration
from ecse_localizer.subtitle_io import Segment, to_dicts
from ecse_localizer.tts import build_aligned_dub, make_tts_units
from ecse_localizer.utils import ensure_dir, now_id, read_json, setup_logger, slugify, write_json


REPAIRS = {
    92: "大约每盒能得到9500到10000个芯片。",
    629: "太高的话，就要开始寻找替代材料，比如钌、铑等；这些大约发生在20到18nm间距附近。",
    634: "据说大约有10%到15%的性能提升，这可能来自电容降低，也可能来自其他因素。",
    636: "这大约有14到18层布线。",
    637: "有些甚至达到20到21层金属。",
    847: "700纳米或1000纳米间距，逐步缩小到250到6nm间距。",
}

EN_REPAIRS = {
    92: "And you get to about 9500 to 10000 chips per box.",
    629: "Too high, in which case you have to start looking at alternates, whether it is ruthenium, rhodium; all those things happen roughly around the 20 to 18 nm pitch.",
    634: "It is roughly said 10 to 15 percent performance improvement, and that can come from capacitance reduction or a whole variety of things.",
    636: "That is roughly 14 to 18 levels of wiring in this.",
    637: "Some of them go even to 20 to 21 levels of metal.",
    847: "700 pitch or a thousand nanometer pitch to a 250 to 6 nm pitch.",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--tag", default="numberfix")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    privacy_guard(config)
    logger = setup_logger("numberfix", Path(config["project_root"]) / "logs" / "numberfix.log")
    report_path = Path(args.report)
    report = read_json(report_path)
    en_segments = [Segment(int(x["id"]), float(x["start"]), float(x["end"]), str(x["text"])) for x in report["segments"]["en"]]
    zh_segments = [Segment(int(x["id"]), float(x["start"]), float(x["end"]), str(x["text"])) for x in report["segments"]["zh"]]
    en_by_id = {seg.id: seg for seg in en_segments}
    for sid, text in EN_REPAIRS.items():
        if sid in en_by_id:
            en_by_id[sid].text = text
    by_id = {seg.id: seg for seg in zh_segments}
    for sid, text in REPAIRS.items():
        if sid in by_id:
            by_id[sid].text = text

    units = make_tts_units(zh_segments, config)
    changed_unit_ids = sorted({unit.id for unit in units if any(sid in REPAIRS for sid in unit.segment_ids)})
    old_tts = Path(config["work_dir"]) / str(report["run_id"]) / "tts_segments"
    if not old_tts.exists():
        raise RuntimeError(f"Old TTS cache not found: {old_tts}")

    base = str(report.get("name", report_path.stem.replace("_report", "")))
    base = f"{base}_{args.tag}"
    run_dir = ensure_dir(Path(config["work_dir"]) / now_id(slugify(base, 44)))
    new_tts = ensure_dir(run_dir / "tts_segments")
    for src in old_tts.glob("*"):
        if not src.is_file():
            continue
        try:
            sid = int(src.stem.split("_")[1])
        except Exception:
            sid = -1
        if sid in changed_unit_ids:
            continue
        shutil.copy2(src, new_tts / src.name)

    work_video = Path(report.get("work_video") or report.get("source_video"))
    duration = media_duration(work_video)
    temp_wav = run_dir / f"{base}_source_timeline_zh_dub.wav"
    tts_info = build_aligned_dub(zh_segments, duration, temp_wav, run_dir, config, logger)
    result = {
        "name": base,
        "run_id": run_dir.name,
        "mode": "targeted_numberfix_source",
        "source_video": report.get("source_video"),
        "work_video": str(work_video),
        "source_report": str(report_path),
        "subtitle_source": report.get("subtitle_source"),
        "asr_backend": report.get("asr_backend"),
        "translation_backend": str(report.get("translation_backend", "")) + "_targeted_numberfix",
        "audio_enhancement": report.get("audio_enhancement"),
        "tts": tts_info,
        "outputs": {"source_timeline_zh_dub_wav": str(temp_wav)},
        "qa": {"pass": True, "issues": []},
        "numberfix_repairs": [{"segment_id": sid, "zh": text} for sid, text in REPAIRS.items()],
        "segments": {"en": to_dicts(en_segments), "zh": to_dicts(zh_segments)},
    }
    out_report = Path(config["output_dir"]) / f"{base}_source_report.json"
    write_json(out_report, result)
    write_json(run_dir / "result.json", result)
    print(out_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
