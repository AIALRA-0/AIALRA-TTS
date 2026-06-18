from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import torch
import torchaudio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cosyvoice-root", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--progress-json")
    parser.add_argument("--speaker", default="中文男")
    parser.add_argument("--speed", type=float, default=1.0)
    return parser.parse_args()


def write_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = Path(args.cosyvoice_root).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "third_party" / "Matcha-TTS"))
    os.environ["MODELSCOPE_OFFLINE"] = "1"

    from cosyvoice.cli.cosyvoice import AutoModel

    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    out_dir = Path(payload["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_json = Path(args.progress_json) if args.progress_json else Path(args.output_json).with_name(
        Path(args.output_json).stem + "_progress.json"
    )
    model = AutoModel(model_dir=str(Path(args.model_dir).resolve()))
    results = []
    failures = []
    segments = [item for item in payload["segments"] if str(item.get("text", "")).strip()]
    total = len(segments)
    started_at = time.time()

    write_progress(
        progress_json,
        {
            "backend": "cosyvoice_sft",
            "speaker": args.speaker,
            "status": "running",
            "current": 0,
            "total": total,
            "percent": 0,
            "latest_segment_id": None,
            "latest_out_wav": "",
            "failures": failures,
        },
    )
    print(f"CosyVoice progress: 0/{total}", flush=True)

    with torch.inference_mode():
        for index, item in enumerate(segments, start=1):
            segment_id = int(item["id"])
            text = str(item["text"]).strip()
            out_wav = Path(item["out_wav"])
            try:
                if out_wav.exists() and out_wav.stat().st_size > 1000:
                    results.append({"id": segment_id, "out_wav": str(out_wav), "skipped": True})
                else:
                    for result in model.inference_sft(text, args.speaker, stream=False, speed=args.speed):
                        speech = result["tts_speech"].detach().cpu()
                        torchaudio.save(str(out_wav), speech, model.sample_rate)
                        break
                    if not out_wav.exists() or out_wav.stat().st_size < 1000:
                        raise RuntimeError("CosyVoice produced no usable wav")
                    results.append({"id": segment_id, "out_wav": str(out_wav), "skipped": False})
            except Exception as exc:
                failures.append({"id": segment_id, "error": str(exc)[:500]})
            percent = round(100.0 * index / total, 2) if total else 100
            elapsed = max(0.0, time.time() - started_at)
            rate = index / elapsed if elapsed > 0 else 0
            eta = int(round((total - index) / rate)) if rate > 0 else None
            write_progress(
                progress_json,
                {
                    "backend": "cosyvoice_sft",
                    "speaker": args.speaker,
                    "status": "running",
                    "current": index,
                    "total": total,
                    "percent": percent,
                    "latest_segment_id": segment_id,
                    "latest_out_wav": str(out_wav),
                    "failures": failures,
                    "eta_seconds": eta,
                },
            )
            print(f"CosyVoice progress: {index}/{total} ({percent}%) segment={segment_id}", flush=True)

    Path(args.output_json).write_text(
        json.dumps(
            {
                "backend": "cosyvoice_sft",
                "speaker": args.speaker,
                "sample_rate": getattr(model, "sample_rate", None),
                "results": results,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_progress(
        progress_json,
        {
            "backend": "cosyvoice_sft",
            "speaker": args.speaker,
            "status": "completed" if not failures else "failed",
            "current": total,
            "total": total,
            "percent": 100,
            "failures": failures,
            "output_json": str(args.output_json),
        },
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
