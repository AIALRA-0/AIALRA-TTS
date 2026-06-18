from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


@dataclass
class LocalLLMStatus:
    available: bool
    backend: str
    endpoint: str
    model: str | None
    message: str


def assert_loopback(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"Refusing non-local LLM endpoint: {endpoint}")


class LocalLLMClient:
    def __init__(self, config: dict):
        self.config = config
        self.endpoint = config.get("llm", {}).get("endpoint", "http://127.0.0.1:11434/v1").rstrip("/")
        assert_loopback(self.endpoint)
        self.model: str | None = None
        self.session = requests.Session()
        self._status_cache: LocalLLMStatus | None = None

    def status(self, refresh: bool = False) -> LocalLLMStatus:
        if self._status_cache is not None and self._status_cache.available and not refresh:
            return self._status_cache
        try:
            with self.session.get(f"{self.endpoint}/models", timeout=1.5) as res:
                res.raise_for_status()
                data = res.json()
        except Exception as exc:
            return LocalLLMStatus(False, "none", self.endpoint, None, f"local LLM unavailable: {exc}")
        models = [m.get("id") or m.get("name") for m in data.get("data", []) if isinstance(m, dict)]
        candidates = self.config.get("llm", {}).get("model_candidates", [])
        self.model = next((m for m in candidates if m in models), models[0] if models else None)
        if not self.model:
            return LocalLLMStatus(False, "none", self.endpoint, None, "endpoint has no models")
        self._status_cache = LocalLLMStatus(True, "openai_compatible_local", self.endpoint, self.model, "ready")
        return self._status_cache

    def json_chat(self, system: str, user: str, schema_hint: str) -> dict:
        status = self.status()
        if not status.available or not status.model:
            raise RuntimeError(status.message)
        payload = {
            "model": status.model,
            "temperature": self.config.get("llm", {}).get("temperature", 0.2),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"{user}\n\nReturn JSON matching this shape:\n{schema_hint}"},
            ],
        }
        retries = int(self.config.get("llm", {}).get("max_retries", 5))
        last_error = ""
        for _ in range(retries):
            try:
                timeout = int(self.config.get("llm", {}).get("timeout_seconds", 120))
                with self.session.post(f"{self.endpoint}/chat/completions", json=payload, timeout=timeout) as res:
                    res.raise_for_status()
                    content = res.json()["choices"][0]["message"]["content"]
                return repair_json(content)
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(f"LLM JSON call failed: {last_error}")


def repair_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
