from __future__ import annotations

import re
from dataclasses import dataclass


PROTECT_PATTERNS = [
    r"`[^`]+`",
    r"https?://[^\s<>()]+",
    r"[A-Za-z]:\\(?:[^\s\\/:*?\"<>|\r\n]+\\)*[^\s\\/:*?\"<>|\r\n]+",
    r"\bO\([^)]+\)",
    r"\b[A-Za-z]_[A-Za-z0-9]+\b",
    r"\b(?:ResNet|VGG|BERT|Qwen|GPT|Llama|ECSE|CD-SEM|AMHS|EUV|ALD|CMP|SPC|OPC)(?:[-\s]?\d+(?:\.\d+)*)?\b",
    r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*\b",
    r"\bECSE\s+\d+\b",
    r"\b\d+(?:\.\d+)?\s?(?:V|kHz|MHz|GHz|Hz|nm|um|µm|mm|cm|mA|A|W|eV|%)\b",
    r"\b\d+(?:st|nd|rd|th)\b",
    r"\b\d+(?:,\d{3})*(?:\.\d+)?\b",
    r"\b\d+\^-?\d+\b",
    r"\bv?\d+(?:\.\d+){1,3}\b",
    r"\b[A-Za-z][A-Za-z0-9_./-]*\.(?:py|js|ts|json|yaml|yml|csv|tsv|txt|md|mp4|wav|srt|vtt|ass)\b",
]


@dataclass
class ProtectionResult:
    text: str
    mapping: dict[str, str]


def protect_text(text: str) -> ProtectionResult:
    matches: list[tuple[int, int, str]] = []
    for pattern in PROTECT_PATTERNS:
        for match in re.finditer(pattern, text):
            matches.append((match.start(), match.end(), match.group(0)))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    selected: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, value in matches:
        if start >= last_end:
            selected.append((start, end, value))
            last_end = end

    mapping: dict[str, str] = {}
    out: list[str] = []
    cursor = 0
    for idx, (start, end, value) in enumerate(selected, start=1):
        placeholder = f"<KEEP_{idx:03d}>"
        out.append(text[cursor:start])
        out.append(placeholder)
        mapping[placeholder] = value
        cursor = end
    out.append(text[cursor:])
    return ProtectionResult("".join(out), mapping)


def restore_text(text: str, mapping: dict[str, str]) -> str:
    restored = text
    for placeholder, value in mapping.items():
        restored = restored.replace(placeholder, value)
    return restored


def protected_roundtrip(text: str) -> bool:
    result = protect_text(text)
    return restore_text(result.text, result.mapping) == text
