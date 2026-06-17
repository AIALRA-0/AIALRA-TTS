from __future__ import annotations

from typing import Any

from .platform_store import safe_worker_id


Finding = dict[str, str]


def assess_worker_health(
    payload: dict[str, Any],
    *,
    remote_checked: bool = False,
    remote_ok: bool = False,
    remote_error_type: str = "",
) -> dict[str, Any]:
    findings: list[Finding] = []
    privacy = payload.get("privacy") if isinstance(payload.get("privacy"), dict) else {}
    if privacy.get("allow_cloud_api") is not False:
        add(findings, "error", "cloud_api_not_disabled", "privacy.allow_cloud_api", "Worker must keep cloud inference disabled.")
    if privacy.get("allow_upload_media") is not False:
        add(findings, "error", "media_upload_not_disabled", "privacy.allow_upload_media", "Worker must not upload media to third-party services.")
    if privacy.get("allow_voice_clone_without_consent") is not False:
        add(findings, "error", "voice_clone_consent_boundary_disabled", "privacy.allow_voice_clone_without_consent", "Voice clone consent guard must stay enabled.")

    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    gpu_rows = metrics.get("gpu") if isinstance(metrics.get("gpu"), list) else []
    if not any(isinstance(row, dict) and row.get("available") for row in gpu_rows):
        add(findings, "warn", "gpu_not_detected", "metrics.gpu", "GPU metrics are unavailable; worker can still run CPU fallbacks but throughput will be lower.")
    if not isinstance(metrics.get("cpu"), dict) or "load_percent" not in metrics.get("cpu", {}):
        add(findings, "warn", "cpu_metrics_missing", "metrics.cpu", "CPU load metric is missing.")
    if not isinstance(metrics.get("memory"), dict) or "used_percent" not in metrics.get("memory", {}):
        add(findings, "warn", "memory_metrics_missing", "metrics.memory", "Memory metric is missing.")
    local_storage = metrics.get("local_storage") if isinstance(metrics.get("local_storage"), dict) else {}
    if not local_storage:
        add(findings, "warn", "local_storage_missing", "metrics.local_storage", "Local managed-storage usage is missing.")

    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    if not capabilities:
        add(findings, "warn", "capabilities_missing", "capabilities", "Worker language capability summary is missing.")
    else:
        if not component_available(capabilities.get("asr")):
            add(findings, "warn", "asr_capability_unknown", "capabilities.asr", "ASR capability is unavailable or unknown.")
        if not component_available(capabilities.get("translation")):
            add(findings, "warn", "translation_capability_unknown", "capabilities.translation", "Translation capability is unavailable or unknown.")
        if not component_available(capabilities.get("tts")):
            add(findings, "warn", "tts_capability_unknown", "capabilities.tts", "TTS capability is unavailable or unknown.")

    if remote_checked and not remote_ok:
        code = "remote_heartbeat_failed"
        message = "Signed heartbeat failed; check tunnel, remote URL, worker token, and Contabo worker auth settings."
        if remote_error_type:
            message += f" Error type: {remote_error_type}."
        add(findings, "error", code, "remote.heartbeat", message)

    errors = sum(1 for item in findings if item["level"] == "error")
    warnings = sum(1 for item in findings if item["level"] == "warn")
    return {
        "pass": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "worker_id": safe_worker_id(payload.get("worker_id"), default=""),
        "version": str(payload.get("version") or ""),
        "remote": {"checked": remote_checked, "ok": bool(remote_ok) if remote_checked else None},
        "findings": findings,
    }


def component_available(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("available") is False:
        return False
    if value.get("current_supported") is False:
        return False
    if value.get("supported_languages") or value.get("supported_target_languages"):
        return True
    return bool(value.get("available") or value.get("auto_detect") or value.get("supports_arbitrary_targets"))


def add(findings: list[Finding], level: str, code: str, path: str, message: str) -> None:
    findings.append({"level": level, "code": code, "path": path, "message": message})
