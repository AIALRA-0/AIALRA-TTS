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

Unauthenticated monitoring probes are available at `/healthz` and `/readyz`. They return only service/version/mode/check booleans and do not expose usernames, job records, filesystem paths, tokens, or local media metadata. Use `/readyz` for Docker, systemd, Caddy, Nginx, and external uptime checks because it also verifies the WebUI metadata, job, upload, and output stores are writable.

Set real credentials in local `config.yaml` or `.env`; never commit them. The WebUI supports uploads, task history, project/folder metadata, project and folder editing/archiving/restoring, project quotas, reusable parameter templates, invite-only users, admin-managed user status/quotas, common tuning parameters, logs, quota checks, local worker health/metrics, SSE live status updates with polling fallback, failed-job retry, and soft-deleting/restoring job records.

Per-job language, quality, and style settings are written to generated job config files under `runs/`. The base `config.yaml` stays local and is not mutated by submitted jobs.

Parameter templates store non-secret generation settings such as source language, target subtitle/TTS language, quality mode, teaching style, TTS speed/emotion, pause settings, maximum compact dubbing gap, subtitle length, and hard/soft subtitle preferences. Users can create a new template, update the currently selected template after tuning the task form, or delete templates they no longer use; template params are sanitized through a fixed allowlist before they are saved. Templates live under `runs/platform/` and are ignored by git.

The task form shows local language capability hints for ASR, subtitle translation, and TTS. In worker-queue mode, an online Windows worker can publish its detected `language_capabilities` through heartbeat/claim calls, and the Contabo UI uses that live worker view first. If no worker capability heartbeat is online, hints fall back to `asr.supported_languages`, `translation.supported_target_languages`, `translation.allow_unlisted_targets`, and `tts.supported_languages`, plus the detected local LLM/TTS backend. Unlisted subtitle targets can be queued when the local LLM is available, but they are flagged as QA-required; TTS targets must be supported by the active local TTS backend.

The WebUI also has a `产物` page for generated artifacts. It can list reports, subtitles, WAV/MP4 outputs, filter artifacts by project, folder, job id, and output kind, show local preview links for media, create short-lived signed download URLs, delete managed output files, run cleanup dry-runs, and let an admin confirm managed cleanup. Deletion is restricted to managed output, run, upload, job-log, and preview-cache directories; it refuses to touch the original video root. Deleting a remote preview or temporary full-artifact cache also prunes the matching `preview_manifest.json` row so deleted artifacts do not linger in the remote index. Cleanup preserves job JSON and platform/user metadata, but can remove expired run/cache files plus old soft-deleted job outputs and preview manifest rows. Uploads enforce suffix, per-file size, user quota, and request-level reserved bytes; job submission enforces per-user and global active-job limits.

For Contabo mode, low-bitrate previews and thumbnails can be registered through `webui.preview_manifest` without exposing Windows source paths. The manifest can be a list or `{ "previews": [...] }`; each row may include `name`, `preview_path`, `thumbnail_path`, `owner`, `project_id`, `job_id`, and `source_output_key`. The API serves only the preview-cache files through signed URLs.

When a queued Windows worker job finishes successfully, the worker can generate a low-bitrate MP4 plus JPG thumbnail with ffmpeg and upload them to `/api/worker/jobs/{job_id}/preview`. These uploads use the same HMAC worker signature as heartbeat/status requests; worker metadata headers such as `X-Worker-Id`, preview IDs, and artifact refs are included in the signature so they cannot be changed without invalidating the request. Uploads require a valid `Content-Length`, are capped by `webui.worker_preview_max_upload_mb`, and count against `remote_quota_bytes`. Preview upload failure is non-fatal: the full local output remains on the Windows worker and the job status still reflects the actual processing result.

The worker also registers opaque full-output artifact refs after successful jobs. Contabo stores only safe metadata such as file name, size, MIME type, output key, and `ref_id`; it does not store the Windows path. In the artifacts page these entries show `请求下载`. Clicking it queues a worker action that uploads the selected full file to a remote temporary cache, capped by `webui.worker_artifact_cache_max_upload_mb` and the user's `remote_quota_bytes`. When the worker has reported the artifact size, the WebUI rejects over-quota or over-limit cache requests before queueing worker work. Repeated requests for the same artifact reuse the active cache job instead of consuming more worker slots. Once cached, the same artifact becomes a normal signed download link and can be cleaned up by the preview/cache cleanup policy.

