# Contabo Deployment Prompt

Use this prompt with a server-side coding/deployment agent after creating the GitHub repository and pointing DNS at the Contabo server. Replace placeholders only on the server. Do not commit generated `.env`, `deploy/config.remote.yaml`, public hostnames, private IPs, worker secrets, admin credentials, logs, uploads, previews, or cache files.

```text
You are deploying AIALRA-TTS on a Contabo Linux server as the public WebUI only. The Windows RTX worker remains the only GPU/media-processing machine. The server must store only login/project/job metadata, small preview files, thumbnails, and short-lived full-output cache files.

Repository:
https://github.com/AIALRA-0/AIALRA-TTS.git

Hard rules:
- Do not upload source videos, full outputs, subtitles, transcripts, worker logs, model weights, or Windows paths to any cloud service.
- Do not expose the Windows worker directly to the internet.
- Do not commit `.env`, `deploy/config.remote.yaml`, credentials, domains, IP addresses, tokens, generated previews, uploads, or run caches.
- Keep `webui.execution_mode` as `worker_queue`.
- Keep `webui.allow_remote_media_uploads` as `false` unless there is an explicit short-lived upload policy.
- Keep `webui.allow_worker_path_submission` as `false` for public deployments.
- Use HMAC worker authentication and nonce replay protection.
- Enforce remote storage quotas and cleanup timers before opening the service to users.

Target public origin:
https://YOUR_DOMAIN.example.invalid

Storage policy:
- Reserve `/srv/aialra/previews` for Contabo-side uploads, previews, thumbnails, and temporary artifact caches.
- Reserve `/srv/aialra/runs` for remote job metadata and transient server state.
- Reserve `/srv/aialra/platform` for user/project/template metadata.
- Set `webui.global_remote_quota_gb` to the server-approved total remote storage budget.
- Set each user's `quota_remote_gb` to their allowed Contabo-side storage.
- Treat remote full-output cache as temporary; Windows remains the source of truth.

Deployment steps:
1. Install Docker, Docker Compose, git, and a TLS reverse proxy such as Caddy or Nginx.
2. Clone the repository to `/opt/aialra/AIALRA-TTS`.
3. Run:
   `python3 deploy/bootstrap_contabo.py --public-base-url https://YOUR_DOMAIN.example.invalid --admin-username admin`
4. Review `.env` and `deploy/config.remote.yaml` locally on the server. Do not print secrets into chat logs.
5. Edit `deploy/config.remote.yaml` only for quotas, domain trusted origins, and storage policy. Keep privacy and worker security settings locked down.
6. Run:
   `docker compose -f deploy/docker-compose.yml build`
   `docker compose -f deploy/docker-compose.yml up -d`
7. Run:
   `docker compose -f deploy/docker-compose.yml exec web python -m ecse_localizer --config deploy/config.remote.yaml deploy-check`
8. Install and enable the cleanup timer from `deploy/systemd/` so expired preview/cache files are purged.
9. Put the WebUI behind HTTPS. Confirm cookies are secure and CSRF origin checks pass through the proxy.
10. Confirm `/api/health`, login, dashboard, projects, history, artifacts, and quotas work from the public origin.

Windows worker handoff:
- Give the Windows operator only the public origin and generated worker shared secret through a private channel.
- On Windows, set persistent environment variables for `REMOTE_PUBLIC_BASE_URL` and `WORKER_SHARED_TOKEN`.
- Run `.\09_worker_healthcheck.ps1`, then `.\13_start_worker.ps1 -LocalCheck`, then the long-running `.\13_start_worker.ps1`.
- Confirm the Contabo dashboard shows the worker online with sanitized GPU/CPU/storage metrics.

Acceptance checks:
- `deploy-check` returns zero errors.
- Browser uploads are disabled in worker queue mode.
- Worker auth mode is HMAC and nonce protection is enabled.
- Public URLs are HTTPS and not localhost or private IPs.
- The WebUI does not display raw Windows paths to non-admin users.
- Job submission queues work instead of running processing on Contabo.
- Preview upload and artifact-cache upload count against remote quota.
- Cleanup dry-run reports only managed preview/cache/run files.
- Secret scan passes before any push.

If any check fails, stop and fix the config or deployment scripts before exposing the service.
```

