from ecse_localizer.capabilities import infer_piper_language, language_capabilities, language_supported


def test_language_supported_handles_common_aliases():
    assert language_supported(["zh-CN"], "mandarin")
    assert language_supported(["zh"], "zh-HK")
    assert language_supported(["yue"], "cantonese")
    assert not language_supported(["zh-CN"], "ja")


def test_infer_piper_language_from_model_filename():
    assert infer_piper_language("models/piper/voices/zh_CN-huayan-medium.onnx") == "zh-CN"
    assert infer_piper_language("en_US-lessac-medium.onnx") == "en-US"


def test_language_capabilities_reports_tts_support_from_configured_list():
    caps = language_capabilities(
        {
            "asr": {"language": "auto"},
            "translation": {"target_language": "ja", "allow_unlisted_targets": True},
            "tts": {"language": "ja", "supported_languages": ["zh-CN", "yue"]},
        },
        llm_status={"available": True, "backend": "openai_compatible_local", "model": "qwen2.5:14b-instruct"},
        tts_status={"backend": "cosyvoice_sft"},
    )
    assert caps["asr"]["auto_detect"] is True
    assert caps["translation"]["current_supported"] is True
    assert caps["tts"]["current_supported"] is False
    assert caps["tts"]["supported_languages"] == ["zh-CN", "yue"]


def test_language_capabilities_marks_no_real_tts_backend_unsupported():
    caps = language_capabilities(
        {"translation": {"target_language": "zh-CN"}, "tts": {"language": "zh-CN"}},
        llm_status={"available": False, "backend": "none", "model": None},
        tts_status={"backend": "ffmpeg_tone_fallback"},
    )
    assert caps["translation"]["current_supported"] is False
    assert caps["tts"]["current_supported"] is False
    assert "tone fallback" in caps["tts"]["notes"]