Quota fields are split by storage responsibility. Per-user `remote_quota_bytes` limits a user's Contabo-side uploads plus preview-cache files, and `webui.global_remote_quota_gb` caps total Contabo-side upload/preview/cache storage across all users. `local_quota_bytes` is kept as the Windows worker storage budget for original media and full outputs. Upload requests are preflighted before multipart parsing when `Content-Length` makes them clearly impossible under the remaining remote quota; `webui.upload_preflight_overhead_mb` accounts for multipart envelope bytes, while the final quota decision still uses the exact streamed file bytes and reserves bytes across all files in the same request. New jobs are rejected when the local worker quota or selected project quota cannot fit current usage plus active queued/running job reservations. For `process_one`, the reservation uses the safe source video size from worker media refs or local uploaded media multiplied by `webui.job_storage_reserve_multiplier` so Contabo does not queue work that is likely to fail on Windows storage. Quota and project APIs expose `*_reserved_bytes`, `*_committed_bytes`, and `*_available_bytes` so the UI can show active reservations instead of only already-written files. In `worker_queue` production mode, browser media upload is disabled by default unless `webui.allow_remote_media_uploads: true` is explicitly set; this prevents long-term original videos from landing on the Contabo disk.

In `worker_queue` mode, the WebUI also uses the online Windows worker heartbeat's sanitized `metrics.disk.free_bytes` as a physical disk guard. If the estimated local output reservation would leave less than `webui.worker_disk_min_free_gb` free on the worker output volume, job submission returns HTTP 413 before the worker starts. This complements, but does not replace, per-user `local_quota_bytes` and project quotas.

Job records are JSON files with `schema_version`. WebUI normalizes older records on read/claim/update by filling missing metadata, log path, dispatch target, timestamps, retry count, and worker args when possible. Job states are normalized to `queued`, `claimed`, `running`, `paused`, `retrying`, `done`, `failed`, `cancelled`, and `deleted`; older `passed` records are migrated to `done` with `legacy_status: passed`.

Project quota is tracked as generated managed artifact usage per project. Original course videos stay outside project cleanup and remain protected. Project and folder deletion in the UI is soft archive: archived targets are hidden from new-job selectors and rejected for new jobs, but old job records keep their original `project_id` and `folder_id` for audit history. The project page can show archived targets and restore them when they need to become selectable again.

Cleanup is intentionally two-stage: deleting a job first hides the record from normal history by marking it `deleted`; choose the `deleted` status filter in the history UI or call `/api/jobs?status=deleted` to review the recycle-bin view, then restore with `/api/jobs/{job_id}/restore` before cleanup if needed. Artifacts linked to deleted jobs are hidden from the normal artifacts page and signed download/cache endpoints, while `/api/jobs/{job_id}/artifacts` can still show them from the deleted job detail view for review before cleanup. Restoring a record does not automatically rerun queued/running work; use retry explicitly when you want it to run again. The scheduled `cleanup` command later removes that deleted job's report bundle, logs, preview cache files, and stale manifest rows after `webui.cleanup_older_than_days`. It also removes stale managed run-cache files under `work_dir`, with reason summaries for TTS segment WAVs, temporary/enhanced audio, subtitle caches, translation/ASR trace JSON, preview cache, and generic temp files. Final output videos, final WAVs, subtitles, reports, job JSON, user/project metadata, and templates are preserved by the TTL scan unless the job has been soft-deleted and aged past the cleanup threshold. The job JSON record remains as audit metadata unless you remove it manually from the managed job store.

## Remote + Local Architecture

The intended production layout is:

- Contabo server: web frontend, login, user/project/job metadata, quotas, preview index, and status dashboard.
- Windows local worker: all media storage, GPU/CPU processing, ASR, translation, subtitles, TTS, muxing, and artifact cleanup.
- Connection: Windows initiates a reverse tunnel or private VPN connection. Do not expose the Windows worker directly to the public internet.

Contabo should store only metadata, small thumbnails, low-bitrate previews, and optional short-lived caches under `webui.preview_dir`. Original videos and full-resolution outputs remain on the local worker by default.

Remote production config should keep `webui.allow_remote_media_uploads: false` and `webui.allow_worker_path_submission: false`. The Windows worker can publish opaque `worker-ref:<id>` media options from `worker.media_roots` or `input_dir`; Contabo stores only file name, size, media type, and ref id, while the real path stays in the worker's local registry. With that default, the task form hides and clears the raw worker-path input and submits only selected `worker-ref` media options. A raw Windows worker-visible path can be enabled only for a private trusted deployment by setting `webui.allow_worker_path_submission: true`; public Contabo deployments should not store these paths. Add a private worker upload tunnel later if users need browser uploads without placing originals on the Contabo disk.

