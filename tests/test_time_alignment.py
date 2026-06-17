from array import array

from ecse_localizer.align import overlap_count
from ecse_localizer.compact import schedule_compact_units
from ecse_localizer.ffmpeg_utils import audio_duration
from ecse_localizer.subtitle_io import Segment, normalize_segments, write_bilingual_ass
from ecse_localizer.tts import (
    TTSUnit,
    enforce_tts_slot_limit,
    make_tts_units,
    tts_dynamic_target_duration,
    tts_scheduled_start,
    tts_target_duration,
    tts_unconstrained_target_duration,
    write_pcm,
)


def test_normalize_removes_overlap():
    segments = [Segment(1, 0, 2, "a"), Segment(2, 1, 3, "b")]
    normalized = normalize_segments(segments)
    assert overlap_count(normalized) == 0
    assert normalized[1].start > normalized[0].end


def test_clip_to_max_end():
    segments = [Segment(1, 0, 2, "a"), Segment(2, 3, 7, "b")]
    normalized = normalize_segments(segments, max_end=5)
    assert len(normalized) == 2
    assert normalized[-1].end == 5


def test_tts_grouping_merges_short_adjacent_segments():
    segments = [
        Segment(1, 0.0, 4.0, "可以开始了。"),
        Segment(2, 4.0, 8.0, "谢谢刘教授的邀请。"),
        Segment(3, 8.0, 12.0, "我是拉维。"),
    ]
    config = {
        "tts": {
            "align_mode": "grouped",
            "end_gap_seconds": 0.2,
            "merge_gap_seconds": 0.35,
            "min_group_duration": 7.0,
            "max_group_duration": 12.0,
            "max_group_chars": 110,
            "estimated_zh_chars_per_second": 5.2,
            "group_min_estimated_fill_ratio": 0.72,
        }
    }
    units = make_tts_units(segments, config)
    assert len(units) == 1
    assert units[0].segment_ids == [1, 2, 3]


def test_tts_target_duration_respects_next_segment_start():
    config = {"tts": {"end_gap_seconds": 0.2, "prevent_audio_overlap": True, "min_audio_gap_seconds": 0.1}}
    unit = TTSUnit(1, 0.0, 3.0, "第一句。", [1])
    next_unit = TTSUnit(2, 1.5, 3.5, "第二句。", [2])

    assert tts_unconstrained_target_duration(unit, 10.0, config) == 2.8
    assert tts_target_duration(unit, next_unit, 10.0, config) == 1.4


def test_tts_target_duration_keeps_floor_for_tight_overlap():
    config = {"tts": {"end_gap_seconds": 0.2, "prevent_audio_overlap": True, "min_audio_gap_seconds": 0.1}}
    unit = TTSUnit(1, 1.0, 2.0, "第一句。", [1])
    next_unit = TTSUnit(2, 1.05, 2.5, "第二句。", [2])

    assert tts_target_duration(unit, next_unit, 10.0, config) == 0.25


def test_tts_scheduled_start_accounts_for_previous_audio_end():
    config = {"tts": {"prevent_audio_overlap": True, "min_audio_gap_seconds": 0.1}}
    unit = TTSUnit(2, 1.0, 3.0, "第二句。", [2])

    assert tts_scheduled_start(unit, previous_audio_end=1.4, video_duration=10.0, config=config) == 1.5


def test_tts_dynamic_target_duration_shrinks_when_previous_audio_delays_slot():
    config = {
        "tts": {
            "end_gap_seconds": 0.2,
            "prevent_audio_overlap": True,
            "min_audio_gap_seconds": 0.1,
            "shrink_delayed_slots_to_original_timeline": True,
        }
    }
    unit = TTSUnit(2, 1.0, 3.0, "第二句。", [2])
    next_unit = TTSUnit(3, 3.0, 4.0, "第三句。", [3])

    assert tts_target_duration(unit, next_unit, 10.0, config) == 1.8
    assert tts_dynamic_target_duration(unit, next_unit, previous_audio_end=1.4, video_duration=10.0, config=config) == 1.3


