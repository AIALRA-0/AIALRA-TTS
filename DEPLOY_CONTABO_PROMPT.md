# Prompt For Contabo Deployment Agent

You are deploying the remote web control plane for `AIALRA Local Video Localizer`.

Hard constraints:
- Do not run cloud inference.
- Do not store long-term original videos or full-resolution generated outputs on the Contabo server unless explicitly enabled by quota policy.
- Do not expose the Windows GPU worker directly to the public internet.
- Use placeholders from `.env.example`; never commit or print real secrets.

Target architecture:
- Contabo hosts the authenticated web UI, metadata DB, user/project/job records, quota state, small previews, and reverse-tunnel endpoint.
- Windows worker stores original media and full outputs locally, runs GPU/CPU inference, and sends status/heartbeat to Contabo.
- Windows worker initiates the connection to Contabo through a reverse tunnel or private VPN. Contabo calls only the tunnel endpoint or receives worker polling.

Deployment steps:
1. Clone the repository:
   ```bash
   git clone https://github.com/AIALRA-0/AIALRA-TTS.git
   cd AIALRA-TTS
   ```
2. Generate local-only deployment files. This writes `.env` and `deploy/config.remote.yaml`, refuses to overwrite existing files unless `--force` is passed, and does not print generated secrets:
   ```bash
   python3 deploy/bootstrap_contabo.py --public-base-url https://your-domain.example --admin-username admin
   ```
   If you need to set the first admin password manually, add `--admin-password 'REPLACE_WITH_STRONG_LOCAL_SECRET'`. Do not commit `.env` or `deploy/config.remote.yaml`.
3. Review `.env` locally and fill or rotate if needed:
   - `WEBUI_SESSION_SECRET`
   - `WEBUI_ADMIN_USERNAME`
   - `WEBUI_ADMIN_PASSWORD`
   - `REMOTE_PUBLIC_BASE_URL`
   - `WORKER_SHARED_TOKEN`
   - `WEBUI_DOWNLOAD_SECRET`
4. Review the production config in `deploy/config.remote.yaml`.
   - Set `privacy.allow_cloud_api=false`.
   - Set `privacy.allow_upload_media=false`.
   - Set small Contabo remote quota defaults.
   - Set `webui.default_project_quota_gb` to the per-project generated-artifact budget.
   - Set `webui.max_active_jobs_per_user` and `webui.max_active_jobs_global` conservatively for the first public test.
   - Set `asr.supported_languages`, `translation.supported_target_languages`, and `tts.supported_languages` as fallback hints; when the Windows worker is online, its heartbeat/claim `capabilities` payload should be treated as the authoritative local model capability view.
   - Keep full media storage on the Windows worker.
   - Set `webui.execution_mode=worker_queue` on Contabo.
   - Keep `webui.allow_remote_media_uploads=false` on Contabo unless a deliberate short-lived cache policy is approved.
   - Keep `webui.allow_worker_path_submission=false`; users should select opaque `worker-ref:<id>` options published by the Windows worker, not paste Windows filesystem paths into the public web app.
   - Do not copy local `config.yaml`; use template values and environment variables only.
5. Run the deployment guard before starting the public service:
   ```bash
   python -m ecse_localizer --config deploy/config.remote.yaml deploy-check
   ```
   Treat any `ERROR` as a hard stop. The check validates placeholder secrets, worker HMAC/nonce enforcement, remote media upload policy, privacy flags, signed URL TTL, remote quota bounds, private IPs, and Windows path leakage without printing secret values.
6. Run the web service behind Caddy or Nginx with HTTPS.
7. Restrict upload size at reverse proxy and application level.
8. Configure persistent volumes only for:
   - metadata database or JSON store
   - thumbnails/previews cache
   - `webui.preview_dir` and `webui.preview_manifest` for low-bitrate preview rows
   - short-lived logs
9. Configure a cleanup job:
   - remove expired previews
   - remove old soft-deleted job artifacts, logs, preview cache files, and stale preview manifest rows
   - preserve job JSON records, user metadata, project metadata, and template metadata
   - reject new uploads when quota would be exceeded
