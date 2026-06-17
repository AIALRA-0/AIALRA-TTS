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
2. Create `.env` from `.env.example` and fill:
   - `WEBUI_SESSION_SECRET`
   - `WEBUI_ADMIN_USERNAME`
   - `WEBUI_ADMIN_PASSWORD`
   - `REMOTE_PUBLIC_BASE_URL`
   - `WORKER_SHARED_TOKEN`
   - `WEBUI_DOWNLOAD_SECRET`
3. Create a production config from `config.example.yaml`.
   - Set `privacy.allow_cloud_api=false`.
   - Set `privacy.allow_upload_media=false`.
   - Set small Contabo remote quota defaults.
   - Set `webui.default_project_quota_gb` to the per-project generated-artifact budget.
   - Set `webui.max_active_jobs_per_user` and `webui.max_active_jobs_global` conservatively for the first public test.
   - Set `asr.supported_languages`, `translation.supported_target_languages`, and `tts.supported_languages` to match the Windows worker's installed models.
   - Keep full media storage on the Windows worker.
   - Set `webui.execution_mode=worker_queue` on Contabo.
   - Keep `webui.allow_remote_media_uploads=false` on Contabo unless a deliberate short-lived cache policy is approved.
   - Do not copy local `config.yaml`; use template values and environment variables only.
4. Run the web service behind Caddy or Nginx with HTTPS.
5. Restrict upload size at reverse proxy and application level.
6. Configure persistent volumes only for:
   - metadata database or JSON store
   - thumbnails/previews cache
   - `webui.preview_dir` and `webui.preview_manifest` for low-bitrate preview rows
   - short-lived logs
7. Configure a cleanup job:
   - remove expired previews
   - remove deleted-user artifacts
   - reject new uploads when quota would be exceeded
8. Configure worker connectivity:
   - prefer WireGuard/Tailscale/cloudflared reverse tunnel
   - set `webui.worker_auth_mode: "hmac"` and require `X-Worker-Timestamp` + `X-Worker-Signature` for worker heartbeat/API
   - treat `WORKER_SHARED_TOKEN` as an HMAC secret; do not send it as a plaintext production header
   - mark worker offline after missed heartbeats
   - enable stale worker-job recovery with `webui.worker_requeue_stale_jobs=true`, choose a conservative `webui.worker_job_heartbeat_timeout_seconds`, and cap retries with `webui.worker_job_max_auto_retries`
   - on Windows, run:
     ```powershell
     .\06_worker_heartbeat.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN" -Loop
     ```
   - to claim and execute queued jobs on Windows, run:
     ```powershell
     .\07_worker_poll.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     ```
   - queued job metadata for source language, target subtitle language, TTS language, quality mode, and style is applied on the Windows worker through a generated local job config under `runs/worker_job_configs`.
   - if the Windows worker heartbeat is missing or stale, new jobs must stay `queued` and the UI must show that they are waiting for the local worker, not that GPU processing has already started.
   - during long jobs, the worker posts `running` status updates with best-effort progress, GPU/CPU/disk metrics, local managed-storage byte counts, and a short log tail; do not require Contabo to read Windows log files directly.
   - worker metrics must be sanitized before storage/display; do not expose Windows filesystem paths in dashboard, quota, job, artifact, or heartbeat responses.
   - if a claimed/running worker job becomes stale, the WebUI should move it to `retrying` so the restored Windows worker can claim it again; after the configured retry cap, it should become `failed`.
   - after successful jobs, the worker may upload a low-bitrate MP4 preview and JPG thumbnail to `/api/worker/jobs/{job_id}/preview`; this endpoint must require HMAC signatures, enforce `webui.worker_preview_max_upload_mb`, count files against `remote_quota_bytes`, and never store Windows source paths in the manifest.
   - after successful jobs, the worker registers opaque full-output artifact refs; users request full downloads by queuing an `upload_artifact_cache` worker action, and the worker uploads only that selected file to `/api/worker/jobs/{job_id}/artifact-cache` as temporary remote cache.
   - or install the scheduled task:
     ```powershell
     .\install_worker_heartbeat_task.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     .\install_worker_poll_task.ps1 -RemoteBaseUrl "https://your-domain.example" -WorkerToken "$env:WORKER_SHARED_TOKEN"
     ```
9. Validate:
   - login works
   - project creation works
   - project folders can be created and selected for jobs
   - parameter templates can be listed, created, selected, and applied to queued worker jobs
   - the task form displays ASR/subtitle/TTS language support hints from the configured local model capabilities
   - the task form can queue both `fidelity_audit` and `repair_fidelity`; repair jobs must use the selected report and default to its sibling `*_fidelity_report.json`
   - user quota is enforced
   - `remote_quota_bytes` limits Contabo uploads plus preview-cache files, while `local_quota_bytes` remains the Windows worker storage budget
   - new job submission returns HTTP 413 when the Windows worker local quota or selected project quota is already exhausted
   - upload quota is enforced across multi-file requests and active-job concurrency limits return HTTP 429 with a readable message
   - browser media upload is disabled in `worker_queue` mode unless explicitly enabled, so original videos do not land on the Contabo disk by default
   - admins can disable users and update local/remote user quotas without disabling the last active admin
   - project quota usage is visible for generated managed artifacts
   - worker heartbeat appears online
   - unsigned worker requests are rejected when `worker_auth_mode=hmac`
   - job submission while the worker is offline shows a queued/waiting state and records the worker status at submit time
   - GPU/CPU metrics update
   - dashboard metrics in `worker_queue` mode come from the Windows worker heartbeat and contain no Windows path fields
   - worker offline status appears when the tunnel is stopped
   - users cannot see each other's jobs
   - jobs move through `queued/claimed/running/retrying/done/failed/cancelled/deleted`
   - stale `claimed/running` worker jobs are automatically requeued up to the configured cap, then marked failed
   - older JSON job records without the current schema are normalized on read and remain visible/retryable when their state allows it
   - running jobs refresh progress/log-tail summaries without exposing local Windows paths or full logs
   - failed jobs can be retried without rewriting the base config
   - deleted jobs are soft-deleted from normal history before any physical artifact cleanup
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
- `deploy/systemd/aialra-web.service`
- `deploy/systemd/aialra-cleanup.service`
- `deploy/systemd/aialra-cleanup.timer`

Suggested public description:
`Self-hosted video localization platform with a remote web UI and a Windows GPU worker for local ASR, translation, subtitles, TTS, and video dubbing.`
