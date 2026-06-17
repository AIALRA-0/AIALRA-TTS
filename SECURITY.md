# Security Notes

This project is designed for local media processing. Treat videos, subtitles, transcripts, generated audio, generated video, logs, and reports as private data.

## Do Not Commit

- `.env`
- `config.yaml`
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

## Voice Consent

Voice cloning must remain disabled unless a user provides explicit consent files:

- `voices/authorized_reference.wav`
- `voices/README_CONSENT.txt`

Without those files, use a neutral built-in teaching voice.
