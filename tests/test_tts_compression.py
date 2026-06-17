from ecse_localizer.tts import local_compact_tts_text, preserves_numbers


def test_local_tts_compression_preserves_numbers():
    text = "我在IBM研究院工作，过去23年一直在从事逻辑开发，之前还有6年的内存研发。"
    compact = local_compact_tts_text(text, 28)
    assert len(compact) < len(text)
    assert preserves_numbers(text, compact)
    assert "23" in compact
    assert "6" in compact
