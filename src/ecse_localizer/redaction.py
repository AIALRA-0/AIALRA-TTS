from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


PATH_KEYS = {"path", "source_path", "local_path", "input_dir", "output_dir", "work_dir", "job_config", "config_path"}
PATH_SUFFIXES = ("_path", "_dir")
SECRET_FLAG_NAMES = {
    "--token",
    "--worker-token",
    "--api-key",
    "--apikey",
    "--secret",
    "--password",
    "--passwd",
    "--key",
}
PATH_FLAG_NAMES = {
    "--config",
    "--input",
    "--output",
    "--work-dir",
    "--video",
    "--reference-audio",
    "--reference-text",
    "--report",
}
SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "signature",
    "sig",
    "password",
    "auth",
}

QUOTED_WINDOWS_PATH_RE = re.compile(r"(?P<quote>['\"])(?P<path>[A-Za-z]:\\[^'\"\r\n]+)(?P=quote)")
WINDOWS_PATH_WITH_EXT_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\r\n'\"<>|]+\\)*[^\\\r\n'\"<>|]*\.[A-Z0-9]{1,12}\b")
WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\r\n'\"<>|]+\\)+[^\\\r\n'\"<>|]*")
POSIX_PATH_RE = re.compile(r"(?i)(?<![\w.-])/(?:home|users|mnt|media|root|srv|tmp|var|opt|workspace|data)(?:/[^\s'\"<>|]+)+")
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
AUTH_BEARER_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|worker[_-]?token|api[_-]?key|apikey|secret|password|passwd|signature)\b(\s*[:=]\s*)[^\s,;]+"
)
PRIVATE_IPV4_RE = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2}\b")
TOKEN_RE = re.compile(r"\b(?:hf_|ghp_|github_pat_|sk-)[A-Za-z0-9_\-]{12,}\b")


def is_remote_safe_reference(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith("worker-ref:"))


def sanitize_remote_text(value: Any) -> str:
    """Redact local machine paths and obvious credentials from remote-safe summaries."""
    text = str(value or "")
    if not text:
        return ""
    text = URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = QUOTED_WINDOWS_PATH_RE.sub(lambda match: f"{match.group('quote')}<local-path>{match.group('quote')}", text)
    text = WINDOWS_PATH_WITH_EXT_RE.sub("<local-path>", text)
    text = WINDOWS_PATH_RE.sub("<local-path>", text)
    text = POSIX_PATH_RE.sub("<local-path>", text)
    text = AUTH_BEARER_RE.sub(r"\1<redacted>", text)
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    text = TOKEN_RE.sub("<redacted-token>", text)
    text = PRIVATE_IPV4_RE.sub("<private-ip>", text)
    return text


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<url>"
    netloc = parsed.hostname or parsed.netloc
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    query_pairs = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS or any(token in key.lower() for token in ("token", "secret", "key", "sig", "auth", "password")):
            query_pairs.append((key, "<redacted>"))
        else:
            query_pairs.append((key, item))
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def sanitize_remote_command(command: Any) -> list[str]:
    if isinstance(command, str):
        items = command.split()
    elif isinstance(command, list):
        items = [str(item) for item in command]
    else:
        return []
    cleaned: list[str] = []
    redact_next = False
    local_path_next = False
    for item in items:
        lowered = item.lower()
        if redact_next:
            cleaned.append("<redacted>")
            redact_next = False
            continue
        if local_path_next:
            is_placeholder = item.startswith("<") and item.endswith(">")
            cleaned.append(item if is_placeholder or is_remote_safe_reference(item) else "<local-path>")
            local_path_next = False
            continue
        if lowered in SECRET_FLAG_NAMES:
            cleaned.append(item)
            redact_next = True
            continue
        if lowered in PATH_FLAG_NAMES:
            cleaned.append(item)
            local_path_next = True
            continue
        if any(lowered.startswith(f"{flag}=") for flag in SECRET_FLAG_NAMES):
            key = item.split("=", 1)[0]
            cleaned.append(f"{key}=<redacted>")
            continue
        if any(lowered.startswith(f"{flag}=") for flag in PATH_FLAG_NAMES):
            key = item.split("=", 1)[0]
            value = item.split("=", 1)[1]
            cleaned.append(item if is_remote_safe_reference(value) else f"{key}=<local-path>")
            continue
        cleaned.append(sanitize_remote_text(item))
    return cleaned


def sanitize_remote_value(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in PATH_KEYS or key_lower.endswith(PATH_SUFFIXES):
                output[key_text] = "<local-path>"
            elif any(token in key_lower for token in ("token", "secret", "password", "api_key", "apikey")):
                output[key_text] = "<redacted>"
            else:
                output[key_text] = sanitize_remote_value(item)
        return output
    if isinstance(value, list):
        return [sanitize_remote_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_remote_text(value)
    return value
