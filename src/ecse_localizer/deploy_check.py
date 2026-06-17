from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any


Finding = dict[str, str]

PLACEHOLDER_MARKERS = (
    "change-me",
    "example.invalid",
    "replace-me",
    "your-domain",
    "your_",
    "<generated",
    "<token",
    "<secret",
)

SECRET_MIN_LENGTHS = {
    "webui.session_secret": 32,
    "webui.download_secret": 32,
    "webui.worker_token": 32,
    "webui.password": 12,
}


def check_deploy_config(config: dict[str, Any], *, mode: str = "remote") -> dict[str, Any]:
    findings: list[Finding] = []
    if mode != "remote":
        add(findings, "error", "unsupported_mode", "mode", "deploy-check currently validates remote/Contabo deployments only.")
        return build_result(findings)

    privacy = get_dict(config, "privacy")
    require_false(findings, privacy, "allow_cloud_api", "privacy.allow_cloud_api")
    require_false(findings, privacy, "allow_upload_media", "privacy.allow_upload_media")
    require_false(findings, privacy, "allow_voice_clone_without_consent", "privacy.allow_voice_clone_without_consent")

    webui = get_dict(config, "webui")
    require_true(findings, webui, "enabled", "webui.enabled")
    require_true(findings, webui, "cookie_secure", "webui.cookie_secure")
    require_true(findings, webui, "csrf_origin_check", "webui.csrf_origin_check")
    if str(webui.get("execution_mode", "")).lower() != "worker_queue":
        add(findings, "error", "worker_queue_required", "webui.execution_mode", "Contabo must queue jobs for the Windows worker.")
    if bool(webui.get("allow_remote_media_uploads", False)):
        add(findings, "error", "remote_media_uploads_enabled", "webui.allow_remote_media_uploads", "Original videos should stay on the Windows worker by default.")
    if bool(webui.get("allow_worker_path_submission", False)):
        add(
            findings,
            "error",
            "worker_path_submission_enabled",
            "webui.allow_worker_path_submission",
            "Use opaque worker-ref media IDs in production so Contabo does not store Windows source paths.",
        )
    if bool(webui.get("bind_local_only", False)):
        add(findings, "warn", "bind_local_only_remote", "webui.bind_local_only", "Remote container deployments usually bind 0.0.0.0 behind Caddy/Nginx.")

    auth_mode = str(webui.get("worker_auth_mode", "")).lower()
    if auth_mode not in {"hmac", "signed", "signature"}:
        add(findings, "error", "weak_worker_auth", "webui.worker_auth_mode", "Production worker APIs must require HMAC signed requests.")
    require_true(findings, webui, "worker_require_nonce", "webui.worker_require_nonce")

    for path, min_len in SECRET_MIN_LENGTHS.items():
        value = nested(config, path)
        check_secret(findings, path, value, min_len)
    check_distinct_secrets(findings, config)

    check_numeric_range(findings, webui, "signed_url_ttl_seconds", "webui.signed_url_ttl_seconds", min_value=60, max_value=3600, level="warn")
    check_numeric_range(findings, webui, "worker_signature_max_skew_seconds", "webui.worker_signature_max_skew_seconds", min_value=30, max_value=600, level="warn")
    check_numeric_range(findings, webui, "worker_offline_after_seconds", "webui.worker_offline_after_seconds", min_value=90, max_value=600, level="warn")
    check_numeric_range(findings, webui, "cleanup_older_than_days", "webui.cleanup_older_than_days", min_value=1, max_value=30, level="warn")
    check_numeric_range(findings, webui, "global_remote_quota_gb", "webui.global_remote_quota_gb", min_value=1, max_value=10240, level="warn")
    check_numeric_range(findings, webui, "default_remote_quota_gb", "webui.default_remote_quota_gb", min_value=0.1, max_value=100, level="warn")
    check_numeric_range(findings, webui, "worker_preview_max_upload_mb", "webui.worker_preview_max_upload_mb", min_value=1, max_value=512, level="warn")
    check_numeric_range(findings, webui, "worker_artifact_cache_max_upload_mb", "webui.worker_artifact_cache_max_upload_mb", min_value=1, max_value=4096, level="warn")
    check_numeric_range(findings, webui, "max_active_jobs_per_user", "webui.max_active_jobs_per_user", min_value=1, max_value=20, level="warn")
    check_numeric_range(findings, webui, "max_active_jobs_global", "webui.max_active_jobs_global", min_value=1, max_value=100, level="warn")

    check_remote_paths(findings, config)
    check_capability_hints(findings, config)
    return build_result(findings)


def build_result(findings: list[Finding]) -> dict[str, Any]:
    errors = sum(1 for item in findings if item["level"] == "error")
    warnings = sum(1 for item in findings if item["level"] == "warn")
    return {
        "pass": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "findings": findings,
    }


def add(findings: list[Finding], level: str, code: str, path: str, message: str) -> None:
    findings.append({"level": level, "code": code, "path": path, "message": message})


