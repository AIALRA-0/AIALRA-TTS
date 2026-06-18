import json
import re

from ecse_localizer.subtitle_io import Segment
from ecse_localizer.translate import (
    apply_known_term_corrections,
    build_translation_paragraphs,
    coherence_rejection_flags,
    context_window,
    default_style_guide,
    fallback_translate_text,
    normalize_translation,
    numbers_missing,
    paragraph_lookup,
    protected_term_flags,
    quality_requirements,
    restore_and_repair_protected_terms,
    request_llm_chunk,
    use_best_quality,
)


def test_best_quality_defaults_enabled():
    config = {"translation": {"quality_mode": "best_quality", "style": "natural_chinese_lecture"}}
    assert use_best_quality(config)
    assert "不要像逐词翻译" in default_style_guide(config)
    assert any("Do not omit" in item for item in quality_requirements(config))


def test_best_quality_style_guide_is_target_language_aware():
    config = {"translation": {"quality_mode": "best_quality", "target_language": "es", "style": "natural_lecture"}}

    guide = default_style_guide(config)
    requirements = quality_requirements(config)

    assert "requested target language (es)" in guide
    assert "中文表达" not in guide
    assert any("target-language lecture wording" in item for item in requirements)
    assert not any("Chinese lecture wording" in item for item in requirements)


def test_context_window_keeps_neighbor_segments_only():
    segments = [Segment(i, float(i), float(i + 1), f"text {i}") for i in range(1, 6)]
    before = context_window(segments, 2, -2)
    after = context_window(segments, 2, 2)

    assert [row["id"] for row in before] == [1, 2]
    assert [row["id"] for row in after] == [4, 5]


def test_translation_paragraphs_reconstruct_fragmented_speech():
    config = {
        "translation": {
            "paragraph_max_gap_seconds": 1.0,
            "paragraph_max_source_chars": 240,
            "paragraph_max_duration_seconds": 30,
            "paragraph_min_segments_before_sentence_break": 2,
        }
    }
    segments = [
        Segment(1, 0.0, 1.0, "Today we are going to talk about"),
        Segment(2, 1.1, 2.0, "finite state machines."),
        Segment(3, 2.2, 3.0, "The key point is"),
        Segment(4, 3.1, 4.0, "state transitions."),
        Segment(5, 8.0, 9.0, "Now let's switch topics."),
    ]

    paragraphs = build_translation_paragraphs(segments, config)
    assert [p.segment_ids for p in paragraphs] == [[1, 2], [3, 4], [5]]
    assert paragraphs[0].text == "Today we are going to talk about finite state machines."

    lookup = paragraph_lookup(paragraphs)
    assert lookup[1].id == lookup[2].id
    assert lookup[4].text == "The key point is state transitions."
    assert lookup[5].segment_ids == [5]


def test_translation_paragraphs_respect_source_char_limit():
    config = {
        "translation": {
            "paragraph_max_gap_seconds": 2.0,
            "paragraph_max_source_chars": 25,
            "paragraph_max_duration_seconds": 30,
            "paragraph_min_segments_before_sentence_break": 10,
        }
    }
    segments = [
        Segment(1, 0.0, 1.0, "short fragment"),
        Segment(2, 1.1, 2.0, "another short fragment"),
        Segment(3, 2.1, 3.0, "third fragment"),
    ]

    paragraphs = build_translation_paragraphs(segments, config)
    assert [p.segment_ids for p in paragraphs] == [[1], [2], [3]]


def test_llm_chunk_payload_includes_reconstructed_paragraph_context():
    config = {"translation": {"max_zh_chars_per_second": 8, "max_zh_chars_per_subtitle_line": 22}}
    segments = [
        Segment(1, 0.0, 1.0, "We start with"),
        Segment(2, 1.1, 2.0, "the voltage divider."),
    ]
    paragraphs = build_translation_paragraphs(
        segments,
        {"translation": {"paragraph_min_segments_before_sentence_break": 2}},
    )
    fake = FakeLLMClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        paragraph_lookup(paragraphs),
    )

    literal_payload = fake.calls[0]
    assert literal_payload["segments"][0]["paragraph_segment_ids"] == [1, 2]
    assert literal_payload["segments"][0]["paragraph_text"] == "We start with the voltage divider."
    assert [row[0].id for row in results] == [1, 2]


def test_llm_chunk_flags_missing_protected_terms():
    config = {"translation": {"max_zh_chars_per_second": 80, "max_zh_chars_per_subtitle_line": 80}}
    segments = [
        Segment(
            1,
            0.0,
            2.0,
            "Use Vout = Vin * R2 / (R1 + R2), sensor_readout.py, and https://example.com.",
        )
    ]
    fake = FakeLLMClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    flags = results[0][3]
    missing_flags = [flag for flag in flags if flag.startswith("MISSING_PROTECTED_TERM:")]
    assert missing_flags
    assert "Vout = Vin * R2 / (R1 + R2)" in missing_flags[0]
    assert "sensor_readout.py" in missing_flags[0]
    assert "https://example.com" in missing_flags[0]