Windows worker health and unified runner:

```powershell
cd "<VIDEO_ROOT>\_localizer_project"
$env:REMOTE_PUBLIC_BASE_URL="https://your-contabo-domain.example"
$env:WORKER_SHARED_TOKEN="<generated-worker-hmac-secret>"
.\09_worker_healthcheck.ps1
.\13_start_worker.ps1 -LocalCheck
.\13_start_worker.ps1
```

The preferred long-running worker entry sends HMAC-signed heartbeats and claims queued jobs from the same process:

```powershell
python -m ecse_localizer worker --local-check
python -m ecse_localizer worker
```

The worker CLI reads `REMOTE_PUBLIC_BASE_URL` and `WORKER_SHARED_TOKEN` from the environment when the corresponding flags are omitted. Avoid passing the token as a command-line argument for long-running workers because process arguments can be inspected locally.

`06_worker_heartbeat.ps1` and `07_worker_poll.ps1` remain as compatibility split scripts, but production should run the unified worker unless there is a specific operational reason to split heartbeat and polling.

`worker.max_concurrent_jobs` defaults to `1`. Increase it only after smoke testing GPU memory use; the long-running worker can claim multiple jobs in parallel using per-slot worker IDs such as `local-windows-worker-1` and `local-windows-worker-2`.

For Contabo production, set `webui.execution_mode: "worker_queue"`, `webui.cookie_secure: true`, and `webui.csrf_origin_check: true` in the remote config. In this mode the web server only queues jobs; the Windows worker claims them and runs local GPU/CPU processing. If the worker heartbeat is missing or stale, the WebUI keeps the job queued and shows `等待 worker` instead of claiming the task has started.

When `webui.csrf_origin_check` is enabled, non-GET browser requests must come from the same Origin/Referer as the public WebUI host or from `webui.csrf_trusted_origins`. The signed `/api/worker/*` endpoints are exempt because they use worker HMAC authentication instead of browser cookies.

Worker API authentication supports signed requests. Production remote config should set `webui.worker_auth_mode: "hmac"` and `webui.worker_require_nonce: true` so worker heartbeat, claim, and status requests must include `X-Worker-Timestamp`, `X-Worker-Nonce`, and `X-Worker-Signature`; the shared secret stays in `WORKER_SHARED_TOKEN` and is not sent as a plaintext header. The nonce is part of the signature and is persisted in platform metadata for the timestamp window, so replaying a captured signed request is rejected even after a WebUI restart within that window. Local development can keep `hmac_or_token` for compatibility with older scripts.

Queued jobs carry only portable worker arguments plus non-secret job metadata such as source language, target subtitle language, TTS language, quality mode, style, and the worker availability state at submit time. The Windows worker applies those values to a local generated job config before running the CLI. Worker heartbeat and claim polls also include the worker's current language capability summary so the remote UI reflects the installed local ASR/LLM/TTS models instead of guessing from the Contabo host.

While a worker job is running, the Windows worker periodically reports a remote-safe status payload: `running`, `worker_id`, `pid`, best-effort `progress`, sanitized system metrics, local managed-storage byte counts, and a log tail. Contabo stores only that summary; full logs stay on the Windows worker. Metrics are stripped of filesystem path fields before storage or display, and the dashboard uses worker heartbeat metrics in `worker_queue` mode instead of the Contabo host metrics. The browser opens `/api/events` as an authenticated Server-Sent Events stream for near-real-time job, queue, worker, quota, and metrics updates; if the stream drops, the UI keeps the existing polling fallback.

Status, control, preview, and cache upload calls are bound to the `claimed_by` worker id on the job record. If a stale worker reports late after the server has requeued and another worker has claimed the job, the old update is rejected instead of overwriting the active run.

Worker status summaries are redacted before storage: Windows paths, local user names embedded in paths, obvious token/password/API-key values, authorization credentials, and private LAN IP addresses are replaced with placeholders. Heartbeat messages and worker-published media display names are also cleaned before Contabo stores them. The complete local log remains on the Windows worker for debugging; Contabo receives only the safe tail.

Running worker jobs can be cancelled from the WebUI. The remote server marks the job with `cancel_requested`; the Windows worker sees that flag through its signed `/api/worker/jobs/{job_id}/control` poll, terminates the local child process, and reports `cancelled`. Contabo still never opens an inbound connection to the Windows machine.

Queued worker jobs can be paused before a worker claims them. A paused job is not claimable until the user resumes it, at which point it returns to the worker queue. Running GPU jobs are not fake-paused; use cancel for work already executing on Windows.

