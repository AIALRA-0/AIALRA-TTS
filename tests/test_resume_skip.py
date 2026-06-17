import json
from pathlib import Path

from ecse_localizer.cli import completed_report_for
from ecse_localizer.utils import slugify


def test_completed_report_for_requires_pass_and_outputs(tmp_path):
    video = tmp_path / "Lecture One.mp4"
    video.write_bytes(b"placeholder")
    output = tmp_path / "out"
    output.mkdir()
    files = {}
    for key, suffix in {
        "en_srt": "_en.srt",
        "zh_srt": "_zh.srt",
        "bilingual_srt": "_bilingual.srt",
        "bilingual_ass": "_bilingual.ass",
        "zh_dub_wav": "_zh_dub.wav",
        "zh_dub_mp4": "_zh_dub.mp4",
    }.items():
        path = output / f"Lecture_One{suffix}"
        path.write_text("ok", encoding="utf-8")
        files[key] = str(path)
    report = output / f"{slugify(video.name)}_report.json"
    report.write_text(json.dumps({"mode": "full", "qa": {"pass": True}, "outputs": files}), encoding="utf-8")
    assert completed_report_for(video, output) == report


def test_completed_report_for_rejects_failed_report(tmp_path):
    video = tmp_path / "Lecture One.mp4"
    output = tmp_path / "out"
    output.mkdir()
    report = output / f"{slugify(video.name)}_report.json"
    report.write_text(json.dumps({"mode": "full", "qa": {"pass": False}, "outputs": {}}), encoding="utf-8")
    assert completed_report_for(video, output) is None
