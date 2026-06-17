from pathlib import Path

from ecse_localizer.release_check import run_release_check


def write_minimal_repo(root: Path, *, pyproject_version: str = "0.1.0", init_version: str = "0.1.0") -> None:
    (root / "src" / "ecse_localizer").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "tools").mkdir()
    (root / "pyproject.toml").write_text(
        f"""
[project]
name = "test"
version = "{pyproject_version}"
""",
        encoding="utf-8",
    )
    (root / "src" / "ecse_localizer" / "__init__.py").write_text(f'__version__ = "{init_version}"\n', encoding="utf-8")
    for rel in [
        "README.md",
        "DEPLOY_CONTABO_PROMPT.md",
        "deploy/config.remote.example.yaml",
        "deploy/REMOTE_TUNNEL_GUIDE.md",
        "deploy/docker-compose.yml",
        "deploy/Dockerfile.web",
        "deploy/Caddyfile.example",
        "deploy/nginx.conf.example",
        "deploy/bootstrap_contabo.py",
        ".env.example",
        "licenses_report.md",
        "tools/secret_scan.ps1",
        "config.example.yaml",
        "08_deploy_check.ps1",
        "09_worker_healthcheck.ps1",
        "10_release_check.ps1",
        "11_remote_smoke.ps1",
        "12_platform_check.ps1",
    ]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("placeholder\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "\n".join(
            [
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
        ),
        encoding="utf-8",
    )


def test_release_check_current_repo_passes():
    result = run_release_check(Path(__file__).resolve().parents[1])

    assert result["pass"] is True
    assert result["errors"] == 0


def test_release_check_detects_version_mismatch(tmp_path):
    write_minimal_repo(tmp_path, pyproject_version="0.1.0", init_version="0.2.0")

    result = run_release_check(tmp_path)

    assert result["pass"] is False
    assert "version_mismatch" in {item["code"] for item in result["findings"]}


def test_release_check_detects_missing_ignore_pattern(tmp_path):
    write_minimal_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("config.yaml\n", encoding="utf-8")

    result = run_release_check(tmp_path)

    assert result["pass"] is False
    assert "gitignore_pattern_missing" in {item["code"] for item in result["findings"]}
