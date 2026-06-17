# Remote Tunnel Guide

This platform does not require a public inbound port on the Windows GPU worker. The safe production shape is:

- Contabo serves the WebUI over HTTPS.
- Windows worker initiates outbound connectivity to Contabo or a private overlay network.
- Worker API calls are signed with HMAC plus nonce.
- Contabo stores metadata, thumbnails, low-bitrate previews, and temporary download cache only.

## Recommended Options

### Tailscale

Use this when you want the simplest private overlay between Contabo and Windows.

1. Install Tailscale on Contabo and Windows.
2. Put both machines in the same tailnet.
3. Keep the WebUI public through Caddy/Nginx, but keep worker-only endpoints protected by HMAC.
4. On Windows, set:

   ```powershell
   $env:REMOTE_PUBLIC_BASE_URL="https://<REMOTE_DOMAIN>"
   $env:WORKER_SHARED_TOKEN="<LONG_RANDOM_HMAC_SECRET>"
   .\09_worker_healthcheck.ps1
   .\06_worker_heartbeat.ps1 -Loop
   .\07_worker_poll.ps1
   ```

### Cloudflare Tunnel

Use this when Contabo should receive public HTTPS traffic without opening extra ports beyond the tunnel agent.

1. Run the tunnel agent on Contabo for the WebUI domain.
2. Route the public hostname to the local WebUI service on Contabo.
3. Do not route traffic to the Windows worker.
4. Windows still calls the public WebUI URL outbound and signs worker requests.

The repository includes a systemd unit template for the Contabo tunnel agent:

```bash
sudo install -m 0644 deploy/systemd/aialra-worker-tunnel.service /etc/systemd/system/aialra-worker-tunnel.service
sudo install -d -m 0750 /etc/aialra
sudo tee /etc/aialra/worker-tunnel.env >/dev/null <<'EOF'
CLOUDFLARED_CONFIG=/etc/cloudflared/aialra-localizer.yml
CLOUDFLARED_TUNNEL_NAME=aialra-localizer
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now aialra-worker-tunnel.service
```

Keep tunnel credentials in the cloudflared config directory on the server. Do not put worker tokens, public hostnames, private IPs, or cloudflared credentials in the unit file or in Git.

### WireGuard

Use this when you want explicit self-managed VPN routing.

1. Put Contabo and Windows into one WireGuard network.
2. Keep the Windows worker behind outbound-only firewall rules where possible.
3. Use Caddy/Nginx on Contabo for public WebUI HTTPS.
4. Keep `webui.worker_auth_mode: "hmac"` and `webui.worker_require_nonce: true`.

## Required Checks

Run these before exposing the service:

```bash
python -m ecse_localizer --config deploy/config.remote.yaml deploy-check
```

Run these on Windows after setting local environment variables:

```powershell
.\09_worker_healthcheck.ps1
.\06_worker_heartbeat.ps1
.\07_worker_poll.ps1 -Once -DryRun
```

The worker health check sends a signed heartbeat only when both `REMOTE_PUBLIC_BASE_URL` and `WORKER_SHARED_TOKEN` are available. It never prints the token and reports only pass/fail status plus issue codes.

## Security Rules

- Do not commit `.env`, production config, public hostnames, private IPs, or worker tokens.
- Do not expose a Windows worker HTTP server directly to the internet.
- Keep browser video upload disabled on Contabo unless a short-lived cache policy is explicitly approved.
- Use short signed URLs for previews and downloads.
- Treat remote full-output cache as temporary; the Windows worker remains the source of truth.
