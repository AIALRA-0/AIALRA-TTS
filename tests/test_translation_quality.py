from ecse_localizer.fidelity import heuristic_fidelity_issues
from ecse_localizer.qa import run_qa
from ecse_localizer.repair import select_repair_ids
from ecse_localizer.subtitle_io import Segment
from ecse_localizer.translation_quality import (
    assess_translation_quality,
    is_possibly_overcompressed,
    quality_flag_severity,
)
from ecse_localizer.translate import TranslationTrace


def test_quality_flags_machine_translation_smells():
    source = "This routing layer is used to connect the local interconnects across the block."
    zh = "这个东西被用来去连接块里的local interconnects。"

    flags = assess_translation_quality(source, zh)

    assert "VAGUE_OBJECT_REFERENCE" in flags
    assert "ENGLISH_WORD_ORDER_CALQUE" in flags


def test_quality_flags_summary_style_is_high_severity():
    flags = assess_translation_quality("We compare the two algorithms.", "这一段主要围绕两个算法展开。")

    assert "SUMMARY_STYLE_TRANSLATION" in flags
    assert quality_flag_severity(flags) == "high"


def test_quality_flags_unchanged_literal_rewrite_for_long_segment():
    source = "Today we are going to derive the voltage divider equation and then connect it to sensor readout."
    literal = "今天我们要推导分压器方程，然后把它和传感器读出联系起来。"

    flags = assess_translation_quality(source, literal, literal)

    assert "LECTURE_REWRITE_UNCHANGED_REVIEW_REQUIRED" in flags


def test_overcompressed_translation_detected_conservatively():
    source = (
        "The important point is that the comparator threshold changes with temperature, "
        "so the calibration value must be updated before the next measurement."
    )

    assert is_possibly_overcompressed(source, "阈值会变。")
    assert not is_possibly_overcompressed(source, "关键是比较器阈值会随温度变化，所以在下一次测量前必须更新校准值。")


def test_qa_reports_translation_quality_heuristics():
    en = [Segment(1, 0.0, 3.0, "We compare the two algorithms.")]
    zh = [Segment(1, 0.0, 3.0, "这一段主要围绕两个算法展开。")]
    traces = [TranslationTrace(1, en[0].text, zh[0].text, zh[0].text, 3.0, 16, [])]

    qa = run_qa({}, en, zh, {}, traces, {"duration": 3.0, "flags": []}, 3.0, {"qa": {}})

    issues = [issue for issue in qa["issues"] if issue["type"] == "translation_quality_heuristic"]
    assert issues
    assert issues[0]["severity"] == "high"


def test_qa_reports_tts_alignment_metadata():
    en = [Segment(1, 0.0, 2.0, "First sentence.")]
    zh = [Segment(1, 0.0, 2.0, "第一句。")]
    traces = [TranslationTrace(1, en[0].text, zh[0].text, zh[0].text, 2.0, 16, [])]
    tts_info = {
        "duration": 2.0,
        "flags": [],
        "would_overlap_without_prevention_count": 2,
        "truncated_audio_count": 1,
        "max_audio_delay_seconds": 2.0,
    }

    qa = run_qa({}, en, zh, {}, traces, tts_info, 2.0, {"tts": {"max_audio_delay_warning_seconds": 1.0}, "qa": {}})
    issue_types = {issue["type"] for issue in qa["issues"]}

    assert "tts_audio_overlap_prevented" in issue_types
    assert "tts_audio_truncated" in issue_types
    assert "tts_audio_delay_high" in issue_types


def test_fidelity_heuristics_include_quality_flags_and_repair_selection():
    en = [{"id": 4, "text": "We derive Vout = Vin * R2 / (R1 + R2) and then use sensor_readout.py."}]
    zh = [{"id": 4, "text": "这一段主要围绕两个算法展开。"}]

    issues = heuristic_fidelity_issues(en, zh)
    assert any(issue["type"] == "translation_quality_heuristic" for issue in issues)
    assert any(issue["type"] == "protected_term_mismatch" for issue in issues)

    fidelity = {"reviews": [], "issues": issues}
    assert 4 in select_repair_ids(fidelity, max_score=3, include_high=True)