Worker availability and stale-job recovery use separate thresholds. `webui.worker_offline_after_seconds` controls when the dashboard marks the Windows worker offline; keep it comfortably above the worker heartbeat interval, for example 180 seconds for the default 60-second heartbeat. With `webui.worker_requeue_stale_jobs: true`, records stuck in `claimed` or `running` longer than `webui.worker_job_heartbeat_timeout_seconds` are moved to `retrying` and become claimable again; after `webui.worker_job_max_auto_retries`, the record is marked `failed` instead of looping forever.

After a successful worker job, set `worker.upload_previews: true` to have the worker create and upload a preview cache item. The default preview is 854px wide at about 700k video bitrate and 96k AAC audio; adjust `worker.preview_max_width`, `worker.preview_video_bitrate`, `worker.preview_audio_bitrate`, and `worker.preview_max_seconds` to fit the remote storage quota.

Full-output downloads in remote mode are request-based. The Windows worker keeps a local artifact registry under `runs/worker_artifacts/registry.json`, receives `upload_artifact_cache` jobs, and uploads only the requested file to `/api/worker/jobs/{job_id}/artifact-cache` using HMAC. Treat that remote cache as temporary storage, not the source of truth.

Optional Windows Scheduled Tasks:

```powershell
.\install_worker_task.ps1 -RemoteBaseUrl $env:REMOTE_PUBLIC_BASE_URL -WorkerToken $env:WORKER_SHARED_TOKEN -StoreUserEnvironment
```

The unified task reads `REMOTE_PUBLIC_BASE_URL` and `WORKER_SHARED_TOKEN` from the persistent User/Machine environment at runtime, so the token is not embedded in the scheduled task command line. The installer refuses to create the task unless those values are already persistent or `-StoreUserEnvironment` is used. The older `install_worker_heartbeat_task.ps1` and `install_worker_poll_task.ps1` scripts are still available for split deployments.

Manage the unified scheduled task without exposing command-line secrets:

```powershell
.\14_manage_worker_task.ps1 -Action Status
.\14_manage_worker_task.ps1 -Action Restart
.\14_manage_worker_task.ps1 -Action Stop
.\14_manage_worker_task.ps1 -Action Uninstall
```

The management script intentionally omits task action arguments from its output. Use `-Json` when a monitoring script needs machine-readable status.

Contabo deployment templates live in `deploy/`:

- `deploy/docker-compose.yml`
- `deploy/Dockerfile.web`
- `deploy/Caddyfile.example`
- `deploy/nginx.conf.example`
- `deploy/config.remote.example.yaml`
- `deploy/bootstrap_contabo.py`
- `deploy/REMOTE_TUNNEL_GUIDE.md`
- `deploy/systemd/*.service`
- `deploy/systemd/*.timer`

The authoritative handoff prompt for a Contabo-side deployment agent is `DEPLOY_CONTABO_PROMPT.md` at the repository root.

On a fresh Contabo clone, generate local-only deployment files without printing secrets:

```bash
python3 deploy/bootstrap_contabo.py --public-base-url https://your-domain.example --admin-username admin
```

This creates `.env` and `deploy/config.remote.yaml`; both are ignored by git and must stay off GitHub. The generated remote config is rendered from `deploy/config.remote.example.yaml` with local deployment secrets, so it can be checked directly without exporting environment variables first.

Before putting the remote WebUI behind a public domain, run the deployment guard on the target config:

```bash
python -m ecse_localizer --config deploy/config.remote.yaml deploy-check
```

It fails closed when production safety settings are missing: placeholder secrets, weak worker auth, missing nonce enforcement, remote media upload enabled, unsafe privacy flags, unresolved environment variables, public/private inference endpoints in ASR/LLM/TTS settings, private IPs, Windows paths, or unreasonable preview/cache quota values. If `REMOTE_PUBLIC_BASE_URL` is set in the environment, it must be an HTTPS public origin, not `localhost`, a private IP, or plain HTTP. The check reports field paths and issue codes only; it does not echo secret values.

## Backends

