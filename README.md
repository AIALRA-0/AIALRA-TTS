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

Set real credentials in local `config.yaml` or `.env`; never commit them. The WebUI supports uploads, task history, project/folder metadata, project quotas, reusable parameter templates, invite-only users, admin-managed user status/quotas, common tuning parameters, logs, quota checks, local worker health/metrics, failed-job retry, and soft-deleting job records.

Per-job language, quality, and style settings are written to generated job config files under `runs/`. The base `config.yaml` stays local and is not mutated by submitted jobs.

Parameter templates store non-secret generation settings such as source language, target subtitle/TTS language, quality mode, teaching style, TTS speed/emotion, pause settings, subtitle length, and hard/soft subtitle preferences. Templates live under `runs/platform/` and are ignored by git.

The task form shows local language capability hints for ASR, subtitle translation, and TTS. These hints come from `asr.supported_languages`, `translation.supported_target_languages`, `translation.allow_unlisted_targets`, and `tts.supported_languages`, plus the currently detected local LLM/TTS backend. Unlisted subtitle targets can be queued when the local LLM is available, but they are flagged as QA-required; TTS targets must be supported by the active local TTS backend.

The WebUI also has a `产物` page for generated artifacts. It can list reports, subtitles, WAV/MP4 outputs, show local preview links for media, create short-lived signed download URLs, delete managed output files, and run cleanup dry-runs. Deletion is restricted to managed output, run, upload, job-log, and preview-cache directories; it refuses to touch the original video root. Cleanup preserves job JSON and platform/user metadata, but can remove expired run/cache files plus old soft-deleted job outputs and preview manifest rows. Uploads enforce suffix, per-file size, user quota, and request-level reserved bytes; job submission enforces per-user and global active-job limits.

For Contabo mode, low-bitrate previews and thumbnails can be registered through `webui.preview_manifest` without exposing Windows source paths. The manifest can be a list or `{ "previews": [...] }`; each row may include `name`, `preview_path`, `thumbnail_path`, `owner`, `project_id`, `job_id`, and `source_output_key`. The API serves only the preview-cache files through signed URLs.

When a queued Windows worker job finishes successfully, the worker can generate a low-bitrate MP4 plus JPG thumbnail with ffmpeg and upload them to `/api/worker/jobs/{job_id}/preview`. These uploads use the same HMAC worker signature as heartbeat/status requests, are capped by `webui.worker_preview_max_upload_mb`, and count against `remote_quota_bytes`. Preview upload failure is non-fatal: the full local output remains on the Windows worker and the job status still reflects the actual processing result.

The worker also registers opaque full-output artifact refs after successful jobs. Contabo stores only safe metadata such as file name, size, MIME type, output key, and `ref_id`; it does not store the Windows path. In the artifacts page these entries show `请求下载`. Clicking it queues a worker action that uploads the selected full file to a remote temporary cache, capped by `webui.worker_artifact_cache_max_upload_mb` and the user's `remote_quota_bytes`. Once cached, the same artifact becomes a normal signed download link and can be cleaned up by the preview/cache cleanup policy.

Quota fields are split by storage responsibility. `remote_quota_bytes` limits Contabo-side uploads plus preview-cache files; `local_quota_bytes` is kept as the Windows worker storage budget for original media and full outputs. Upload requests reserve bytes against the remote quota across all files in the same request. New jobs are rejected when the local worker quota or selected project quota is already exhausted, rather than queuing work that cannot be stored. In `worker_queue` production mode, browser media upload is disabled by default unless `webui.allow_remote_media_uploads: true` is explicitly set; this prevents long-term original videos from landing on the Contabo disk.

Job records are JSON files with `schema_version`. WebUI normalizes older records on read/claim/update by filling missing metadata, log path, dispatch target, timestamps, retry count, and worker args when possible. Job states are normalized to `queued`, `claimed`, `running`, `paused`, `retrying`, `done`, `failed`, `cancelled`, and `deleted`; older `passed` records are migrated to `done` with `legacy_status: passed`.

Project quota is tracked as generated managed artifact usage per project. Original course videos stay outside project cleanup and remain protected.

Cleanup is intentionally two-stage: deleting a job first hides the record from normal history by marking it `deleted`; the scheduled `cleanup` command later removes that deleted job's report bundle, logs, preview cache files, and stale manifest rows after `webui.cleanup_older_than_days`. The job JSON record remains as audit metadata unless you remove it manually from the managed job store.

## Remote + Local Architecture

The intended production layout is:

- Contabo server: web frontend, login, user/project/job metadata, quotas, preview index, and status dashboard.
- Windows local worker: all media storage, GPU/CPU processing, ASR, translation, subtitles, TTS, muxing, and artifact cleanup.
- Connection: Windows initiates a reverse tunnel or private VPN connection. Do not expose the Windows worker directly to the public internet.

Contabo should store only metadata, small thumbnails, low-bitrate previews, and optional short-lived caches under `webui.preview_dir`. Original videos and full-resolution outputs remain on the local worker by default.