10. Configure worker connectivity:
   - prefer WireGuard/Tailscale/cloudflared reverse tunnel
   - follow `deploy/REMOTE_TUNNEL_GUIDE.md` for the outbound-only worker connection model
   - set `webui.worker_auth_mode: "hmac"` and `webui.worker_require_nonce: true`; require `X-Worker-Timestamp` + `X-Worker-Nonce` + `X-Worker-Signature` for worker heartbeat/API
   - treat `WORKER_SHARED_TOKEN` as an HMAC secret; do not send it as a plaintext production header
   - mark worker offline after missed heartbeats
   - enable stale worker-job recovery with `webui.worker_requeue_stale_jobs=true`, choose a conservative `webui.worker_job_heartbeat_timeout_seconds`, and cap retries with `webui.worker_job_max_auto_retries`
   - on Windows, run:
     ```powershell
     .\09_worker_healthcheck.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     .\06_worker_heartbeat.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN" -Loop
     ```
   - to claim and execute queued jobs on Windows, run:
     ```powershell
     .\07_worker_poll.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     ```
   - preferred direct worker entry:
     ```powershell
     python -m ecse_localizer worker --local-check
     python -m ecse_localizer worker --remote-base-url "https://your-domain.example" --worker-token "$env:WORKER_SHARED_TOKEN"
     ```
   - queued job metadata for source language, target subtitle language, TTS language, quality mode, and style is applied on the Windows worker through a generated local job config under `runs/worker_job_configs`.
   - if the Windows worker heartbeat is missing or stale, new jobs must stay `queued` and the UI must show that they are waiting for the local worker, not that GPU processing has already started.
   - during long jobs, the worker posts `running` status updates with best-effort progress, GPU/CPU/disk metrics, local managed-storage byte counts, and a short log tail; do not require Contabo to read Windows log files directly.
   - worker metrics must be sanitized before storage/display; do not expose Windows filesystem paths in dashboard, quota, job, artifact, or heartbeat responses.
   - worker log tails, errors, and command summaries must be redacted before storage/display; do not expose Windows paths, local user names from paths, private LAN IPs, authorization credentials, worker tokens, passwords, signatures, or API keys.
   - worker heartbeat messages and media-ref display names must be treated as untrusted and redacted; store only file display names, never full Windows paths.
   - cancelling a running worker job must set a `cancel_requested` flag; the Windows worker must observe it through a signed `/api/worker/jobs/{job_id}/control` poll and then report `cancelled`.
   - pausing a worker job is a queue-level operation for `queued/retrying` jobs only; paused jobs must not be claimable until resumed.
   - worker heartbeat/claim may include opaque media refs from `worker.media_roots`; Contabo must store only ref id, display name, size, MIME type, and mtime, never the Windows source path.
   - if a claimed/running worker job becomes stale, the WebUI should move it to `retrying` so the restored Windows worker can claim it again; after the configured retry cap, it should become `failed`.
   - after successful jobs, the worker may upload a low-bitrate MP4 preview and JPG thumbnail to `/api/worker/jobs/{job_id}/preview`; this endpoint must require HMAC signatures, enforce `webui.worker_preview_max_upload_mb`, count files against `remote_quota_bytes`, and never store Windows source paths in the manifest.
   - after successful jobs, the worker registers opaque full-output artifact refs; users request full downloads by queuing an `upload_artifact_cache` worker action, and the worker uploads only that selected file to `/api/worker/jobs/{job_id}/artifact-cache` as temporary remote cache.
   - or install the scheduled task:
     ```powershell
     .\install_worker_heartbeat_task.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     .\install_worker_poll_task.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     ```