- Subtitles: existing `.vtt/.srt/.ass` are preferred and normalized before ASR.
- ASR: `whisperx` then `faster-whisper` when installed; existing subtitles avoid ASR when good enough. The default source language is `auto`, and reports record requested language, Whisper backend language code, detected language, and probability.
- Audio enhancement: ffmpeg loudnorm/highpass/lowpass always available; DeepFilterNet/ClearerVoice optional.
- Translation: local Ollama/LM Studio OpenAI-compatible endpoint only. `best_quality` reconstructs spoken paragraphs before per-segment JSON translation, then applies a course style guide, coherence pass, and deterministic quality flags for summary-like translations, over-compression, repeated calques, unchanged literal rewrites, and missing protected technical tokens such as formulas, code/file names, URLs, acronyms, variables, and model names. QA text validation is target-language aware, so Latin-script targets such as Spanish are not rejected by Chinese-script heuristics while Chinese/Japanese/Korean targets still reject ASCII-only untranslated text. QA reports summarize actionable trace flags and include flagged segment samples, so review does not require opening the full trace JSON. `fidelity-audit` reviews the finished report, and `repair-fidelity` rewrites only the failed/low-score/quality-flagged segments before regenerating subtitles, TTS, muxed video, and QA. Qwen 14B is preferred for quality; 7B is fallback.
- `translation-sample` writes a deterministic local JSON/Markdown comparison of the same source segment across `literal`, `lecture`, `coherence`, and `repair` stages. Use it as a quick quality gate before long video runs.
- TTS: CosyVoice SFT is the preferred local Chinese backend; Piper is a lightweight fallback. Voice cloning stays disabled unless explicit consent files are present. The final dub mix applies clarity EQ, compression, loudness normalization, and limiting by default so missing per-deployment filters do not fall back to quiet raw audio.
- TTS alignment: each utterance is fit against its own subtitle window and the next utterance start; compact dubbing defaults to `bounded_distributed` scheduling so long silent gaps are capped by `tts.compact_distributed_max_gap_seconds` while still preserving minimum audio gaps. When earlier audio delays a segment, the target slot can shrink against the original timeline to avoid cumulative drift. Overlong audio is speed-limited, then slot-trimmed with a short fade-out when it still cannot fit. Reports include placement metadata, prevented overlap counts, slot trims, high delay warnings, and audio truncated at the video end.
- Subtitles: `.ass` means Advanced SubStation Alpha and is used for styled bilingual subtitles.

## Privacy And Git Safety

Do not commit:

- `config.yaml`, `.env`, secrets, tokens, server addresses, IPs, or passwords.
- source videos, generated videos, generated audio, subtitles, logs, reports, model weights, or run caches.
- local Windows absolute paths containing personal account names.

Before committing:

```powershell
.\tools\secret_scan.ps1
.\tools\check_powershell_syntax.ps1
.\12_platform_check.ps1
.\11_remote_smoke.ps1
.\10_release_check.ps1
```

The public template files are `config.example.yaml` and `.env.example`. Release rules live in `RELEASE.md`; CI runs tests, compile checks, WebUI JS syntax, PowerShell syntax, secret scan, translation sample, remote worker smoke, isolated WebUI API workflow smoke, worker health, aggregate platform check, and release metadata gates on GitHub. The isolated WebUI smoke creates throwaway user/project/folder/template records, publishes a signed worker heartbeat with a redacted `worker-ref`, queues and claims a worker job, verifies remote cancel/control/status handling, and never touches real platform state. CI explicitly uses `config.example.yaml`; never commit local `config.yaml`.

## Useful Commands

```powershell
python -m ecse_localizer audit --input "<VIDEO_ROOT>"
python -m ecse_localizer smoke --input "<VIDEO_ROOT>" --seconds 90
python -m ecse_localizer process-one --video "<VIDEO_ROOT>\<lecture>.mp4"
python -m ecse_localizer process-all --input "<VIDEO_ROOT>"
python -m ecse_localizer report --output "<VIDEO_ROOT>\_localizer_output"
python -m ecse_localizer fidelity-audit --report "<VIDEO_ROOT>\_localizer_output\<lecture>_report.json"
python -m ecse_localizer repair-fidelity --report "<VIDEO_ROOT>\_localizer_output\<lecture>_report.json"
python -m ecse_localizer translation-sample --output ".\runs\translation_quality_sample"
python -m ecse_localizer remote-smoke --output ".\runs\remote_smoke"
python -m ecse_localizer platform-check --output ".\runs\platform_check"
python -m ecse_localizer tts-health
python -m ecse_localizer worker-status
python -m ecse_localizer worker-health --skip-remote
python -m ecse_localizer worker --local-check
python -m ecse_localizer worker --once --dry-run
python -m ecse_localizer worker-poll --once --dry-run
.\14_manage_worker_task.ps1 -Action Status -Json
python -m ecse_localizer --config deploy/config.remote.yaml deploy-check
python -m ecse_localizer release-check
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
