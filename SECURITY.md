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

## Artifact Access And Deletion

Generated artifact download URLs are short-lived HMAC-signed URLs. The URL binds to the artifact id and user; it does not expose the filesystem path in the token.

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
