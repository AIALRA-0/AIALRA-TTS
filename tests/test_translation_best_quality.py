import json

from ecse_localizer.subtitle_io import Segment
from ecse_localizer.translate import (
    build_translation_paragraphs,
    context_window,
    default_style_guide,
    paragraph_lookup,
    quality_requirements,
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