def test_coherence_pass_cannot_drop_protected_placeholders():
    config = {"translation": {"max_zh_chars_per_second": 80, "max_zh_chars_per_subtitle_line": 80}}
    segments = [
        Segment(
            1,
            0.0,
            2.0,
            "Use Vout = Vin * R2 / (R1 + R2) before running sensor_readout.py.",
        )
    ]
    fake = FakeCoherenceDropsProtectedClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "coherence",
        {},
    )

    _, literal, zh, flags, _ = results[0]
    assert "Vout=Vin*R2/(R1+R2)" in literal
    assert "Vout=Vin*R2/(R1+R2)" in zh
    assert "sensor_readout.py" in zh
    assert "COHERENCE_REJECTED_FIDELITY_GUARD" in flags
    assert not any(flag.startswith("MISSING_PROTECTED_TERM") for flag in flags)


def test_coherence_rejects_short_source_overexpansion():
    flags = coherence_rejection_flags(
        "Okay.",
        "好的。",
        "我仍然不明白为什么这不起作用。",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "COHERENCE_REJECTED_SHORT_SOURCE_GUARD" in flags
    assert "COHERENCE_SHORT_SOURCE_OVEREXPANDED" in flags


def test_coherence_rejects_neighbor_literal_leak():
    flags = coherence_rejection_flags(
        "I still don't understand why this doesn't work.",
        "我还是不明白为什么这不起作用。",
        "今天真是奇怪的一天。",
        {"translation": {"target_language": "zh-CN"}},
        literal_zh="我还是不明白为什么这不起作用。",
        neighbor_literal_zh=["今天真是奇怪的一天。"],
    )

    assert "COHERENCE_REJECTED_NEIGHBOR_LEAK" in flags
    assert "COHERENCE_INCLUDED_NEIGHBOR_LITERAL" in flags


def test_coherence_rejects_partial_neighbor_literal_leak():
    flags = coherence_rejection_flags(
        "A guidebook that was in the industry and people started using and cause he used,",
        "一本行业内的指南书，人们开始使用，并且因为他用了它。",
        "一本行业内的指南书，人们开始使用，并且因为他用了它。他注意到他在这一阶段收集了大量这些数据。",
        {"translation": {"target_language": "zh-CN"}},
        literal_zh="一本行业内的指南书，人们开始使用，并且因为他用了它。",
        neighbor_literal_zh=["他注意到他在这一阶段收集了大量这些数据，并且他发现有。"],
    )

    assert "COHERENCE_REJECTED_NEIGHBOR_LEAK" in flags
    assert "COHERENCE_INCLUDED_NEIGHBOR_LITERAL" in flags


def test_coherence_allows_normal_short_source_polish():
    flags = coherence_rejection_flags(
        "Okay, let's start.",
        "好的，我们开始吧。",
        "好，我们开始。",
        {"translation": {"target_language": "zh-CN"}},
        literal_zh="好的，我们开始吧。",
        neighbor_literal_zh=["今天真是奇怪的一天。"],
    )

    assert "COHERENCE_REJECTED_SHORT_SOURCE_GUARD" not in flags
    assert "COHERENCE_REJECTED_NEIGHBOR_LEAK" not in flags


def test_llm_chunk_keeps_good_rows_when_one_row_is_unusable():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(1, 0.0, 1.0, "Okay."),
        Segment(2, 1.0, 4.0, "Statistical process control reduces variation."),
    ]
    fake = FakeOneBadRowClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert len(results) == 2
    assert results[0][2] == "好的。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[0][3]
    assert results[1][2] == "统计过程控制可以减少波动。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" not in results[1][3]


def test_llm_chunk_rejects_short_source_overexpanded_row():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(1, 0.0, 1.0, "Over 40 years."),
        Segment(2, 1.0, 4.0, "Statistical process control reduces variation."),
    ]
    fake = FakeShortSourceOverexpandedClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert results[0][2] == "超过40年。"
    assert "LLM_ROW_UNUSABLE_LITERAL_FALLBACK" in results[0][3]
    assert results[1][2] == "统计过程控制可以减少波动。"


def test_llm_chunk_rejects_rewrite_that_includes_neighbor_literal():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(1, 0.0, 3.0, "I was working with SPC charts."),
        Segment(2, 3.0, 6.0, "The next job was about MRAM tools."),
    ]
    fake = FakeNeighborLeakRewriteClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert results[0][2] == "我当时在使用SPC图表。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[0][3]
    assert results[1][2] == "下一份工作是关于MRAM工具的。"


