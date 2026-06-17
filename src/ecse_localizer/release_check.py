from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any


Finding = dict[str, str]

REQUIRED_FILES = [
    "README.md",
    "DEPLOY_CONTABO_PROMPT.md",
    "deploy/config.remote.example.yaml",
    "deploy/REMOTE_TUNNEL_GUIDE.md",
    "deploy/docker-compose.yml",
    "deploy/Dockerfile.web",
    "deploy/Caddyfile.example",
    "deploy/nginx.conf.example",
    "deploy/bootstrap_contabo.py",
    "deploy/systemd/aialra-worker-tunnel.service",
    ".env.example",
    "licenses_report.md",
    "tools/secret_scan.ps1",
    "tools/check_powershell_syntax.ps1",
    "config.example.yaml",
    "08_deploy_check.ps1",
    "09_worker_healthcheck.ps1",
    "10_release_check.ps1",
    "11_remote_smoke.ps1",
    "12_platform_check.ps1",
    "13_start_worker.ps1",
    "install_worker_task.ps1",
]

REQUIRED_GITIGNORE_PATTERNS = [
    "config.yaml",
    ".env",
    "deploy/config.remote.yaml",
    "logs/",
    "runs/",
    "models/",
    "_localizer_output/",
    "*.mp4",
    "*.wav",
    "*.srt",
    "*.vtt",
    "*.ass",
]

DANGEROUS_TRACKED_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".wav",
    ".mp3",
    ".flac",
    ".srt",
    ".vtt",
    ".ass",
    ".log",
    ".env",
}


def run_release_check(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root)
    findings: list[Finding] = []
    check_versions(root, findings)
    check_required_files(root, findings)
    check_gitignore(root, findings)
    check_tracked_files(root, findings)
    errors = sum(1 for item in findings if item["level"] == "error")
    warnings = sum(1 for item in findings if item["level"] == "warn")
    return {
        "pass": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "findings": findings,
    }


def check_versions(root: Path, findings: list[Finding]) -> None:
    pyproject = root / "pyproject.toml"
    init_py = root / "src" / "ecse_localizer" / "__init__.py"
    if not pyproject.exists() or not init_py.exists():
        add(findings, "error", "version_file_missing", "version", "pyproject.toml and package __init__.py are required.")
        return
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    pyproject_version = str(project.get("project", {}).get("version", ""))
    init_version = read_init_version(init_py)
    semver = r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?$"
    if not re.match(semver, pyproject_version):
        add(findings, "error", "invalid_pyproject_version", "pyproject.toml:project.version", "Use SemVer such as 0.1.0.")
    if not re.match(semver, init_version):
        add(findings, "error", "invalid_package_version", "src/ecse_localizer/__init__.py:__version__", "Use SemVer such as 0.1.0.")
    if pyproject_version != init_version:
        add(findings, "error", "version_mismatch", "version", "pyproject.toml and package __version__ must match.")


def read_init_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant):
                        return str(node.value.value)
    return ""


def check_required_files(root: Path, findings: list[Finding]) -> None:
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            add(findings, "error", "required_file_missing", rel, "Release package is missing a required file.")


def check_gitignore(root: Path, findings: list[Finding]) -> None:
    path = root / ".gitignore"
    if not path.exists():
        add(findings, "error", "gitignore_missing", ".gitignore", "A release must protect local outputs and secrets.")
        return
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")}
    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        if pattern not in lines:
            add(findings, "error", "gitignore_pattern_missing", ".gitignore", f"Missing ignore pattern: {pattern}")


def check_tracked_files(root: Path, findings: list[Finding]) -> None:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception as exc:
        add(findings, "warn", "git_ls_files_failed", "git", type(exc).__name__)
        return
    if proc.returncode != 0:
        add(findings, "warn", "git_ls_files_failed", "git", "Unable to inspect tracked files.")
        return
    for rel in proc.stdout.splitlines():
        path = Path(rel)
        lower = rel.lower()
        if lower == ".env" or lower.endswith("/config.yaml") or lower == "config.yaml":
            add(findings, "error", "tracked_secret_config", rel, "Do not track local config or env files.")
        if path.suffix.lower() in DANGEROUS_TRACKED_SUFFIXES:
            add(findings, "error", "tracked_generated_or_media_file", rel, "Do not track generated media, subtitles, logs, or env files.")
        if any(part in {"logs", "runs", "models", "_localizer_output"} for part in path.parts):
            add(findings, "error", "tracked_generated_directory", rel, "Do not track generated output directories.")


def add(findings: list[Finding], level: str, code: str, path: str, message: str) -> None:
    findings.append({"level": level, "code": code, "path": path, "message": message})
