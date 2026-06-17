from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__
from .capabilities import language_capabilities
from .deploy_check import check_deploy_config
from .llm_local import LocalLLMClient
from .metrics import collect_system_metrics
from .platform_store import safe_worker_id
from .release_check import run_release_check
from .remote_smoke import run_remote_smoke
from .translation_sample import write_translation_quality_sample
from .tts import tts_health
from .utils import PROJECT_ROOT, ensure_dir, write_json
from .worker_client import worker_concurrency
from .worker_health import assess_worker_health


GateFn = Callable[[], dict[str, Any]]


def run_platform_check(
    config: dict[str, Any],
    *,
    output_dir: str | Path | None = None,
    worker_payload: dict[str, Any] | None = None,
    worker_id: str = "local-windows-worker",
) -> dict[str, Any]:
    out = ensure_dir(output_dir) if output_dir else None
    gates: dict[str, dict[str, Any]] = {}

    gates["release_check"] = run_gate("release_check", lambda: run_release_check(config.get("project_root") or PROJECT_ROOT))
    gates["translation_sample"] = run_gate("translation_sample", lambda: translation_sample_gate(config, out))
    gates["remote_smoke"] = run_gate("remote_smoke", lambda: remote_smoke_gate(config, out))
    gates["webui_api_smoke"] = run_gate("webui_api_smoke", lambda: webui_api_smoke_gate(config, out))
    gates["worker_health_local"] = run_gate(
        "worker_health_local",
        lambda: assess_worker_health(worker_payload or build_worker_health_payload(config, worker_id=worker_id), remote_checked=False),
    )
    gates["deploy_template_guard"] = run_gate("deploy_template_guard", deploy_template_guard)

    failed = [name for name, gate in gates.items() if not gate.get("pass")]
    result = {
        "pass": not failed,
        "mode": "aialra_platform_check",
        "summary": {
            "checked_gates": len(gates),
            "failed_gates": failed,
        },
        "gates": gates,
    }
    if out:
        json_path = out / "platform_check_report.json"
        md_path = out / "platform_check_report.md"
        write_json(json_path, result)
        md_path.write_text(render_platform_check_markdown(result), encoding="utf-8")
        result["json"] = str(json_path)
        result["markdown"] = str(md_path)
    return result


def build_worker_health_payload(config: dict[str, Any], *, worker_id: str = "local-windows-worker") -> dict[str, Any]:
    tts = tts_health(config)
    llm = local_llm_status(config)
    return {
        "worker_id": safe_worker_id(worker_id),
        "version": __version__,
        "max_concurrent_jobs": worker_concurrency(config),
        "privacy": config.get("privacy", {}),
        "metrics": collect_system_metrics(config),
        "tts": tts,
        "llm": llm,
        "capabilities": language_capabilities(config, llm_status=llm, tts_status=tts),
    }


def local_llm_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return LocalLLMClient(config).status().__dict__
    except Exception as exc:
        return {"available": False, "backend": "none", "endpoint": "", "model": None, "message": type(exc).__name__}


def translation_sample_gate(config: dict[str, Any], output_dir: Path | None) -> dict[str, Any]:
    target = output_dir / "translation_quality_sample" if output_dir else None
    result = write_translation_quality_sample(target or Path(config["work_dir"]) / "translation_quality_sample", config)
    sample = result.get("sample", {})
    return {
        "pass": bool(result.get("pass")),
        "errors": 0 if result.get("pass") else 1,
        "warnings": 0,
        "json": result.get("json"),
        "markdown": result.get("markdown"),
        "checks": sample.get("checks", {}),
        "rows": len(sample.get("rows", [])) if isinstance(sample.get("rows"), list) else 0,
    }


def remote_smoke_gate(config: dict[str, Any], output_dir: Path | None) -> dict[str, Any]:
    target = output_dir / "remote_smoke" if output_dir else None
    result = run_remote_smoke(config, output_dir=target)
    return {
        "pass": bool(result.get("pass")),
        "errors": len(result.get("summary", {}).get("failed_steps", [])),
        "warnings": 0,
        "json": result.get("json"),
        "markdown": result.get("markdown"),
        "summary": result.get("summary", {}),
        "steps": result.get("steps", []),
    }


