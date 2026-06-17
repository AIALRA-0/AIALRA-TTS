from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


TIME_RE = re.compile(
    r"(?P<s>(?:\d{2}:)?\d{2}:\d{2}[\.,]\d{3})\s*-->\s*(?P<e>(?:\d{2}:)?\d{2}:\d{2}[\.,]\d{3})"
)


def parse_time(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        h = 0
        m, s = parts
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError(f"Invalid timestamp: {value}")
    return int(h) * 3600 + int(m) * 60 + float(s)


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    if ms == 1000:
        total += 1
        ms = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(",", ".")


def clean_subtitle_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = repair_caption_numeric_artifacts(text)
    return text


def repair_caption_numeric_artifacts(text: str) -> str:
    # Web captions sometimes turn counts like "543 chips" into "05:43 chips".
    count_units = r"chips?|dies?|wafers?|shuttles?|lots?|parts?|devices?|transistors?|samples?"
    return re.sub(rf"\b0?([1-9]):([0-9]{{2}})\b(?=\s+(?:{count_units})\b)", r"\1\2", text, flags=re.IGNORECASE)


def read_subtitles(path: str | Path) -> list[Segment]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".vtt":
        return read_vtt(path)
    if suffix == ".srt":
        return read_srt(path)
    if suffix == ".ass":
        try:
            import pysubs2

            subs = pysubs2.load(str(path), encoding="utf-8")
            return [
                Segment(i + 1, ev.start / 1000, ev.end / 1000, clean_subtitle_text(ev.plaintext))
                for i, ev in enumerate(subs)
                if clean_subtitle_text(ev.plaintext)
            ]
        except Exception as exc:
            raise RuntimeError(f"Failed to parse ASS subtitles {path}: {exc}") from exc
    raise ValueError(f"Unsupported subtitle format: {path}")


def read_vtt(path: str | Path) -> list[Segment]:
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    segments: list[Segment] = []
    i = 0
    while i < len(lines):
        match = TIME_RE.search(lines[i])
        if not match:
            i += 1
            continue
        start = parse_time(match.group("s"))
        end = parse_time(match.group("e"))
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            if not TIME_RE.search(lines[i]):
                text_lines.append(lines[i].strip())
            i += 1
        text = clean_subtitle_text(" ".join(text_lines))
        if text:
            segments.append(Segment(len(segments) + 1, start, end, text))
    return normalize_segments(segments)


def read_srt(path: str | Path) -> list[Segment]:
    content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n"))
    segments: list[Segment] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        time_line = next((ln for ln in lines if "-->" in ln), "")
        match = TIME_RE.search(time_line)
        if not match:
            continue
        time_index = lines.index(time_line)
        text = clean_subtitle_text(" ".join(lines[time_index + 1 :]))
        if text:
            segments.append(Segment(len(segments) + 1, parse_time(match.group("s")), parse_time(match.group("e")), text))
    return normalize_segments(segments)


def normalize_segments(segments: Iterable[Segment], max_end: float | None = None) -> list[Segment]:
    out: list[Segment] = []
    last_end = 0.0
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        start = max(0.0, seg.start)
        end = max(start + 0.05, seg.end)
        if max_end is not None:
            if start >= max_end:
                continue
            end = min(end, max_end)
        if start < last_end:
            start = last_end + 0.01
            end = max(end, start + 0.05)
        text = clean_subtitle_text(seg.text)
        if text:
            out.append(Segment(len(out) + 1, start, end, text))
            last_end = end
    return out


def split_long_segments(
    segments: Iterable[Segment],
    *,
    max_duration: float = 8.0,
    max_chars: int = 120,
) -> list[Segment]:
    out: list[Segment] = []
    for seg in segments:
        desired = max(1, int((seg.duration + max_duration - 0.001) // max_duration))
        desired = max(desired, int((len(seg.text) + max_chars - 1) // max_chars))
        if desired <= 1:
            out.append(Segment(len(out) + 1, seg.start, seg.end, seg.text))
            continue
        chunks = split_text_chunks(seg.text, desired)
        chunk_duration = seg.duration / len(chunks)
        for i, chunk in enumerate(chunks):
            start = seg.start + i * chunk_duration
            end = seg.start + (i + 1) * chunk_duration
            out.append(Segment(len(out) + 1, start, end, chunk))
    return normalize_segments(out)


def split_text_chunks(text: str, desired: int) -> list[str]:
    text = clean_subtitle_text(text)
    sentence_parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+", text) if p.strip()]
    # Prefer semantic sentence boundaries over exact duration. Hard word splitting creates
    # fragments that translation models tend to complete or duplicate.
    if len(sentence_parts) > 1:
        return sentence_parts
    clause_parts = [p.strip() for p in re.split(r"(?<=[,;:，；：])\s+", text) if p.strip()]
    if len(clause_parts) > 1 and len(text) > 160:
        return balance_parts(clause_parts, min(desired, len(clause_parts)))
    words = text.split()
    if len(words) <= desired:
        return [w for w in words if w]
    per = max(1, (len(words) + desired - 1) // desired)
    chunks = [" ".join(words[i : i + per]) for i in range(0, len(words), per)]
    return [c for c in chunks if c]


def balance_parts(parts: list[str], desired: int) -> list[str]:
    chunks = ["" for _ in range(desired)]
    for idx, part in enumerate(parts):
        target = min(desired - 1, idx * desired // max(1, len(parts)))
        chunks[target] = f"{chunks[target]} {part}".strip()
    return [c for c in chunks if c]


def wrap_cjk(text: str, limit: int = 22) -> str:
    if len(text) <= limit:
        return text
    chunks: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if len(current) >= limit and ch in "，。；：、,.!?！？ ":
            chunks.append(current.strip())
            current = ""
    if current:
        chunks.append(current.strip())
    if len(chunks) <= 1:
        chunks = split_cjk_without_breaking_tokens(text, limit)
    return "\n".join(chunks)


def split_cjk_without_breaking_tokens(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    i = 0
    while i < len(text):
        end = min(len(text), i + limit)
        if end < len(text) and is_token_char(text[end - 1]) and is_token_char(text[end]):
            forward = end
            while forward < len(text) and is_token_char(text[forward]):
                forward += 1
            backward = end - 1
            while backward > i and is_token_char(text[backward - 1]):
                backward -= 1
            if backward > i and (end - backward) <= 8:
                end = backward
            else:
                end = forward
        chunks.append(text[i:end].strip())
        i = end
    return [c for c in chunks if c]


def is_token_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "._+-/%^")


def wrap_latin(text: str, limit: int = 52) -> str:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit and current:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def write_srt(path: str | Path, segments: Iterable[Segment], *, cjk: bool = False, line_limit: int = 22) -> None:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        text = wrap_cjk(seg.text, line_limit) if cjk else wrap_latin(seg.text)
        lines.extend([str(i), f"{format_srt_time(seg.start)} --> {format_srt_time(seg.end)}", text, ""])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_vtt(path: str | Path, segments: Iterable[Segment], *, cjk: bool = False, line_limit: int = 22) -> None:
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, start=1):
        text = wrap_cjk(seg.text, line_limit) if cjk else wrap_latin(seg.text)
        lines.extend([str(i), f"{format_vtt_time(seg.start)} --> {format_vtt_time(seg.end)}", text, ""])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def bilingual_segments(en: list[Segment], zh: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for i, (e, z) in enumerate(zip(en, zh), start=1):
        text = f"{z.text}\n{e.text}"
        out.append(Segment(i, e.start, e.end, text))
    return out


def write_bilingual_ass(path: str | Path, en: list[Segment], zh: list[Segment]) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZhMain, Microsoft YaHei UI, 42, &H00FFFFFF, &H000000FF, &H00000000, &H99000000, 1, 0, 0, 0, 100, 100, 0, 0, 1, 4, 0.5, 2, 110, 110, 178, 1
Style: EnSub, Arial, 24, &H00F0F0F0, &H000000FF, &H00000000, &H99000000, 0, 0, 0, 0, 100, 100, 0, 0, 1, 3, 0.3, 2, 130, 130, 72, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for e, z in zip(en, zh):
        start = ass_time(e.start)
        end = ass_time(e.end)
        zh_text = ass_escape(wrap_cjk(z.text, 18)).replace("\n", r"\N")
        en_text = ass_escape(wrap_latin(e.text, 60)).replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{start},{end},ZhMain,,0,0,0,,{zh_text}")
        lines.append(f"Dialogue: 1,{start},{end},EnSub,,0,0,0,,{en_text}")
    Path(path).write_text("\n".join(lines), encoding="utf-8-sig")


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    cs = int(round((seconds - int(seconds)) * 100))
    total = int(seconds)
    if cs == 100:
        total += 1
        cs = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", r"\{").replace("}", r"\}")


def to_dicts(segments: Iterable[Segment]) -> list[dict]:
    return [asdict(seg) for seg in segments]
