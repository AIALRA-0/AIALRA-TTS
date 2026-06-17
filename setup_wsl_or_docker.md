# Optional WSL2/Docker Route

Native Windows PowerShell is the default route for this project. WSL2 and Docker Desktop are optional for heavier ASR/TTS experiments.

Detected-friendly strategy:

1. Keep all course inputs on the Windows filesystem.
2. Mount the course folder into WSL/Docker read-only for experiments.
3. Write generated outputs only to `_localizer_output`.
4. Do not bind any cloud credentials or API keys into containers.

Example Docker pattern, after Docker Desktop is running:

```powershell
docker run --rm --gpus all `
  -v "<VIDEO_ROOT>:/work:rw" `
  -w /work/_localizer_project `
  python:3.12-slim python -m ecse_localizer audit --input "/work"
```

Use the native scripts first unless a dependency explicitly requires Linux.