def test_llm_chunk_rejects_neighbor_leak_after_protected_terms_restore():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(1, 0.0, 3.0, "I was working with IBM charts."),
        Segment(2, 3.0, 6.0, "The next job used MRAM tools."),
    ]
    fake = FakeProtectedNeighborLeakRewriteClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert results[0][2] == "我当时在IBM处理图表。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[0][3]
    assert results[1][2] == "下一份工作使用MRAM工具。"


def test_llm_chunk_rejects_partial_neighbor_literal_leak():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(33, 0.0, 3.0, "A guidebook that was in the industry and people started using and cause he used,"),
        Segment(34, 3.0, 6.0, "he noticed that he he was collecting a lot of this data during his 1st part of this and he noticed there was,"),
    ]
    fake = FakePartialNeighborLeakRewriteClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert results[0][2] == "一本行业内的指南书，人们开始使用，并且因为他用了它。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[0][3]
    assert results[1][2] == "他注意到，自己在第一阶段收集了大量这类数据，并且发现..."


def test_llm_chunk_rejects_short_source_with_next_two_segment_leak():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(136, 0.0, 2.0, "The Japanese just took it as a philosophy."),
        Segment(137, 2.0, 4.0, "They just basically, every industry,"),
        Segment(138, 4.0, 6.0, "not just the auto industry, every industry,"),
    ]
    fake = FakeJapanesePhilosophyLeakClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert results[0][2] == "日本人只是将其作为一种哲学来接受。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[0][3]


def test_llm_chunk_rejects_context_only_phrase_leaks():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(137, 0.0, 2.0, "They just basically, every industry,"),
        Segment(138, 2.0, 4.0, "not just the auto industry, every industry,"),
        Segment(139, 4.0, 6.0, "they started using this as just their core process"),
    ]
    fake = FakeContextOnlyPhraseLeakClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    assert "汽车行业" not in results[0][2]
    assert "不仅仅" not in results[0][2]
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[0][3]
    assert "核心流程" not in results[1][2]
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in results[1][3]


