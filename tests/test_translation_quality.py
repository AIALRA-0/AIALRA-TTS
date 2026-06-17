from ecse_localizer.fidelity import heuristic_fidelity_issues
from ecse_localizer.qa import run_qa
from ecse_localizer.repair import repair_chunk, select_repair_ids
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


def test_quality_checks_do_not_apply_chinese_script_rules_to_latin_targets():
    source = (
        "The important point is that the comparator threshold changes with temperature, "
        "so the calibration value must be updated before the next measurement."
    )
    spanish = (
        "El punto importante es que el umbral del comparador cambia con la temperatura, "
        "así que el valor de calibración debe actualizarse antes de la siguiente medición."
    )
    config = {"translation": {"target_language": "es"}}

    assert not is_possibly_overcompressed(source, "corto", config)
    flags = assess_translation_quality(source, spanish, spanish, config)
    assert "HIGH_ASCII_RATIO_TRANSLATION" not in flags
    assert "POSSIBLY_OVERCOMPRESSED_TRANSLATION" not in flags


def test_qa_translation_text_validation_is_target_language_aware():
    source = "The comparator threshold changes with temperature."
    spanish = "El umbral del comparador cambia con la temperatura."
    en = [Segment(1, 0.0, 3.0, source)]
    zh = [Segment(1, 0.0, 3.0, spanish)]
    traces = [TranslationTrace(1, source, spanish, spanish, 3.0, 60, [])]

    qa = run_qa({}, en, zh, {}, traces, {"duration": 3.0, "flags": []}, 3.0, {"translation": {"target_language": "es"}, "qa": {}})

    issue_types = {issue["type"] for issue in qa["issues"]}
    assert "invalid_translation_text" not in issue_types
    assert "possibly_untranslated" not in issue_types
    assert qa["pass"] is True


def test_qa_still_rejects_ascii_only_text_for_chinese_target():
    source = "The comparator threshold changes with temperature."
    en = [Segment(1, 0.0, 3.0, source)]
    zh = [Segment(1, 0.0, 3.0, "The comparator threshold changes with temperature.")]
    traces = [TranslationTrace(1, source, zh[0].text, zh[0].text, 3.0, 60, [])]

    qa = run_qa({}, en, zh, {}, traces, {"duration": 3.0, "flags": []}, 3.0, {"translation": {"target_language": "zh-CN"}, "qa": {}})

    issue_types = {issue["type"] for issue in qa["issues"]}
    assert "invalid_translation_text" in issue_types
    assert "possibly_untranslated" in issue_types
    assert qa["pass"] is False


def test_qa_reports_translation_quality_heuristics():
    en = [Segment(1, 0.0, 3.0, "We compare the two algorithms.")]
    zh = [Segment(1, 0.0, 3.0, "这一段主要围绕两个算法展开。")]
    traces = [TranslationTrace(1, en[0].text, zh[0].text, zh[0].text, 3.0, 16, [])]

    qa = run_qa({}, en, zh, {}, traces, {"duration": 3.0, "flags": []}, 3.0, {"qa": {}})

    issues = [issue for issue in qa["issues"] if issue["type"] == "translation_quality_heuristic"]
    assert issues
    assert issues[0]["severity"] == "high"


def test_qa_summarizes_actionable_trace_flags_and_samples():
    en = [Segment(7, 0.0, 4.0, "Use 3.3V before running sensor_readout.py.")]
    zh = [Segment(7, 0.0, 4.0, "先运行脚本。")]
    traces = [
        TranslationTrace(
            7,
            en[0].text,
            "先使用 3.3V，再运行 sensor_readout.py。",
            "先运行脚本。",
            4.0,
            20,
            ["MISSING_NUMBER:3.3", "MISSING_PROTECTED_TERM:sensor_readout.py", "COHERENCE_PASS"],
            paragraph_id=2,
        )
    ]

    qa = run_qa({}, en, zh, {}, traces, {"duration": 4.0, "flags": []}, 4.0, {"qa": {}})

    assert qa["trace_flags"]["COHERENCE_PASS"] == 1
    assert qa["actionable_trace_flags"]["MISSING_NUMBER:3.3"] == 1
    assert "COHERENCE_PASS" not in qa["actionable_trace_flags"]
    issue = next(item for item in qa["issues"] if item["type"] == "translation_trace_flags")
    assert issue["severity"] == "medium"
    sample = qa["translation_flag_samples"][0]
    assert sample["segment_id"] == 7
    assert sample["paragraph_id"] == 2
    assert sample["flags"] == ["MISSING_NUMBER:3.3", "MISSING_PROTECTED_TERM:sensor_readout.py"]


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


def test_fidelity_heuristics_respect_non_chinese_target_language():
    en = [
        {
            "id": 1,
            "text": (
                "The important point is that the comparator threshold changes with temperature, "
                "so the calibration value must be updated before the next measurement."
            ),
        }
    ]
    es = [
        {
            "id": 1,
            "text": (
                "El punto importante es que el umbral del comparador cambia con la temperatura, "
                "así que el valor de calibración debe actualizarse antes de la siguiente medición."
            ),
        }
    ]

    issues = heuristic_fidelity_issues(en, es, {"translation": {"target_language": "es"}})

    assert not any(issue["type"] == "possibly_overcompressed_translation" for issue in issues)
    assert not any(
        issue["type"] == "translation_quality_heuristic"
        and "HIGH_ASCII_RATIO_TRANSLATION" in issue.get("flags", [])
        for issue in issues
    )


def test_fidelity_repair_accepts_non_chinese_target_language():
    en_segments = [
        Segment(
            1,
            0.0,
            3.0,
            "Set the comparator threshold to 25 mV before the next measurement.",
        )
    ]
    current = [Segment(1, 0.0, 3.0, "Resumen incorrecto.")]

    repairs = repair_chunk(
        FakeRepairClient(),
        "repair prompt",
        en_segments,
        current,
        [1],
        {1: {"score": 2}},
        "",
        {"translation": {"target_language": "es"}},
    )

    assert repairs[0].new_text == "Ajusta el umbral del comparador a 25 mV antes de la siguiente medición."
    assert "FIDELITY_REPAIRED" in repairs[0].flags


class FakeRepairClient:
    def json_chat(self, _system, _user, _schema):
        return {
            "segments": [
                {
                    "id": 1,
                    "zh": "Ajusta el umbral del comparador a 25 mV antes de la siguiente medición.",
                    "flags": [],
                    "notes": "repair missing content",
                }
            ]
        }
