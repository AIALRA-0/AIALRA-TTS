from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import torch
import torchaudio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cosyvoice-root", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--speaker", default="中文男")
    parser.add_argument("--speed", type=float, default=1.0)
    return parser.parse_args()


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
    model = AutoModel(model_dir=str(Path(args.model_dir).resolve()))
    results = []
    failures = []

    with torch.inference_mode():
        for item in payload["segments"]:
            segment_id = int(item["id"])
            text = str(item["text"]).strip()
            out_wav = Path(item["out_wav"])
            if not text:
                continue
            try:
                if out_wav.exists() and out_wav.stat().st_size > 1000:
                    results.append({"id": segment_id, "out_wav": str(out_wav), "skipped": True})
                    continue
                for result in model.inference_sft(text, args.speaker, stream=False, speed=args.speed):
                    speech = result["tts_speech"].detach().cpu()
                    torchaudio.save(str(out_wav), speech, model.sample_rate)
                    break
                if not out_wav.exists() or out_wav.stat().st_size < 1000:
                    raise RuntimeError("CosyVoice produced no usable wav")
                results.append({"id": segment_id, "out_wav": str(out_wav), "skipped": False})
            except Exception as exc:
                failures.append({"id": segment_id, "error": str(exc)[:500]})

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
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
