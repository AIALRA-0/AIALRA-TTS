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
        "Vout = Vin * R2 / (R1 + R2)",
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


def test_formula_is_protected_as_one_token():
    text = "Use Vout = Vin * R2 / (R1 + R2) before sensor_readout.py."
    result = protect_text(text)

    assert "Vout = Vin * R2 / (R1 + R2)" in result.mapping.values()
    assert restore_text(result.text, result.mapping) == text
