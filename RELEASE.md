# Release Rules

This repository is safe to publish only when the release gate passes. Do not include local media, generated subtitles, logs, model weights, production config, hostnames, IPs, passwords, or worker tokens in git.

## Versioning

- Use SemVer: `MAJOR.MINOR.PATCH`.
- Keep `pyproject.toml` `project.version` and `src/ecse_localizer/__init__.py` `__version__` identical.
- Tag releases as `vX.Y.Z` after the release gate passes.
- Use patch releases for fixes that do not change job schema or generated output contracts.
- Use minor releases for new WebUI/worker capabilities, deployment options, translation/TTS behavior changes, or output metadata additions.
- Use major releases only for incompatible config, API, job-schema, or artifact-layout changes.

## Required Gate

Run locally before tagging:

```powershell
.\tools\secret_scan.ps1
python -m pytest -q
python -m compileall .\src\ecse_localizer
node --check .\src\ecse_localizer\static\app.js
python -m ecse_localizer translation-sample --output ".\runs\translation_quality_sample"
python -m ecse_localizer worker-health --skip-remote
python -m ecse_localizer --config deploy\config.remote.example.yaml deploy-check
python -m ecse_localizer release-check
```

`deploy-check` is expected to fail on `deploy/config.remote.example.yaml` until placeholder secrets are replaced in a real deployment config. For release gating, this proves placeholders are still present in the public template and catches accidental unsafe edits. A production `deploy/config.remote.yaml` must pass `deploy-check` before exposure.

## Tagging

```powershell
git status -sb
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

## Release Notes

Every release note should state:

- Translation pipeline changes.
- WebUI/API/worker changes.
- Deployment/config changes.
- Security or quota behavior changes.
- Migration notes for existing job JSON or platform metadata.
- Known limitations and manual validation still required.
