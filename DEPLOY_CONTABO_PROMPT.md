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
   - Keep full media storage on the Windows worker.
   - Set `webui.execution_mode=worker_queue` on Contabo.
   - Do not copy local `config.yaml`; use template values and environment variables only.
4. Run the web service behind Caddy or Nginx with HTTPS.
5. Restrict upload size at reverse proxy and application level.
6. Configure persistent volumes only for:
   - metadata database or JSON store
   - thumbnails/previews cache
   - short-lived logs
7. Configure a cleanup job:
   - remove expired previews
   - remove deleted-user artifacts
   - reject new uploads when quota would be exceeded
8. Configure worker connectivity:
   - prefer WireGuard/Tailscale/cloudflared reverse tunnel
   - require `X-Worker-Token: $WORKER_SHARED_TOKEN` for worker heartbeat/API
   - mark worker offline after missed heartbeats
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
   - during long jobs, the worker posts `running` status updates with best-effort progress, GPU/CPU/disk metrics, and a short log tail; do not require Contabo to read Windows log files directly.
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
   - user quota is enforced
   - admins can disable users and update local/remote user quotas without disabling the last active admin
   - project quota usage is visible for generated managed artifacts
   - worker heartbeat appears online
   - job submission while the worker is offline shows a queued/waiting state and records the worker status at submit time
   - GPU/CPU metrics update
   - worker offline status appears when the tunnel is stopped
   - users cannot see each other's jobs
   - jobs move through `queued/claimed/running/retrying/done/failed/cancelled/deleted`
   - older JSON job records without the current schema are normalized on read and remain visible/retryable when their state allows it
   - running jobs refresh progress/log-tail summaries without exposing local Windows paths or full logs
   - failed jobs can be retried without rewriting the base config
   - deleted jobs are soft-deleted from normal history before any physical artifact cleanup
   - a queued job can be claimed through `/api/worker/jobs/claim`
   - worker status updates through `/api/worker/jobs/{job_id}/status`
   - `/api/artifacts` lists only authorized artifacts
   - signed artifact download URLs expire and do not expose filesystem paths
   - artifact deletion refuses paths outside managed output/run/upload roots

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