def get_dict(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key, {})
    return value if isinstance(value, dict) else {}


def nested(config: dict[str, Any], dotted: str) -> Any:
    value: Any = config
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def require_false(findings: list[Finding], section: dict[str, Any], key: str, path: str) -> None:
    if section.get(key) is not False:
        add(findings, "error", "must_be_false", path, "This value must be false for the remote privacy boundary.")


def require_true(findings: list[Finding], section: dict[str, Any], key: str, path: str) -> None:
    if section.get(key) is not True:
        add(findings, "error", "must_be_true", path, "This value must be true for production safety.")


def check_secret(findings: list[Finding], path: str, value: Any, min_length: int) -> None:
    text = str(value or "")
    if not text:
        add(findings, "error", "secret_missing", path, "Required secret is empty.")
        return
    lowered = text.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS) or "${" in text or "%{" in text:
        add(findings, "error", "secret_placeholder", path, "Replace template placeholders with deployment-specific secrets.")
        return
    if len(text) < min_length:
        add(findings, "error", "secret_too_short", path, f"Secret must be at least {min_length} characters.")


def check_distinct_secrets(findings: list[Finding], config: dict[str, Any]) -> None:
    secret_paths = ["webui.session_secret", "webui.download_secret", "webui.worker_token"]
    seen: dict[str, str] = {}
    for path in secret_paths:
        value = nested(config, path)
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            add(findings, "error", "secret_reused", path, f"Do not reuse the same secret as {seen[value]}.")
        else:
            seen[value] = path


def check_numeric_range(
    findings: list[Finding],
    section: dict[str, Any],
    key: str,
    path: str,
    *,
    min_value: float,
    max_value: float,
    level: str,
) -> None:
    try:
        value = float(section.get(key))
    except (TypeError, ValueError):
        add(findings, "error", "number_required", path, "A numeric value is required.")
        return
    if value < min_value:
        add(findings, level, "number_below_recommended", path, f"Recommended minimum is {min_value}.")
    if value > max_value:
        add(findings, level, "number_above_recommended", path, f"Recommended maximum is {max_value}.")


def check_remote_paths(findings: list[Finding], config: dict[str, Any]) -> None:
    for path, value in walk_strings(config):
        if path == "project_root":
            continue
        text = value.strip()
        if looks_like_windows_path(text):
            add(findings, "error", "windows_path_in_remote_config", path, "Remote config must not contain local Windows paths.")
        if has_private_ip(text):
            add(findings, "error", "private_ip_in_remote_config", path, "Do not commit or deploy templates with private IP addresses.")
    for path in ["input_dir", "output_dir", "work_dir", "webui.upload_dir", "webui.preview_dir", "webui.job_dir", "webui.platform_dir"]:
        value = nested(config, path)
        if not isinstance(value, str) or not value:
            add(findings, "error", "path_missing", path, "Required deployment path is missing.")
            continue
        if "${" in value:
            add(findings, "error", "unresolved_env_var", path, "Environment variable placeholder was not expanded.")
        if value.startswith("/"):
            PurePosixPath(value)
        else:
            add(findings, "warn", "relative_remote_path", path, "Use absolute POSIX paths on Contabo.")
    if nested(config, "input_dir") == nested(config, "output_dir"):
        add(findings, "error", "input_output_same_path", "input_dir", "Remote input and output directories must be separate.")


def check_capability_hints(findings: list[Finding], config: dict[str, Any]) -> None:
    asr = nested(config, "asr.supported_languages")
    translation = nested(config, "translation.supported_target_languages")
    tts = nested(config, "tts.supported_languages")
    if not isinstance(asr, list) or not asr:
        add(findings, "warn", "missing_asr_hints", "asr.supported_languages", "Set fallback ASR language hints for worker-offline UI state.")
    if not isinstance(translation, list) or not translation:
        add(findings, "warn", "missing_translation_hints", "translation.supported_target_languages", "Set fallback subtitle target-language hints.")
    if not isinstance(tts, list) or not tts:
        add(findings, "warn", "missing_tts_hints", "tts.supported_languages", "Set fallback TTS language hints.")


def walk_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(walk_strings(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{prefix}[{index}]"
            rows.extend(walk_strings(item, child))
    elif isinstance(value, str):
        rows.append((prefix, value))
    return rows


def looks_like_windows_path(value: str) -> bool:
    return bool(re.search(r"(^|[\s\"'])([A-Za-z]:\\|\\\\|/mnt/[a-z]/)", value))


def has_private_ip(value: str) -> bool:
    for match in re.finditer(r"\b(\d{1,3})(?:\.(\d{1,3})){3}\b", value):
        parts = [int(part) for part in match.group(0).split(".")]
        if any(part > 255 for part in parts):
            continue
        if parts[0] == 10 or parts[0] == 127:
            return True
        if parts[0] == 192 and parts[1] == 168:
            return True
        if parts[0] == 172 and 16 <= parts[1] <= 31:
            return True
    return False
