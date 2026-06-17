from pathlib import Path

from ecse_localizer.subtitle_io import Segment, clean_subtitle_text, read_vtt, split_long_segments, wrap_cjk, write_srt


def test_read_vtt_webex_shape(tmp_path: Path):
    path = tmp_path / "sample.vtt"
    path.write_text(
        "WEBVTT\n\n1 \"Speaker\"\n00:00:00.850 --> 00:00:20.010\nHello class.\n\n",
        encoding="utf-8",
    )
    segments = read_vtt(path)
    assert len(segments) == 1
    assert segments[0].start == 0.850
    assert segments[0].text == "Hello class."


def test_write_srt(tmp_path: Path):
    path = tmp_path / "out.srt"
    segments = read_vtt(tmp_path / "missing.vtt") if False else []
    segments = [Segment(1, 0.0, 1.2, "你好")]
    write_srt(path, segments, cjk=True)
    text = path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,200" in text
    assert "你好" in text


def test_split_long_segments_prefers_sentence_boundaries():
    seg = Segment(
        1,
        0.0,
        18.0,
        "First complete sentence. This second sentence should stay together even if it is a little longer than the target duration.",
    )
    split = split_long_segments([seg], max_duration=8.0, max_chars=60)
    assert len(split) == 2
    assert split[0].text == "First complete sentence."
    assert split[1].text.startswith("This second sentence should stay together")


def test_repair_count_misread_as_timestamp():
    assert clean_subtitle_text("Each wafer will give you 05:43 chips.") == "Each wafer will give you 543 chips."


def test_wrap_cjk_does_not_split_numbers_or_units():
    wrapped = wrap_cjk("目前半导体销售额大约为500亿美元，使用300mm晶圆。", limit=18)
    assert "5\n00" not in wrapped
    assert "300\nmm" not in wrapped
    assert "500" in wrapped
    assert "300mm" in wrapped
