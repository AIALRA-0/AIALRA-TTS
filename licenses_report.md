# License Report

Generated baseline for the localizer project. The CLI refreshes a copy in `_localizer_output`.

| Component | Use | License | Commercial-use risk |
|---|---|---|---|
| Project code in `_localizer_project` | Local orchestration | User-owned local project | User decides distribution terms |
| Python | Runtime | PSF License | Low |
| PyYAML | Config parsing | MIT | Low |
| pysubs2 | Subtitle parsing/writing | MIT | Low |
| requests | Local endpoint probing | Apache-2.0 | Low |
| jsonschema | Local structured output/schema validation | MIT | Low |
| tqdm | Local progress reporting | MPL-2.0 AND MIT | Low; verify MPL notice obligations before redistribution |
| FastAPI | Local WebUI/API server | MIT | Low |
| Uvicorn | Local WebUI ASGI server | BSD-3-Clause | Low |
| python-multipart | Local WebUI upload parsing | Apache-2.0 | Low |
| httpx2 | Local WebUI/TestClient HTTP client | BSD-3-Clause | Low |
| httpcore2 | httpx2 transport dependency | BSD-3-Clause | Low |
| truststore | Local TLS certificate verification for HTTP client stack | MIT | Low |
| pytest | Tests | MIT | Low |
| FFmpeg/ffprobe gyan build | Audio/video processing | FFmpeg is LGPL/GPL depending on build flags | Check exact binary build before commercial redistribution |
| NVIDIA driver/CUDA runtime | GPU acceleration | NVIDIA proprietary EULA | Runtime redistribution restrictions may apply |
| Piper | Offline TTS engine | MIT | Low for engine |
| rhasspy/piper-voices `zh_CN-huayan-medium` | Mandarin TTS voice | Hugging Face repo metadata: MIT | Verify source voice/dataset provenance before commercial redistribution |
| faster-whisper | Optional ASR | MIT | Low |
| CTranslate2 | Optional ASR inference | MIT | Low |
| WhisperX | Optional alignment/ASR | BSD/MIT-style dependencies vary | Verify dependency stack before commercial use |
| OpenAI Whisper model weights | Optional ASR model | MIT | Low |
| Qwen2.5 14B Instruct via Ollama | Local LLM translation, rewrite, glossary, fidelity audit | `ollama show qwen2.5:14b-instruct --license` reports Apache-2.0 | Low for model license; verify downstream redistribution obligations before commercial packaging |
| CosyVoice code in `third_party/CosyVoice` | Local Chinese TTS backend | Apache-2.0 from local `LICENSE` | Low for code license |
| FunAudioLLM `CosyVoice-300M-SFT` | Local neutral Chinese teacher voice (`中文男`), no voice cloning | Local model README metadata says `license: apache-2.0` | Low for model license; verify source training data/provenance before commercial redistribution |
| F5-TTS/Fish-Speech | Optional future TTS backends | Varies by repo/model | Some weights may be non-commercial or research-only; not enabled by default |

No OpenAI, Google, Azure, ElevenLabs, DeepL, Baidu, Tencent, Alibaba Cloud, Rask, or HeyGen APIs are used by this project.
