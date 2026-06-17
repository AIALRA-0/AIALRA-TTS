# AIALRA Local Video Localizer

Self-hosted video localization for local ASR, translation, bilingual subtitles, TTS dubbing, and video muxing. The core pipeline runs on a Windows GPU worker and never sends media, audio, subtitles, transcripts, or generated artifacts to cloud inference APIs.

## Local Quick Start

Put this project inside the course/video root as `_localizer_project`, then run:

```powershell
cd "<VIDEO_ROOT>\_localizer_project"
.\setup.ps1
.\00_audit_env.ps1
.\01_smoke_test.ps1 -Seconds 90
```

Process one full video:

```powershell
.\02_process_one.ps1 -Video "<VIDEO_ROOT>\<lecture>.mp4"
```

Batch processing:

```powershell
.\03_process_all.ps1
```

`process-all` skips videos that already have a PASS report and all required outputs. Use `.\03_process_all.ps1 -Force` only when you intentionally want to regenerate.

## Local WebUI

Start the local control panel:

```powershell
.\05_start_webui.ps1
```

Open:

```text
http://127.0.0.1:7861
```

Set real credentials in local `config.yaml` or `.env`; never commit them. The WebUI supports uploads, task history, project metadata, common tuning parameters, logs, quota checks, and local worker health/metrics.

The WebUI also has a `产物` page for generated artifacts. It can list reports, subtitles, WAV/MP4 outputs, show local preview links for media, create short-lived signed download URLs, delete managed output files, and run cleanup dry-runs. Deletion is restricted to managed output, run, and upload directories; it refuses to touch the original video root.

## Remote + Local Architecture

The intended production layout is:

- Contabo server: web frontend, login, user/project/job metadata, quotas, preview index, and status dashboard.
- Windows local worker: all media storage, GPU/CPU processing, ASR, translation, subtitles, TTS, muxing, and artifact cleanup.
- Connection: Windows initiates a reverse tunnel or private VPN connection. Do not expose the Windows worker directly to the public internet.

Contabo should store only metadata, small thumbnails, low-bitrate previews, and optional short-lived caches. Original videos and full-resolution outputs remain on the local worker by default.

Windows worker heartbeat:

```powershell
cd "<VIDEO_ROOT>\_localizer_project"
$env:REMOTE_PUBLIC_BASE_URL="https://your-contabo-domain.example"
$env:WORKER_SHARED_TOKEN="<generated-worker-token>"
.\06_worker_heartbeat.ps1 -Loop
```

Windows worker queue polling:

```powershell
.\07_worker_poll.ps1 -RemoteBaseUrl $env:REMOTE_PUBLIC_BASE_URL -WorkerToken $env:WORKER_SHARED_TOKEN
```

For Contabo production, set `webui.execution_mode: "worker_queue"` in the remote config. In this mode the web server only queues jobs; the Windows worker claims them and runs local GPU/CPU processing.

Optional Windows Scheduled Tasks:

```powershell
.\install_worker_heartbeat_task.ps1 -RemoteBaseUrl $env:REMOTE_PUBLIC_BASE_URL -WorkerToken $env:WORKER_SHARED_TOKEN
.\install_worker_poll_task.ps1 -RemoteBaseUrl $env:REMOTE_PUBLIC_BASE_URL -WorkerToken $env:WORKER_SHARED_TOKEN
```

Contabo deployment templates live in `deploy/`:

- `deploy/docker-compose.yml`
- `deploy/Dockerfile.web`
- `deploy/Caddyfile.example`
- `deploy/config.remote.example.yaml`
- `deploy/systemd/*.service`
- `deploy/systemd/*.timer`

## Backends

- Subtitles: existing `.vtt/.srt/.ass` are preferred and normalized before ASR.
- ASR: `whisperx` then `faster-whisper` when installed; existing subtitles avoid ASR when good enough.
- Audio enhancement: ffmpeg loudnorm/highpass/lowpass always available; DeepFilterNet/ClearerVoice optional.
- Translation: local Ollama/LM Studio OpenAI-compatible endpoint only. Qwen 14B is preferred for quality; 7B is fallback.
- TTS: CosyVoice SFT is the preferred local Chinese backend; Piper is a lightweight fallback. Voice cloning stays disabled unless explicit consent files are present.
- Subtitles: `.ass` means Advanced SubStation Alpha and is used for styled bilingual subtitles.

## Privacy And Git Safety

Do not commit:

- `config.yaml`, `.env`, secrets, tokens, server addresses, IPs, or passwords.
- source videos, generated videos, generated audio, subtitles, logs, reports, model weights, or run caches.
- local Windows absolute paths containing personal account names.

Before committing:

```powershell
.\tools\secret_scan.ps1
```

The public template files are `config.example.yaml` and `.env.example`.

## Useful Commands

```powershell
python -m ecse_localizer audit --input "<VIDEO_ROOT>"
python -m ecse_localizer smoke --input "<VIDEO_ROOT>" --seconds 90
python -m ecse_localizer process-one --video "<VIDEO_ROOT>\<lecture>.mp4"
python -m ecse_localizer process-all --input "<VIDEO_ROOT>"
python -m ecse_localizer report --output "<VIDEO_ROOT>\_localizer_output"
python -m ecse_localizer tts-health
python -m ecse_localizer worker-status
python -m ecse_localizer worker-poll --remote-base-url "https://example.invalid" --worker-token "<token>" --once --dry-run
python -m ecse_localizer cleanup --older-than-days 7
```

## GitHub

Recommended repository:

```text
https://github.com/AIALRA-0/AIALRA-TTS.git
```

Recommended description:

```text
Self-hosted video localization platform with a remote web UI and a Windows GPU worker for local ASR, translation, subtitles, TTS, and video dubbing.
```
