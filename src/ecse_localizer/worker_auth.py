from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
from typing import Any


UNSIGNED_WORKER_HEADERS = {
    "x-worker-auth",
    "x-worker-nonce",
    "x-worker-signature",
    "x-worker-timestamp",
    "x-worker-token",
}


def canonical_worker_signed_headers(headers: Mapping[str, Any] | None = None) -> str:
    rows: list[tuple[str, str]] = []
    for key, value in (headers or {}).items():
        name = str(key or "").strip().lower()
        if not name.startswith("x-worker-") or name in UNSIGNED_WORKER_HEADERS:
            continue
        text = " ".join(str(value or "").strip().split())
        if text:
            rows.append((name, text))
    return "\n".join(f"{name}:{text}" for name, text in sorted(rows))


def worker_hmac_signature(
    worker_token: str,
    *,
    timestamp: str,
    method: str,
    path: str,
    body: bytes,
    nonce: str | None = None,
    headers: Mapping[str, Any] | None = None,
) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    parts = [str(timestamp), method.upper(), path]
    if nonce:
        parts.append(str(nonce))
    signed_headers = canonical_worker_signed_headers(headers)
    if signed_headers:
        parts.append(signed_headers)
    parts.append(body_hash)
    message = "\n".join(parts).encode("utf-8")
    return hmac.new(worker_token.encode("utf-8"), message, hashlib.sha256).hexdigest()