def webui_api_smoke_gate(config: dict[str, Any], output_dir: Path | None) -> dict[str, Any]:
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:
        return {
            "pass": False,
            "errors": 1,
            "warnings": 0,
            "findings": [
                {
                    "level": "error",
                    "code": "testclient_unavailable",
                    "path": "fastapi.testclient",
                    "message": type(exc).__name__,
                }
            ],
        }

    from .webui import create_app
    from .worker_client import worker_headers

    smoke_parent = output_dir or Path(config.get("work_dir") or PROJECT_ROOT / "runs") / "platform_check"
    root = reset_webui_smoke_root(smoke_parent)
    smoke_config = isolated_webui_smoke_config(config, root)
    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(smoke_config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    username = smoke_config["webui"]["username"]
    password = smoke_config["webui"]["password"]
    app = create_app(config_path)
    steps: list[dict[str, Any]] = []

    def add_step(name: str, response_status: int, passed: bool, detail: str = "") -> None:
        row: dict[str, Any] = {"name": name, "status_code": response_status, "pass": passed}
        if detail:
            row["detail"] = detail
        steps.append(row)

    with TestClient(app) as client:
        protected = client.get("/api/dashboard")
        add_step("auth_required", protected.status_code, protected.status_code in {401, 403}, "dashboard rejects anonymous access")

        login = client.post("/api/login", json={"username": username, "password": password})
        login_json = response_json(login)
        add_step("login", login.status_code, login.status_code == 200 and bool(login_json.get("ok")), "API login sets a session cookie")

        session = client.get("/api/session")
        session_json = response_json(session)
        add_step("session", session.status_code, session.status_code == 200 and session_json.get("authenticated") is True)

        dashboard = client.get("/api/dashboard")
        dashboard_json = response_json(dashboard)
        dashboard_ok = dashboard.status_code == 200 and all(
            key in dashboard_json for key in ["worker", "queue", "quota", "metrics", "capabilities", "upload_policy"]
        )
        add_step("dashboard", dashboard.status_code, dashboard_ok, "dashboard exposes worker, queue, quota, metrics, and capability state")

        quota = client.get("/api/quota")
        quota_json = response_json(quota)
        quota_ok = quota.status_code == 200 and all(key in quota_json for key in ["local_quota_bytes", "remote_quota_bytes"])
        add_step("quota", quota.status_code, quota_ok)

        metrics = client.get("/api/metrics")
        metrics_json = response_json(metrics)
        metrics_ok = metrics.status_code == 200 and all(key in metrics_json for key in ["metrics", "worker", "queue", "quota"])
        add_step("metrics", metrics.status_code, metrics_ok)

        capabilities = client.get("/api/capabilities")
        capabilities_json = response_json(capabilities)
        capabilities_ok = capabilities.status_code == 200 and all(key in capabilities_json for key in ["asr", "translation", "tts"])
        add_step("capabilities", capabilities.status_code, capabilities_ok)

        artifacts = client.get("/api/artifacts")
        artifacts_json = response_json(artifacts)
        artifacts_ok = artifacts.status_code == 200 and isinstance(artifacts_json.get("artifacts"), list)
        add_step("artifacts", artifacts.status_code, artifacts_ok)

        users = client.get("/api/users")
        add_step("admin_users", users.status_code, users.status_code == 200 and isinstance(response_json(users).get("users"), list))

        created_user = client.post(
            "/api/users",
            json={
                "username": "student.smoke",
                "password": "student-smoke-password",
                "role": "user",
                "quota_local_gb": 1,
                "quota_remote_gb": 1,
            },
        )
        add_step("create_user", created_user.status_code, created_user.status_code == 200 and response_json(created_user).get("ok") is True)

        project_response = client.post(
            "/api/projects",
            json={"name": "Smoke Course", "description": "isolated platform-check project", "quota_project_gb": 1},
        )
        project_json = response_json(project_response)
        project = project_json.get("project") if isinstance(project_json.get("project"), dict) else {}
        add_step("create_project", project_response.status_code, project_response.status_code == 200 and bool(project.get("id")))

        folder_response = client.post(f"/api/projects/{project.get('id', '')}/folders", json={"name": "Week 1"})
        folder_json = response_json(folder_response)
        folder = folder_json.get("folder") if isinstance(folder_json.get("folder"), dict) else {}
        add_step("create_folder", folder_response.status_code, folder_response.status_code == 200 and bool(folder.get("id")))

        template_response = client.post(
            "/api/templates",
            json={
                "name": "Smoke Best Quality",
                "params": {
                    "source_language": "auto",
                    "target_subtitle_language": "zh-CN",
                    "target_tts_language": "zh-CN",
                    "quality_mode": "best_quality",
                    "tts_speed": 1.08,
                    "tts_end_gap_seconds": 0.25,
                    "mux_hard_subtitle": True,
                },
            },
        )
        template_json = response_json(template_response)
        template = template_json.get("template") if isinstance(template_json.get("template"), dict) else {}
        template_ok = (
            template_response.status_code == 200
            and template.get("params", {}).get("quality_mode") == "best_quality"
            and "unknown_secret" not in template.get("params", {})
        )
        add_step("create_template", template_response.status_code, template_ok)

        heartbeat_payload = {
            "status": "online",
            "worker_id": "windows-smoke-worker",
            "version": "platform-check",
            "metrics": {
                "cpu": {"load_percent": 18},
                "memory": {"used_percent": 32},
                "gpu": [{"available": True, "util_percent": 21, "memory_used_percent": 28}],
                "disk": {"free_bytes": 20 * 1024 * 1024 * 1024, "used_percent": 40},
            },
            "capabilities": {
                "asr": {"available": True, "supported_languages": ["auto", "en", "zh"]},
                "translation": {"available": True, "supported_target_languages": ["zh-CN", "ja", "es"]},
                "tts": {"available": True, "supported_languages": ["zh-CN"]},
            },
            "media_refs": [
                {
                    "ref_id": "smoke_media_1",
                    "name": r"D:\worker_secret_area\lecture.mp4",
                    "path": r"D:\worker_secret_area\lecture.mp4",
                    "size": 1024,
                    "media_type": "video/mp4",
                }
            ],
        }
        heartbeat_body = json.dumps(heartbeat_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        heartbeat = client.post(
            "/api/worker/heartbeat",
            content=heartbeat_body,
            headers=worker_headers(
                smoke_config["webui"]["worker_token"],
                path="/api/worker/heartbeat",
                body=heartbeat_body,
            ),
        )
        heartbeat_json = response_json(heartbeat)
        heartbeat_text = json.dumps(heartbeat_json, ensure_ascii=False)
        heartbeat_ok = heartbeat.status_code == 200 and "worker_secret_area" not in heartbeat_text.lower()
        add_step("signed_worker_heartbeat", heartbeat.status_code, heartbeat_ok)

        videos_response = client.get("/api/videos")
        videos_json = response_json(videos_response)
        videos_text = json.dumps(videos_json, ensure_ascii=False)
        videos_ok = (
            videos_response.status_code == 200
            and "worker-ref:smoke_media_1" in videos_text
            and "worker_secret_area" not in videos_text.lower()
        )
        add_step("worker_media_refs", videos_response.status_code, videos_ok)

        job_response = client.post(
            "/api/jobs",
            json={
                "type": "process_one",
                "video": "worker-ref:smoke_media_1",
                "video_name": "lecture.mp4",
                "project_id": project.get("id", ""),
                "folder_id": folder.get("id", "root"),
                "template_id": template.get("id", ""),
            },
        )
        job_json = response_json(job_response)
        job = job_json.get("job") if isinstance(job_json.get("job"), dict) else {}
        job_text = json.dumps(job_json, ensure_ascii=False)
        job_ok = (
            job_response.status_code == 200
            and job_json.get("dispatch", {}).get("target") == "worker"
            and job.get("status") == "queued"
            and job.get("metadata", {}).get("worker_args") == ["process-one", "--video", "worker-ref:smoke_media_1"]
            and job.get("metadata", {}).get("quality_mode") == "best_quality"
            and "worker_secret_area" not in job_text.lower()
        )
        add_step("queue_worker_ref_job", job_response.status_code, job_ok)

        jobs_response = client.get(f"/api/jobs?project_id={project.get('id', '')}&folder_id={folder.get('id', '')}")
        jobs_json = response_json(jobs_response)
        job_list = jobs_json.get("jobs") if isinstance(jobs_json.get("jobs"), list) else []
        jobs_ok = jobs_response.status_code == 200 and any(item.get("id") == job.get("id") for item in job_list)
        add_step("job_history_filter", jobs_response.status_code, jobs_ok)

        healthz = client.get("/healthz")
        healthz_json = response_json(healthz)
        health_ok = healthz.status_code == 200 and "ok" in healthz_json and "worker_token" not in str(healthz_json).lower()
        add_step("healthz_redacted", healthz.status_code, health_ok)

    failed = [step for step in steps if not step.get("pass")]
    return {
        "pass": not failed,
        "errors": len(failed),
        "warnings": 0,
        "config": str(config_path),
        "steps": steps,
    }


def isolated_webui_smoke_config(config: dict[str, Any], root: Path) -> dict[str, Any]:
    smoke = copy.deepcopy(config)
    smoke["input_dir"] = str(ensure_dir(root / "input"))
    smoke["output_dir"] = str(ensure_dir(root / "output"))
    smoke["work_dir"] = str(ensure_dir(root / "runs"))
    webui = smoke.setdefault("webui", {})
    webui["username"] = "platform-smoke-admin"
    webui["password"] = "platform-smoke-password"
    webui["session_secret"] = "platform-smoke-session-secret"
    webui["download_secret"] = "platform-smoke-download-secret"
    webui["worker_token"] = "platform-smoke-worker-token"
    webui["execution_mode"] = "worker_queue"
    webui["allow_worker_path_submission"] = False
    webui["platform_dir"] = str(root / "platform")
    webui["job_dir"] = str(root / "jobs")
    webui["upload_dir"] = str(root / "uploads")
    webui["preview_dir"] = str(root / "previews")
    webui["preview_manifest"] = str(root / "previews" / "preview_manifest.json")
    webui["worker_auth_mode"] = "hmac"
    webui["worker_require_nonce"] = True
    webui.setdefault("signed_url_ttl_seconds", 900)
    webui.setdefault("default_local_quota_gb", 1)
    webui.setdefault("default_remote_quota_gb", 1)
    webui.setdefault("default_project_quota_gb", 1)
    webui.setdefault("worker_disk_min_free_gb", 1)
    return smoke


def reset_webui_smoke_root(parent: Path) -> Path:
    parent = ensure_dir(parent)
    root = parent / "webui_api_smoke"
    resolved_parent = parent.resolve()
    resolved_root = root.resolve() if root.exists() else root
    if root.exists():
        if root.name != "webui_api_smoke" or not resolved_root.is_relative_to(resolved_parent):
            raise RuntimeError(f"Refusing to reset unsafe WebUI smoke directory: {root}")
        shutil.rmtree(root)
    return ensure_dir(root)


def response_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def deploy_template_guard() -> dict[str, Any]:
    template = PROJECT_ROOT / "deploy" / "config.remote.example.yaml"
    if not template.exists():
        return {
            "pass": False,
            "errors": 1,
            "warnings": 0,
            "findings": [
                {
                    "level": "error",
                    "code": "template_missing",
                    "path": "deploy/config.remote.example.yaml",
                    "message": "Remote deployment template is missing.",
                }
            ],
        }
    raw_config = yaml.safe_load(template.read_text(encoding="utf-8")) or {}
    result = check_deploy_config(raw_config, mode="remote")
    error_codes = [item["code"] for item in result.get("findings", []) if item.get("level") == "error"]
    unexpected = [code for code in error_codes if code != "secret_placeholder"]
    expected_placeholders = [code for code in error_codes if code == "secret_placeholder"]
    return {
        "pass": bool(expected_placeholders) and not unexpected,
        "expected_failure": True,
        "template": "deploy/config.remote.example.yaml",
        "errors": len(unexpected),
        "warnings": int(result.get("warnings", 0)),
        "placeholder_errors": len(expected_placeholders),
        "unexpected_error_codes": unexpected,
        "findings": result.get("findings", []),
    }


def run_gate(name: str, fn: GateFn) -> dict[str, Any]:
    try:
        result = fn()
        result.setdefault("pass", False)
        return result
    except Exception as exc:
        return {
            "pass": False,
            "errors": 1,
            "warnings": 0,
            "findings": [
                {
                    "level": "error",
                    "code": "gate_exception",
                    "path": name,
                    "message": type(exc).__name__,
                }
            ],
        }


def render_platform_check_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# AIALRA Platform Check",
        "",
        f"Status: {'PASS' if result.get('pass') else 'FAIL'}",
        f"Checked gates: {result.get('summary', {}).get('checked_gates', 0)}",
        f"Failed gates: {', '.join(result.get('summary', {}).get('failed_gates', [])) or 'none'}",
        "",
        "## Gates",
        "",
    ]
    for name, gate in result.get("gates", {}).items():
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"- Status: {'PASS' if gate.get('pass') else 'FAIL'}")
        if "errors" in gate:
            lines.append(f"- Errors: {gate.get('errors', 0)}")
        if "warnings" in gate:
            lines.append(f"- Warnings: {gate.get('warnings', 0)}")
        if gate.get("summary"):
            failed = gate["summary"].get("failed_steps", [])
            lines.append(f"- Failed steps: {', '.join(failed) or 'none'}")
        if gate.get("steps"):
            failed_steps = [str(step.get("name")) for step in gate.get("steps", []) if not step.get("pass")]
            lines.append(f"- Step failures: {', '.join(failed_steps) or 'none'}")
        if gate.get("placeholder_errors") is not None:
            lines.append(f"- Template placeholder checks: {gate.get('placeholder_errors', 0)}")
        for finding in gate.get("findings", [])[:20]:
            lines.append(f"- {str(finding.get('level', '')).upper()} [{finding.get('code')}] {finding.get('path')}: {finding.get('message')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
