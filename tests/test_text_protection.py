from ecse_localizer.text_protection import protect_text, protected_roundtrip, restore_text


def test_required_tokens_roundtrip():
    samples = [
        "O(n log n)",
        "x_i",
        r"C:\path\file.py",
        "https://example.com",
        "ResNet-50",
        "ECSE 4961",
        "PSMC, TSMC, AMD, IBM, US",
        "543 dies, 25 mm, 1st shuttle, October 2025",
        "3.3V, 5 kHz, 10^-6",
    ]
    for sample in samples:
        result = protect_text(sample)
        assert result.mapping
        assert restore_text(result.text, result.mapping) == sample
        assert protected_roundtrip(sample)


def test_mixed_text_restores_exactly():
    text = r"Use O(n log n), x_i, C:\path\file.py and https://example.com with 3.3V."
    result = protect_text(text)
    assert "<KEEP_001>" in result.text
    assert restore_text(result.text, result.mapping) == text
