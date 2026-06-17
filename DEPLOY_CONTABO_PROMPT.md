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
3. Create a production config from `config.example.yaml`.
   - Set `privacy.allow_cloud_api=false`.
   - Set `privacy.allow_upload_media=false`.
   - Set small Contabo remote quota defaults.
   - Keep full media storage on the Windows worker.
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
9. Validate:
   - login works
   - project creation works
   - user quota is enforced
   - worker heartbeat appears online
   - GPU/CPU metrics update
   - worker offline status appears when the tunnel is stopped
   - users cannot see each other's jobs

Do not push `.env`, production config, logs, media, model weights, IP addresses, server hostnames, or credentials back to GitHub.

Suggested service model:
- `aialra-web.service`: Contabo web app.
- `aialra-worker-tunnel.service`: reverse tunnel endpoint/client as appropriate.
- `aialra-cleanup.timer`: TTL cleanup for previews and deleted artifacts.

Suggested public description:
`Self-hosted video localization platform with a remote web UI and a Windows GPU worker for local ASR, translation, subtitles, TTS, and video dubbing.`