def test_non_chinese_target_translation_is_accepted_and_keeps_spaces():
    config = {
        "translation": {
            "target_language": "es",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [
        Segment(
            1,
            0.0,
            2.0,
            "Use Vout = Vin * R2 / (R1 + R2) before running sensor_readout.py.",
        )
    ]
    fake = FakeSpanishLLMClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    _, literal, lecture, flags, _ = results[0]
    assert literal.startswith("Primero usamos ")
    assert lecture.startswith("Primero usamos ")
    assert " " in lecture
    assert "Vout = Vin * R2 / (R1 + R2)" in lecture
    assert "sensor_readout.py" in lecture
    assert "HIGH_ASCII_RATIO_TRANSLATION" not in flags
    assert not any(flag.startswith("MISSING_PROTECTED_TERM") for flag in flags)


def test_known_spc_asr_name_confusions_are_corrected():
    text = "因此，在1938年，Suehart与WEdwardDimming合作。休哈特与W·爱德华·戴明合作。后来Stuart and Dimming推广了SVC图表。战后的日本受到了斯图尔特和迪明的启发。"

    corrected = apply_known_term_corrections(
        text,
        "So in 1938 Suehart worked with WEdwardDimming. Stuart and Dimming used SVC charts.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "Shewhart" in corrected
    assert "W. Edwards Deming" in corrected
    assert "Dimming" not in corrected
    assert "Stuart and" not in corrected
    assert "斯图尔特" not in corrected
    assert "迪明" not in corrected
    assert "休哈特" not in corrected
    assert "戴明" not in corrected
    assert "SPC图表" in corrected


def test_known_spc_asr_deming_variants_are_corrected():
    corrected = apply_known_term_corrections(
        "Shewhart与W.EdwardDeming合作。后面写成W.EdwardsDeming。作为Dimming的14点之一。",
        "So in 1938, Shewhart collaborated with W Edward Dimming. Dimming's 14 points.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "W. Edwards Deming" in corrected
    assert "W.EdwardsDeming" not in corrected
    assert "Deming的14点" in corrected
    assert "Dimming" not in corrected


def test_known_spc_three_sigma_dimming_is_corrected():
    corrected = apply_known_term_corrections(
        "这是三个标准差范围，这是Dimming说的。",
        "This is three Sigma, this is what Dimming said.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "Deming" in corrected
    assert "Dimming" not in corrected


def test_known_spc_stewart_japanese_quality_context_is_corrected():
    corrected = apply_known_term_corrections(
        "我认为这一切都来自他们。你知道斯图尔特，他们从那里开始并继续发展。",
        "The Japanese took the quality philosophy. You know Stewart, they started from there and ran with it.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "Shewhart" in corrected
    assert "斯图尔特" not in corrected


def test_known_spc_you_know_stewart_phrase_is_corrected():
    corrected = apply_known_term_corrections(
        "我认为这一切都来自他们。你知道斯图尔特，他们从那里开始并继续发展。",
        "I think it all came from them and you know Stewart, they started from there and ran with it.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "Shewhart" in corrected
    assert "斯图尔特" not in corrected


def test_known_spc_you_know_stewart_phrase_fixes_awareness_mistranslation():
    corrected = apply_known_term_corrections(
        "我认为这一切都来自他们。Shewhart也知道，他们从那里开始并继续发展。",
        "I think it all came from them and you know Stewart, they started from there and ran with it.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "比如Shewhart" in corrected
    assert "Shewhart也知道" not in corrected


def test_chinese_normalization_preserves_spaces_inside_latin_names():
    normalized = normalize_translation(
        "因此，在1938年，Shewhart 与 W. Edwards Deming 合作。",
        {"translation": {"target_language": "zh-CN"}},
        "So in 1938, Suehart collaborated with W Edward Dimming.",
    )

    assert "W. Edwards Deming" in normalized
    assert "W.EdwardsDeming" not in normalized
    assert "Shewhart与" in normalized


def test_known_spc_control_variants_are_corrected_without_missing_term_flag():
    corrected = apply_known_term_corrections(
        "这里有很多SBC控制和SVC控制方法。许多制造商采用更多统计过程控制（SBC）。你用来做图表的SBC。",
        "There is a lot out there for SBC control and SVC control. Manufacturers incorporated more SBC. Using your SBC to do your charts.",
        {"translation": {"target_language": "zh-CN"}},
    )

    assert "SPC控制" in corrected
    assert "SBC" not in corrected
    assert "SVC" not in corrected
    assert protected_term_flags("SBC control and SVC charts", "SPC控制和SPC图表") == []
    assert protected_term_flags("Start to eliminate some of the SBC controls.", "开始减少一些SPC控制措施。") == []
    assert protected_term_flags("A robust process means less worry about the SVC.", "稳健工艺意味着不用太担心SPC。") == []
    assert protected_term_flags("Their process rules and your SBC.", "它们的工艺规则和你的SPC。") == []
    assert protected_term_flags("That you're using for your SBC to do your charts.", "你用来做SPC图表。") == []
    assert protected_term_flags("That you're using for your SBC to do your charts.", "你用来做图表的SPC到实际硬件。") == []
    assert protected_term_flags(
        "A lot of the US manufacturers started incorporating more SBC.",
        "许多美国制造商开始采用更多统计过程控制方法（SPC）。",
    ) == []
    assert protected_term_flags(
        "SVC should be implemented for the key performance indicators, the KPIs of the design established in the PDK, the process design kit.",
        "SPC图表应该针对设计中在PDK中建立的关键性能指标（KPIs）实施。",
    ) == []

    kpi_corrected = apply_known_term_corrections(
        "SVC应该为设计中在PDK中建立的关键性能指标（KPIs）实施。",
        "SVC should be implemented for the key performance indicators, the KPIs of the design established in the PDK, the process design kit.",
        {"translation": {"target_language": "zh-CN"}},
    )
    assert "SPC" in kpi_corrected
    assert "SVC" not in kpi_corrected

    natural_kpi = normalize_translation(
        "SPC应该为设计中在PDK中建立的关键性能指标（KPIs）实施。",
        {"translation": {"target_language": "zh-CN"}},
        "SVC should be implemented for the key performance indicators, the KPIs of the design established in the PDK, the process design kit.",
    )
    assert natural_kpi == "对PDK中为设计定义的关键性能指标（KPI），也应该实施SPC。"

    natural_kpi_before_spc_fix = normalize_translation(
        "SVC应该为设计中在PDK中建立的关键性能指标（KPIs）实施。",
        {"translation": {"target_language": "zh-CN"}},
        "SVC should be implemented for the key performance indicators, the KPIs of the design established in the PDK, the process design kit.",
    )
    assert natural_kpi_before_spc_fix == "对PDK中为设计定义的关键性能指标（KPI），也应该实施SPC。"


def test_us_manufacturer_spc_translation_does_not_keep_misplaced_us_parenthetical():
    source = "So, a lot of the US manufacturers started incorporating more SBC."
    normalized = normalize_translation(
        "因此，许多美国制造商开始采用更多的统计过程控制（US）方法（SPC）。",
        {"translation": {"target_language": "zh-CN"}},
        source,
    )

    assert "美国制造商" in normalized
    assert "SPC" in normalized
    assert "（US）" not in normalized
    assert "(US)" not in normalized
    assert protected_term_flags(source, normalized) == []


def test_spc_chart_to_actual_hardware_phrase_is_rewritten_as_natural_lecture_zh():
    source = (
        "That you're using for your SBC to do your charts to the actual hardware "
        "that the wafers are seeing, the actual processes."
    )
    normalized = normalize_translation(
        "你用来做你的SPC图表到晶圆实际看到的硬件，以及实际的过程。",
        {"translation": {"target_language": "zh-CN"}},
        source,
    )

    assert normalized == "也就是把你用于制作SPC图表的内容，对应到晶圆实际经过的硬件和真实工艺。"
    assert protected_term_flags(source, normalized) == []

    variant = normalize_translation(
        "你用来做图表的SPC到晶圆实际看到的实际硬件和工艺。",
        {"translation": {"target_language": "zh-CN"}},
        source,
    )
    assert variant == "也就是把你用于制作SPC图表的内容，对应到晶圆实际经过的硬件和真实工艺。"

    contact_variant = normalize_translation(
        "你用来做图表的SPC到晶圆实际接触的硬件，实际工艺流程。",
        {"translation": {"target_language": "zh-CN"}},
        source,
    )
    assert contact_variant == "也就是把你用于制作SPC图表的内容，对应到晶圆实际经过的硬件和真实工艺。"


def test_generated_spc_placeholder_is_repaired():
    normalized = normalize_translation(
        "你必须要有<SPC_003>控制和关键参数指标KPI。",
        {"translation": {"target_language": "zh-CN"}},
        "You absolutely have to have SBC control and the key parameter indicators, a KPI.",
    )

    assert "<SPC_" not in normalized
    assert "SPC控制" in normalized
    assert protected_term_flags(
        "You absolutely have to have SBC control and the key parameter indicators, a KPI.",
        normalized,
    ) == []


def test_hundred_thousand_phrase_is_not_normalized_to_ten_thousand():
    normalized = normalize_translation(
        "晶体管数量超过了一万。",
        {"translation": {"target_language": "zh-CN"}},
        "By then the transistor count was over a hundred thousand.",
    )

    assert "超过十万" in normalized
    assert "一万" not in normalized

    awkward = normalize_translation(
        "晶体管数量超过了一十万。",
        {"translation": {"target_language": "zh-CN"}},
        "By then the transistor count was over a hundred thousand.",
    )

    assert "超过十万" in awkward
    assert "一十万" not in awkward


def test_spc_process_rules_stutter_is_removed():
    source = (
        "I mean, and a company might not call it western Electric anymore, "
        "but there's their process rules and your your your SBC."
    )
    normalized = normalize_translation(
        "我的意思是，一家公司可能不再叫它西方电气了，但它们有自己的工艺规则和你的你的你的SPC。",
        {"translation": {"target_language": "zh-CN"}},
        source,
    )

    assert "你的你的" not in normalized
    assert "工艺规则，也有你的SPC" in normalized
    assert protected_term_flags(source, normalized) == []


def test_fractional_percent_is_normalized_for_zh_subtitles():
    normalized = normalize_translation(
        "有大约.3%的可能性，这可能是假阳性或假阴性。",
        {"translation": {"target_language": "zh-CN"}},
        "There's a .3 % chance that it could be a false positive.",
    )

    assert "0.3%" in normalized
    assert not re.search(r"(?<!\d)[.．]\s*3\s*%", normalized)


def test_rule_fallback_translates_data_sampling_phrase():
    zh, flags = fallback_translate_text("So data, so data sampling.", {})

    assert "数据采样" in zh
    assert "所以数据，所以" not in zh
    assert "data" not in zh.lower()
    assert "HIGH_ASCII_RATIO_RULE_TRANSLATION" not in flags


def test_data_sampling_stutter_is_naturalized_after_llm_output():
    normalized = normalize_translation(
        "所以数据采集，进行数据采样。",
        {"translation": {"target_language": "zh-CN"}},
        "So data, so data sampling.",
    )

    assert normalized == "所以，也就是数据采样。"

    truncated = normalize_translation(
        "所以数据采集...",
        {"translation": {"target_language": "zh-CN"}},
        "So data, so data sampling.",
    )

    assert truncated == "所以，也就是数据采样。"


def test_ordinal_protected_token_not_appended_when_translated():
    restored = restore_and_repair_protected_terms(
        "当我第一次进入这个行业时，日本人在这方面把我们远远甩在后面。",
        {"<KEEP_001>": "1st"},
        "when I 1st started in the industry in the early eighties",
    )

    assert "第一次" in restored
    assert "1st" not in restored

    first_transition = restore_and_repair_protected_terms(
        "首先，我们要讲的是历史部分。",
        {"<KEEP_001>": "1st"},
        "First, this is the 1st history part.",
    )
    assert "1st" not in first_transition

    assert numbers_missing("during his 1st part of this", "在他的第一部分工作中") == []
    assert protected_term_flags("during his 1st part of this", "在他的第一部分工作中") == []
    assert numbers_missing("during his 1st part of this", "在第一阶段收集了大量数据") == []
    assert protected_term_flags("during his 1st part of this", "在第一阶段收集了大量数据") == []
    assert numbers_missing("Put your customers 1st.", "把客户放在第一位。") == []
    assert protected_term_flags("Put your customers 1st.", "把客户放在第一位。") == []
    assert numbers_missing("at 1st when you're starting off", "在新产品初期") == []
    assert protected_term_flags("at 1st when you're starting off", "在新产品初期") == []

    initial_phase = restore_and_repair_protected_terms(
        "但在新产品初期，你可能需要坚持单一供应商。",
        {"<KEEP_001>": "1st"},
        "but it was at 1st when you're starting off with new products",
    )
    assert "1st" not in initial_phase

    first_part = normalize_translation(
        "如果有的话（1st）。",
        {"translation": {"target_language": "zh-CN"}},
        "he noticed that he he was collecting a lot of this data during his 1st part of this and he noticed there was,",
    )

    assert first_part == "他注意到，自己在第一阶段收集了大量这类数据，并且发现..."
    assert "1st" not in first_part


def test_yield_range_spc_controls_has_natural_percent_phrase():
    normalized = normalize_translation(
        "当良率达到一定范围时，你就会开始减少一些SPC控制措施（90）。",
        {"translation": {"target_language": "zh-CN"}},
        "maybe the 90 % yield range, then you'll start to eliminate a lot of these some of the the SBC controls,",
    )

    assert normalized == "当良率达到90%左右时，你就会开始减少一些SPC控制措施。"


def test_decade_numbers_do_not_trigger_missing_number_flags():
    assert numbers_missing("Back in the 1970s, quality improved.", "在20世纪70年代，质量提高了。") == []
    assert numbers_missing("Back in the 1970s, quality improved.", "上世纪70年代，质量提高了。") == []
    assert numbers_missing("So in the 1920s.", "所以到了20世纪20年代。") == []
    assert numbers_missing("In the 1980s, transistor counts grew.", "上世纪八十年代，晶体管数量增长。") == []
    assert numbers_missing("There is a .3% chance.", "有0.3%的可能性。") == []
    assert numbers_missing("Close to 6 million telephones.", "接近600万部电话。") == []
    assert numbers_missing("Use 3.3V.", "使用电压。") == ["3.3"]


def test_million_telephone_quantity_is_natural_chinese():
    normalized = normalize_translation(
        "6百万电话被实际处理过，并且到七十年代时，超过90%的家庭拥有这种旋转装置。",
        {"translation": {"target_language": "zh-CN"}},
        "close to 6 million telephones that were actually processed.",
    )

    assert "600万部电话" in normalized
    assert "6百万电话" not in normalized

    faithful = normalize_translation(
        "接近600万部电话实际被处理了，到后来，在七十年代有超过90%的家庭使用这种旋转式电话，到了八十年代这个比例超过了90%。",
        {"translation": {"target_language": "zh-CN"}},
        "close to 6 million telephones that were actually processed and by the end there was over 90 % of like by the seventies over 90 % of the households had this rotary.",
    )

    assert faithful == "接近600万部电话被实际处理；到七十年代，超过90%的家庭都使用这种旋转式电话。"
    assert "八十年代" not in faithful


def test_restore_repairs_unresolved_keep_placeholders_from_source_mapping():
    restored = restore_and_repair_protected_terms(
        "接近<KEEP_001>百万电话，超过<KEEP_002>%家庭。",
        {},
        "There were nearly 30 million telephones and over 70% of homes.",
    )

    assert "<KEEP_" not in restored
    assert "30" in restored
    assert "70%" in restored


def test_llm_chunk_rejects_extra_unresolved_keep_placeholder_leak():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    segments = [Segment(27, 0.0, 1.0, "back in the twenties, there was,")]
    fake = FakeExtraPlaceholderLeakClient()

    results = request_llm_chunk(
        segments,
        0,
        segments,
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    _, _, lecture, flags, _ = results[0]
    assert "<KEEP_" not in lecture
    assert lecture == "在二十年代，有过。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in flags


def test_llm_chunk_rejects_known_name_leak_from_next_segment():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    all_segments = [
        Segment(36, 0.0, 3.0, "If the control charts were within the three signal limits, that typically was quality enough."),
        Segment(37, 3.0, 6.0, "So in 1938, Suehart collaborated with W Edward Dimming."),
    ]
    fake = FakeKnownNameLeakClient()

    results = request_llm_chunk(
        all_segments,
        0,
        [all_segments[0]],
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    _, _, lecture, flags, _ = results[0]
    assert "Shewhart" not in lecture
    assert "Deming" not in lecture
    assert lecture == "如果控制图在三个信号限制内，通常就足以保证质量。"
    assert "LLM_ROW_UNUSABLE_LECTURE_FALLBACK" in flags


def test_llm_chunk_rejects_chinese_known_name_leak_after_normalization():
    config = {
        "translation": {
            "target_language": "zh-CN",
            "max_zh_chars_per_second": 80,
            "max_zh_chars_per_subtitle_line": 80,
        }
    }
    all_segments = [
        Segment(36, 0.0, 3.0, "If the control charts were within the three signal limits, that typically was quality enough."),
        Segment(37, 3.0, 6.0, "So in 1938, Suehart collaborated with W Edward Dimming."),
    ]
    fake = FakeChineseKnownNameLeakClient()

    results = request_llm_chunk(
        all_segments,
        0,
        [all_segments[0]],
        "",
        config,
        fake,
        "literal",
        "rewrite",
        "style",
        "",
        {},
    )

    _, _, lecture, flags, _ = results[0]
    assert "Shewhart" not in lecture
    assert "Deming" not in lecture
    assert lecture == "如果控制图在三个信号限制内，通常就足以保证质量。"
    assert {"LLM_ROW_CONTEXT_ONLY_KNOWN_TERM_FALLBACK", "LLM_ROW_UNUSABLE_LECTURE_FALLBACK"} & set(flags)


def test_sanitize_flags_drops_protected_placeholder_flags():
    from ecse_localizer.translate import sanitize_flags

    assert sanitize_flags(["KEEP_001", "<KEEP_002>", "COHERENCE_PASS"]) == ["COHERENCE_PASS"]


class FakeLLMClient:
    model = "qwen2.5:14b-instruct"

    def __init__(self):
        self.calls = []

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        self.calls.append(payload)
        ids = [int(row["id"]) for row in payload["segments"]]
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": f"忠实翻译{sid}", "flags": []} for sid in ids]}
        return {"segments": [{"id": sid, "zh_lecture": f"自然讲解{sid}", "flags": []} for sid in ids]}


class FakeCoherenceDropsProtectedClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        segment = payload["segments"][0]
        sid = int(segment["id"])
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": "先使用<KEEP_001>，再运行<KEEP_002>。", "flags": []}]}
        if "more coherent" in schema:
            return {"segments": [{"id": sid, "zh_lecture": "先使用分压器公式，再运行脚本。", "flags": ["COHERENCE_REWRITE"]}]}
        return {"segments": [{"id": sid, "zh_lecture": "先使用<KEEP_001>，再运行<KEEP_002>。", "flags": []}]}


class FakeSpanishLLMClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        sid = int(payload["segments"][0]["id"])
        if "zh_literal" in schema:
            return {
                "segments": [
                    {
                        "id": sid,
                        "zh_literal": "Primero usamos <KEEP_001> antes de ejecutar <KEEP_002>.",
                        "flags": [],
                    }
                ]
            }
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": "Primero usamos <KEEP_001> y luego ejecutamos <KEEP_002>.",
                    "flags": [],
                }
            ]
        }


class FakeExtraPlaceholderLeakClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        if "segments" not in payload:
            return {"id": 27, "zh_literal": "在二十年代，有过。", "zh_lecture": "在二十年代，有过。", "flags": []}
        sid = int(payload["segments"][0]["id"])
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": "在二十年代，有过。", "flags": []}]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": "在二十年代的时候，有接近<KEEP_001>百万电话。",
                    "flags": [],
                }
            ]
        }


class FakeKnownNameLeakClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        if "segments" not in payload:
            return {
                "id": 36,
                "zh_literal": "如果控制图在三个信号限制内，通常就足以保证质量。",
                "zh_lecture": "如果控制图在三个信号限制内，通常就足以保证质量。",
                "flags": [],
            }
        sid = int(payload["segments"][0]["id"])
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": "如果控制图在三个信号限制内，通常就足以保证质量。", "flags": []}]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": "如果控制图在三个信号限制内，通常就足以保证质量。1938年，Shewhart与W.EdwardsDeming合作。",
                    "flags": [],
                }
            ]
        }


class FakeChineseKnownNameLeakClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        if "segments" not in payload:
            return {
                "id": 36,
                "zh_literal": "如果控制图在三个信号限制内，通常就足以保证质量。",
                "zh_lecture": "如果控制图在三个信号限制内，通常就足以保证质量。",
                "flags": [],
            }
        sid = int(payload["segments"][0]["id"])
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": "如果控制图在三个信号限制内，通常就足以保证质量。", "flags": []}]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": "如果控制图在三个信号限制内，通常就足以保证质量。因此，在1938年，休哈特与W·爱德华·戴明合作。",
                    "flags": [],
                }
            ]
        }


class FakeOneBadRowClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        ids = [int(row["id"]) for row in payload["segments"]]
        if "zh_literal" in schema:
            return {
                "segments": [
                    {"id": sid, "zh_literal": "好的。" if sid == 1 else "统计过程控制可以减少波动。", "flags": []}
                    for sid in ids
                ]
            }
        return {
            "segments": [
                {"id": sid, "zh_lecture": "（NO）" if sid == 1 else "统计过程控制可以减少波动。", "flags": []}
                for sid in ids
            ]
        }


class FakeShortSourceOverexpandedClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        if "segments" not in payload:
            return {"id": 1, "zh_literal": "超过40年。", "zh_lecture": "超过40年。", "flags": []}
        ids = [int(row["id"]) for row in payload["segments"]]
        overexpanded = "超过40年了。我曾经在贝尔实验室工作过，那里的统计过程控制研究开始于那里。"
        if "zh_literal" in schema:
            return {
                "segments": [
                    {"id": sid, "zh_literal": overexpanded if sid == 1 else "统计过程控制可以减少波动。", "flags": []}
                    for sid in ids
                ]
            }
        return {
            "segments": [
                {"id": sid, "zh_lecture": overexpanded if sid == 1 else "统计过程控制可以减少波动。", "flags": []}
                for sid in ids
            ]
        }


class FakeNeighborLeakRewriteClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        if "segments" not in payload:
            return {"id": 1, "zh_literal": "我当时在使用SPC图表。", "zh_lecture": "我当时在使用SPC图表。", "flags": []}
        ids = [int(row["id"]) for row in payload["segments"]]
        literals = {
            1: "我当时在使用SPC图表。",
            2: "下一份工作是关于MRAM工具的。",
        }
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": literals[sid], "flags": []} for sid in ids]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": literals[1] + literals[2] if sid == 1 else literals[2],
                    "flags": [],
                }
                for sid in ids
            ]
        }


class FakeProtectedNeighborLeakRewriteClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        if "segments" not in payload:
            return {"id": 1, "zh_literal": "我当时在<KEEP_001>处理图表。", "zh_lecture": "我当时在<KEEP_001>处理图表。", "flags": []}
        ids = [int(row["id"]) for row in payload["segments"]]
        literals = {
            1: "我当时在<KEEP_001>处理图表。",
            2: "下一份工作使用<KEEP_001>工具。",
        }
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": literals[sid], "flags": []} for sid in ids]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": "我当时在<KEEP_001>处理图表。下一份工作使用MRAM工具。" if sid == 1 else literals[2],
                    "flags": [],
                }
                for sid in ids
            ]
        }


class FakePartialNeighborLeakRewriteClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        literals = {
            33: "一本行业内的指南书，人们开始使用，并且因为他用了它。",
            34: "他注意到自己在这一阶段收集了大量数据，并且发现了一个现象。",
        }
        if "segments" not in payload:
            sid = int(payload.get("segment", {}).get("id", 33))
            return {"id": sid, "zh_literal": literals[sid], "zh_lecture": literals[sid], "flags": []}
        ids = [int(row["id"]) for row in payload["segments"]]
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": literals[sid], "flags": []} for sid in ids]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": (
                        literals[33] + "他注意到自己在这一阶段收集了大量数据。"
                        if sid == 33
                        else literals[34]
                    ),
                    "flags": [],
                }
                for sid in ids
            ]
        }


class FakeJapanesePhilosophyLeakClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        literals = {
            136: "日本人只是将其作为一种哲学来接受。",
            137: "他们基本上，各个行业都涉及到了。",
            138: "不仅仅是汽车行业，各个行业都在使用。",
        }
        if "segments" not in payload:
            sid = int(payload.get("segment", {}).get("id", 136))
            return {"id": sid, "zh_literal": literals[sid], "zh_lecture": literals[sid], "flags": []}
        ids = [int(row["id"]) for row in payload["segments"]]
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": literals[sid], "flags": []} for sid in ids]}
        return {
            "segments": [
                {
                    "id": sid,
                    "zh_lecture": (
                        "日本人只是将其作为一种哲学来接受。他们基本上在每个行业中都采用了这种方法，而不仅仅是汽车工业。"
                        if sid == 136
                        else literals[sid]
                    ),
                    "flags": [],
                }
                for sid in ids
            ]
        }


class FakeContextOnlyPhraseLeakClient:
    model = "qwen2.5:14b-instruct"

    def json_chat(self, _system, user, schema):
        payload = json.loads(user)
        literals = {
            137: "他们基本上，各个行业。",
            138: "不仅仅是汽车行业，每个行业。",
            139: "他们开始把这作为核心流程使用。",
        }
        if "segments" not in payload:
            sid = int(payload.get("segment", {}).get("id", 137))
            return {"id": sid, "zh_literal": literals[sid], "zh_lecture": literals[sid], "flags": []}
        ids = [int(row["id"]) for row in payload["segments"]]
        if "zh_literal" in schema:
            return {"segments": [{"id": sid, "zh_literal": literals[sid], "flags": []} for sid in ids]}
        rewrites = {
            137: "他们基本上，不仅仅是在汽车行业。",
            138: "而是每个行业都开始使用这种方法作为核心流程。",
            139: literals[139],
        }
        return {"segments": [{"id": sid, "zh_lecture": rewrites[sid], "flags": []} for sid in ids]}