11. Validate:
   - `python -m ecse_localizer --config deploy/config.remote.yaml deploy-check` returns PASS before the service is exposed
   - `.\09_worker_healthcheck.ps1` returns PASS on the Windows worker before scheduled tasks are installed
   - `python -m ecse_localizer translation-sample --output runs/translation_quality_sample` creates JSON/Markdown comparing literal, lecture, coherence, and repair stages
   - `python -m ecse_localizer remote-smoke --output runs/remote_smoke` passes the local Contabo/worker queue simulation
   - login works
   - project creation works
   - project folders can be created and selected for jobs
   - parameter templates can be listed, created, selected, and applied to queued worker jobs
   - the task form displays ASR/subtitle/TTS language support hints from the online Windows worker heartbeat when available, otherwise from configured fallback capabilities
   - the task form can queue both `fidelity_audit` and `repair_fidelity`; repair jobs must use the selected report and default to its sibling `*_fidelity_report.json`
   - user quota is enforced
   - `remote_quota_bytes` limits Contabo uploads plus preview-cache files, while `local_quota_bytes` remains the Windows worker storage budget
   - new job submission returns HTTP 413 when the Windows worker local quota or selected project quota is already exhausted
   - upload quota is enforced across multi-file requests and active-job concurrency limits return HTTP 429 with a readable message
   - browser media upload is disabled in `worker_queue` mode unless explicitly enabled, so original videos do not land on the Contabo disk by default
   - worker media refs appear in the task video selector as `worker-ref:<id>` options without exposing Windows source paths
   - the task form rejects raw Windows worker-visible local video paths unless `webui.allow_worker_path_submission=true` is deliberately enabled for a private trusted deployment
   - admins can disable users and update local/remote user quotas without disabling the last active admin
   - project quota usage is visible for generated managed artifacts
   - worker heartbeat appears online
   - worker heartbeat/claim payloads can publish ASR, translation, and TTS language capabilities; `/api/capabilities` should report `source=worker_heartbeat` while that worker is online
   - unsigned worker requests are rejected when `worker_auth_mode=hmac`
   - replaying the same signed worker request with the same nonce is rejected
   - job submission while the worker is offline shows a queued/waiting state and records the worker status at submit time
   - GPU/CPU metrics update
   - dashboard metrics in `worker_queue` mode come from the Windows worker heartbeat and contain no Windows path fields
   - remote job records contain only redacted worker log tails and command summaries
   - worker heartbeat messages and worker media options expose no Windows usernames or source paths
   - worker offline status appears when the tunnel is stopped
   - users cannot see each other's jobs
   - jobs move through `queued/claimed/running/paused/retrying/done/failed/cancelled/deleted`
   - pausing a queued worker job removes it from the claimable queue; resuming it makes it claimable again
   - cancelling a running worker job does not require inbound access to Windows; the next worker control poll sees the cancellation request and stops the local process
   - stale `claimed/running` worker jobs are automatically requeued up to the configured cap, then marked failed
   - older JSON job records without the current schema are normalized on read and remain visible/retryable when their state allows it
   - running jobs refresh progress/log-tail summaries without exposing local Windows paths or full logs
   - failed jobs can be retried without rewriting the base config
   - deleted jobs are soft-deleted from normal history before any physical artifact cleanup
   - cleanup dry-runs report old deleted-job artifacts without deleting them; cleanup apply removes only managed output/run/upload/job-log/preview files and preserves metadata JSON
   - a queued job can be claimed through `/api/worker/jobs/claim`
   - a queued `repair_fidelity` job claims portable worker args beginning with `repair-fidelity`
   - worker status updates through `/api/worker/jobs/{job_id}/status`
   - `/api/artifacts` lists only authorized artifacts
   - signed artifact download URLs expire and do not expose filesystem paths
   - preview manifest rows expose only preview-cache paths/display names, never Windows `source_path`
   - preview thumbnails use signed `variant=thumbnail` URLs
   - worker preview uploads reject unsigned bodies, reject disallowed suffixes, and return HTTP 413 when the remote quota would be exceeded
   - worker full-output refs expose no Windows path, show a request-cache action before download, and cached full files obey `webui.worker_artifact_cache_max_upload_mb` plus `remote_quota_bytes`
   - artifact deletion refuses paths outside managed output/run/upload/preview roots

Do not push `.env`, production config, logs, media, model weights, IP addresses, server hostnames, or credentials back to GitHub.

Suggested service model:
- `aialra-web.service`: Contabo web app.
- `aialra-worker-tunnel.service`: reverse tunnel endpoint/client as appropriate.
- `aialra-cleanup.timer`: TTL cleanup for previews and deleted artifacts.

The repository includes:
- `deploy/docker-compose.yml`
- `deploy/Dockerfile.web`
- `deploy/Caddyfile.example`
- `deploy/config.remote.example.yaml`
- `deploy/bootstrap_contabo.py`
- `deploy/systemd/aialra-web.service`
- `deploy/systemd/aialra-cleanup.service`
- `deploy/systemd/aialra-cleanup.timer`
- `RELEASE.md`
- `.github/workflows/ci.yml`

Before tagging a release, run `python -m ecse_localizer release-check` and follow `RELEASE.md`. Do not create releases from a dirty worktree or with generated media/config files tracked by git.

Suggested public description:
`Self-hosted video localization platform with a remote web UI and a Windows GPU worker for local ASR, translation, subtitles, TTS, and video dubbing.`
