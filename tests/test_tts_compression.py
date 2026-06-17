from ecse_localizer.tts import is_valid_tts_compression, local_compact_tts_text, normalize_tts_compression_candidate, preserves_numbers


def test_local_tts_compression_preserves_numbers():
    text = "我在IBM研究院工作，过去23年一直在从事逻辑开发，之前还有6年的内存研发。"
    compact = local_compact_tts_text(text, 28)
    assert len(compact) < len(text)
    assert preserves_numbers(text, compact)
    assert "23" in compact
    assert "6" in compact


def test_non_chinese_tts_compression_keeps_spaces_and_validates():
    config = {"translation": {"target_language": "es"}}
    original = "Ajusta el umbral del comparador a 25 mV antes de la siguiente medición."
    compressed = "Ajusta el umbral a 25 mV antes de medir."

    assert normalize_tts_compression_candidate("  Ajusta   el umbral a 25 mV.  ", config) == "Ajusta el umbral a 25 mV."
    assert is_valid_tts_compression(original, compressed, 48, config)
    assert not is_valid_tts_compression(original, "Ajusta el umbral antes de medir.", 48, config)
