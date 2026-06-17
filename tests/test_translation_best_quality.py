from ecse_localizer.subtitle_io import Segment
from ecse_localizer.translate import context_window, default_style_guide, quality_requirements, use_best_quality


def test_best_quality_defaults_enabled():
    config = {"translation": {"quality_mode": "best_quality", "style": "natural_chinese_lecture"}}
    assert use_best_quality(config)
    assert "不要像逐词翻译" in default_style_guide(config)
    assert any("Do not omit" in item for item in quality_requirements(config))


def test_context_window_keeps_neighbor_segments_only():
    segments = [Segment(i, float(i), float(i + 1), f"text {i}") for i in range(1, 6)]
    before = context_window(segments, 2, -2)
    after = context_window(segments, 2, 2)

    assert [row["id"] for row in before] == [1, 2]
    assert [row["id"] for row in after] == [4, 5]