def test_enforce_tts_slot_limit_trims_audio_and_flags(tmp_path):
    sample_rate = 22050
    source = tmp_path / "long.wav"
    silence = array("h", [0]) * int(sample_rate * 2.0)
    write_pcm(source, silence, sample_rate)
    flags = []
    unit = TTSUnit(1, 0.0, 0.6, "第一句。", [1])

    trimmed = enforce_tts_slot_limit(source, 0.5, tmp_path, unit, flags, {"tts": {}}, None)

    assert trimmed != source
    assert audio_duration(trimmed) <= 0.55
    assert flags[0]["type"] == "tts_slot_trimmed"
    assert flags[0]["trimmed_seconds"] >= 1.4


def test_enforce_tts_slot_limit_can_be_disabled(tmp_path):
    sample_rate = 22050
    source = tmp_path / "long.wav"
    silence = array("h", [0]) * int(sample_rate * 2.0)
    write_pcm(source, silence, sample_rate)
    flags = []

    same = enforce_tts_slot_limit(
        source,
        0.5,
        tmp_path,
        TTSUnit(1, 0.0, 0.6, "第一句。", [1]),
        flags,
        {"tts": {"trim_overlong_audio_to_slot": False}},
        None,
    )

    assert same == source
    assert flags == []


def test_ass_styles_keep_zh_and_en_on_separate_bands(tmp_path):
    path = tmp_path / "bilingual.ass"
    en = [Segment(1, 0, 2, "This is a short English caption.")]
    zh = [Segment(1, 0, 2, "这是一条中文字幕。")]
    write_bilingual_ass(path, en, zh)
    content = path.read_text(encoding="utf-8-sig")
    assert "Style: ZhMain" in content
    assert "Style: EnSub" in content
    assert "ZhMain" in content and ", 178, 1" in content
    assert "EnSub" in content and ", 72, 1" in content


def test_compact_schedule_prevents_audio_overlap(tmp_path):
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()
    sample_rate = 22050
    silence = array("h", [0]) * int(sample_rate * 2.0)
    write_pcm(tts_dir / "seg_00001_pcm.wav", silence, sample_rate)
    write_pcm(tts_dir / "seg_00002_pcm.wav", silence, sample_rate)
    units = [
        TTSUnit(1, 0.0, 1.0, "第一句。", [1]),
        TTSUnit(2, 1.0, 2.0, "第二句。", [2]),
    ]
    config = {
        "tts": {
            "compact_schedule_mode": "source_anchored",
            "prevent_audio_overlap": True,
            "min_audio_gap_seconds": 0.1,
        }
    }
    scheduled = schedule_compact_units(units, tts_dir, 10.0, config)
    assert scheduled[1].scheduled_start >= scheduled[0].scheduled_end + 0.1


def test_compact_schedule_default_caps_long_gaps(tmp_path):
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()
    sample_rate = 22050
    silence = array("h", [0]) * int(sample_rate * 1.0)
    write_pcm(tts_dir / "seg_00001_pcm.wav", silence, sample_rate)
    write_pcm(tts_dir / "seg_00002_pcm.wav", silence, sample_rate)
    units = [
        TTSUnit(1, 0.0, 1.0, "第一句。", [1]),
        TTSUnit(2, 8.0, 9.0, "第二句。", [2]),
    ]
    config = {
        "tts": {
            "compact_min_gap_seconds": 0.22,
            "compact_distributed_max_gap_seconds": 2.0,
            "prevent_audio_overlap": True,
            "min_audio_gap_seconds": 0.08,
        }
    }

    scheduled = schedule_compact_units(units, tts_dir, 10.0, config)

    gap = scheduled[1].scheduled_start - scheduled[0].scheduled_end
    assert gap <= 2.0
    assert scheduled[1].scheduled_start < units[1].start