Remote production config should keep `webui.allow_remote_media_uploads: false`. The Windows worker can publish opaque `worker-ref:<id>` media options from `worker.media_roots` or `input_dir`; Contabo stores only file name, size, media type, and ref id, while the real path stays in the worker's local registry. Users can also paste a Windows worker-visible video path in the task form's `Worker 本地视频路径` field as a fallback, or you can add a private worker upload tunnel later; do not use the public Contabo web disk as the default original-video store.

Windows worker heartbeat:

```powershell
cd "<VIDEO_ROOT>\_localizer_project"
$env:REMOTE_PUBLIC_BASE_URL="https://your-contabo-domain.example"
$env:WORKER_SHARED_TOKEN="<generated-worker-hmac-secret>"
.\06_worker_heartbeat.ps1 -Loop
```

Windows worker queue polling:

```powershell
.\07_worker_poll.ps1 -RemoteBaseUrl $env:REMOTE_PUBLIC_BASE_URL -WorkerToken $env:WORKER_SHARED_TOKEN
```

For Contabo production, set `webui.execution_mode: "worker_queue"` in the remote config. In this mode the web server only queues jobs; the Windows worker claims them and runs local GPU/CPU processing. If the worker heartbeat is missing or stale, the WebUI keeps the job queued and shows `等待 worker` instead of claiming the task has started.

Worker API authentication supports signed requests. Production remote config should set `webui.worker_auth_mode: "hmac"` and `webui.worker_require_nonce: true` so worker heartbeat, claim, and status requests must include `X-Worker-Timestamp`, `X-Worker-Nonce`, and `X-Worker-Signature`; the shared secret stays in `WORKER_SHARED_TOKEN` and is not sent as a plaintext header. The nonce is part of the signature and is remembered for the timestamp window, so replaying a captured signed request is rejected. Local development can keep `hmac_or_token` for compatibility with older scripts.

Queued jobs carry only portable worker arguments plus non-secret job metadata such as source language, target subtitle language, TTS language, quality mode, style, and the worker availability state at submit time. The Windows worker applies those values to a local generated job config before running the CLI.

While a worker job is running, the Windows worker periodically reports a remote-safe status payload: `running`, `worker_id`, `pid`, best-effort `progress`, sanitized system metrics, local managed-storage byte counts, and a log tail. Contabo stores only that summary; full logs stay on the Windows worker. Metrics are stripped of filesystem path fields before storage or display, and the dashboard uses worker heartbeat metrics in `worker_queue` mode instead of the Contabo host metrics.

Worker status summaries are redacted before storage: Windows paths, local user names embedded in paths, obvious token/password/API-key values, authorization credentials, and private LAN IP addresses are replaced with placeholders. The complete local log remains on the Windows worker for debugging; Contabo receives only the safe tail.

Running worker jobs can be cancelled from the WebUI. The remote server marks the job with `cancel_requested`; the Windows worker sees that flag through its signed `/api/worker/jobs/{job_id}/control` poll, terminates the local child process, and reports `cancelled`. Contabo still never opens an inbound connection to the Windows machine.

Queued worker jobs can be paused before a worker claims them. A paused job is not claimable until the user resumes it, at which point it returns to the worker queue. Running GPU jobs are not fake-paused; use cancel for work already executing on Windows.

If a worker-claimed job stops reporting status, the WebUI can recover it automatically. With `webui.worker_requeue_stale_jobs: true`, records stuck in `claimed` or `running` longer than `webui.worker_job_heartbeat_timeout_seconds` are moved to `retrying` and become claimable again; after `webui.worker_job_max_auto_retries`, the record is marked `failed` instead of looping forever.

After a successful worker job, set `worker.upload_previews: true` to have the worker create and upload a preview cache item. The default preview is 854px wide at about 700k video bitrate and 96k AAC audio; adjust `worker.preview_max_width`, `worker.preview_video_bitrate`, `worker.preview_audio_bitrate`, and `worker.preview_max_seconds` to fit the remote storage quota.

Full-output downloads in remote mode are request-based. The Windows worker keeps a local artifact registry under `runs/worker_artifacts/registry.json`, receives `upload_artifact_cache` jobs, and uploads only the requested file to `/api/worker/jobs/{job_id}/artifact-cache` using HMAC. Treat that remote cache as temporary storage, not the source of truth.

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
- Translation: local Ollama/LM Studio OpenAI-compatible endpoint only. `best_quality` reconstructs spoken paragraphs before per-segment JSON translation, then applies a course style guide, coherence pass, and deterministic quality flags for summary-like translations, over-compression, repeated calques, and unchanged literal rewrites. `fidelity-audit` reviews the finished report, and `repair-fidelity` rewrites only the failed/low-score/quality-flagged segments before regenerating subtitles, TTS, muxed video, and QA. Qwen 14B is preferred for quality; 7B is fallback.
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
python -m ecse_localizer fidelity-audit --report "<VIDEO_ROOT>\_localizer_output\<lecture>_report.json"
python -m ecse_localizer repair-fidelity --report "<VIDEO_ROOT>\_localizer_output\<lecture>_report.json"
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
