# Security Notes

This project is designed for local media processing. Treat videos, subtitles, transcripts, generated audio, generated video, logs, and reports as private data.

## Do Not Commit

- `.env`
- `config.yaml`
- `deploy/config.remote.yaml`
- real server hostnames, IP addresses, tunnel names, tokens, or passwords
- source videos or uploaded media
- generated subtitles, audio, videos, QA reports, logs, model weights, and run caches
- Windows absolute paths that include a personal account name

Run before committing:

```powershell
.\tools\secret_scan.ps1
```

## Remote Access

Preferred production access is a reverse tunnel or private VPN initiated by the Windows worker. Avoid opening inbound public ports on the Windows GPU machine.

Worker requests must use a shared token or mTLS. Rotate the token if it appears in logs or terminal output.

Remote WebUI deployments should keep `webui.cookie_secure=true` and `webui.csrf_origin_check=true`. The reverse proxy must preserve the public `Host` and `X-Forwarded-Proto` headers so same-origin browser writes pass and cross-site writes are rejected.

Worker job status, control, preview, and cache upload requests are accepted only from the worker id that currently owns the claimed job. Late responses from a stale worker cannot overwrite a job already reclaimed by another worker.

Global tuning and raw YAML configuration endpoints are admin-only. They may expose local paths, worker credentials, download-signing secrets, and deployment controls.

Dashboard storage paths are redacted for non-admin users. Ordinary users receive storage labels, while admins can still see absolute paths for deployment troubleshooting.

Video lists use `video-ref:<id>` for non-admin users instead of exposing course or upload directory paths. The server resolves the reference only for the current user's visible videos when a local job is submitted.

## Artifact Access And Deletion

Generated artifact download URLs are short-lived HMAC-signed URLs. The URL binds to the artifact id and user; it does not expose the filesystem path in the token.

Disabling a user invalidates their browser session access and their existing signed artifact URLs.

In multi-user WebUI mode, generated artifacts, previews, and temporary remote caches must carry an owner before non-admin users can see them. Ownerless legacy cache rows are admin-only so they can be audited or cleaned without becoming public.

Historical WebUI job records without an owner are also admin-only. Non-admin users can view and act only on jobs explicitly owned by their account.

Report lists and dashboard report summaries follow the same owner rule; ownerless legacy reports are admin-only.

Artifact deletion is restricted to managed directories:

- configured output directory
- configured work/run directory
- configured WebUI upload directory

The artifact API must refuse deletion outside those roots. Original course videos should remain outside deletion scope.

## Voice Consent

Voice cloning must remain disabled unless a user provides explicit consent files:

- `voices/authorized_reference.wav`
- `voices/README_CONSENT.txt`

Without those files, use a neutral built-in teaching voice.
