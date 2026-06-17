from ecse_localizer.qa import has_usable_chinese, looks_like_non_translation_narration
from ecse_localizer.translate import (
    is_forbidden_non_translation,
    is_usable_zh,
    restore_and_repair_protected_terms,
    sanitize_flags,
    safe_short_phrase_translation,
    should_bypass_low_capacity_llm,
)


def test_llm_placeholder_is_not_usable_translation():
    for text in ["...", "…", "advanced logic chips", ""]:
        assert not is_usable_zh(text)
        assert not has_usable_chinese(text)


def test_short_real_chinese_is_usable_translation():
    assert is_usable_zh("欢迎大家。")
    assert has_usable_chinese("这一节课我们先看半导体产业的位置。")


def test_topic_narration_is_rejected_as_translation():
    bad = "这一段主要围绕半导体展开。"
    assert is_forbidden_non_translation(bad)
    assert looks_like_non_translation_narration(bad)


def test_safe_short_phrase_translation_is_real_translation():
    assert safe_short_phrase_translation("So that's.") == "大概就是这样。"
    assert safe_short_phrase_translation("Good to go.") == "可以开始了。"
    assert safe_short_phrase_translation("That's number one.") == "这是第一点。"
    assert safe_short_phrase_translation("Right?") == "对吧？"
    assert safe_short_phrase_translation("when,") == "当时，"
    assert safe_short_phrase_translation("but,") == "不过，"


def test_protected_acronym_repair_replaces_wrong_extra_acronym():
    repaired = restore_and_repair_protected_terms(
        "当TSMC说要运行第一批晶圆。",
        {"<KEEP_001>": "PSMC"},
        "when PSMC says it will run the first shuttles.",
    )
    assert "PSMC" in repaired
    assert "TSMC" not in repaired


def test_protected_number_repair_appends_missing_number():
    repaired = restore_and_repair_protected_terms(
        "工程师应该按时间线思考。",
        {"<KEEP_001>": "25"},
        "In a hundred and 25.",
    )
    assert "25" in repaired


def test_sanitize_flags_drops_placeholders():
    assert sanitize_flags(["<KEEP_001>", "ZH_OVER_TARGET_LENGTH"]) == ["ZH_OVER_TARGET_LENGTH"]


def test_low_capacity_llm_bypass_only_for_long_runs():
    config = {"llm": {"low_capacity_models": ["0.5b"], "low_capacity_bypass_segment_threshold": 120}}
    assert should_bypass_low_capacity_llm("qwen2.5:0.5b-instruct", 857, config)
    assert not should_bypass_low_capacity_llm("qwen2.5:0.5b-instruct", 14, config)
    assert not should_bypass_low_capacity_llm("qwen2.5:7b-instruct", 857, config)
