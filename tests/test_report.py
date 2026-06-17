import json

from ecse_localizer.report import write_video_report


def test_video_report_includes_asr_language_metadata(tmp_path):
    report_md = tmp_path / "report.md"
    report_json = tmp_path / "report.json"
    data = {
        "name": "demo",
        "source_video": "demo.mp4",
        "mode": "smoke",
        "subtitle_source": "ASR",
        "asr_backend": "faster_whisper",
        "asr": {"requested_language": "auto", "backend_language": "auto", "detected_language": "ja", "language_probability": 0.8765},
        "translation_backend": "local_llm",
        "tts": {"backend": "piper"},
        "audio_enhancement": "ffmpeg",
        "qa": {"pass": True, "issues": [], "first_10_subtitles": [], "glossary_sample": []},
        "outputs": {},
    }

    write_video_report(report_md, report_json, data)

    text = report_md.read_text(encoding="utf-8")
    assert "ASR language: requested=auto, backend=auto, detected=ja, probability=0.8765" in text
    assert json.loads(report_json.read_text(encoding="utf-8"))["asr"]["detected_language"] == "ja"
