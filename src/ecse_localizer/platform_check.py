from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__
from .capabilities import language_capabilities
from .deploy_check import check_deploy_config
from .llm_local import LocalLLMClient
from .metrics import collect_system_metrics
from .release_check import run_release_check
from .remote_smoke import run_remote_smoke
from .translation_sample import write_translation_quality_sample
from .tts import tts_health
from .utils import PROJECT_ROOT, ensure_dir, write_json
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
        "worker_id": worker_id,
        "version": __version__,
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
        if gate.get("placeholder_errors") is not None:
            lines.append(f"- Template placeholder checks: {gate.get('placeholder_errors', 0)}")
        for finding in gate.get("findings", [])[:20]:
            lines.append(f"- {str(finding.get('level', '')).upper()} [{finding.get('code')}] {finding.get('path')}: {finding.get('message')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
