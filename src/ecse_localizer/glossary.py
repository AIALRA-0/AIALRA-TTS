from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .subtitle_io import Segment
from .utils import write_json


@dataclass
class GlossaryTerm:
    source_term: str
    zh_term: str
    type: str
    confidence: float
    first_seen_video: str
    notes: str = ""


TECH_TERMS: dict[str, tuple[str, str]] = {
    "semiconductor": ("半导体", "concept"),
    "workforce development": ("人才培养", "concept"),
    "syllabus": ("教学大纲", "concept"),
    "lithography": ("光刻", "process"),
    "photoresist": ("光刻胶", "material"),
    "coater developer track": ("涂胶显影轨道", "equipment"),
    "photomask": ("光掩模", "equipment"),
    "overlay": ("套刻", "metric"),
    "plasma etching": ("等离子体刻蚀", "process"),
    "wet etch": ("湿法刻蚀", "process"),
    "ion implantation": ("离子注入", "process"),
    "doping": ("掺杂", "process"),
    "metallization": ("金属化", "process"),
    "chemical mechanical polishing": ("化学机械抛光", "process"),
    "CMP": ("化学机械抛光 CMP", "acronym"),
    "epitaxy": ("外延", "process"),
    "advanced packaging": ("先进封装", "concept"),
    "hybrid bonding": ("混合键合", "process"),
    "yield learning": ("良率学习", "concept"),
    "failure analysis": ("失效分析", "process"),
    "statistical process control": ("统计过程控制", "concept"),
    "SPC": ("统计过程控制 SPC", "acronym"),
    "SPC chart": ("SPC 图表", "concept"),
    "SPC charts": ("SPC 图表", "concept"),
    "Shewhart": ("Shewhart", "name"),
    "Walter A. Shewhart": ("Walter A. Shewhart", "name"),
    "W. Edwards Deming": ("W. Edwards Deming", "name"),
    "Deming": ("Deming", "name"),
    "machine learning": ("机器学习", "concept"),
    "AMHS": ("自动物料搬运系统 AMHS", "acronym"),
    "cleanroom": ("洁净室", "facility"),
    "facility systems": ("厂务系统", "facility"),
    "EUV": ("极紫外光刻 EUV", "acronym"),
    "ALD": ("原子层沉积 ALD", "acronym"),
    "OPC": ("光学邻近校正 OPC", "acronym"),
    "CD-SEM": ("临界尺寸扫描电镜 CD-SEM", "acronym"),
    "OCD": ("光学临界尺寸量测 OCD", "acronym"),
    "RPI": ("RPI", "name"),
}


def extract_glossary(
    segments: Iterable[Segment],
    first_seen_video: str,
    existing: dict[str, GlossaryTerm] | None = None,
) -> dict[str, GlossaryTerm]:
    terms = dict(existing or {})
    corpus = " ".join(s.text for s in segments)
    lower = corpus.lower()
    for source, (zh, typ) in TECH_TERMS.items():
        if source.lower() in lower and source not in terms:
            terms[source] = GlossaryTerm(source, zh, typ, 0.95, first_seen_video, "seeded technical term")

    for acronym in sorted(set(re.findall(r"\b[A-Z][A-Z0-9-]{2,}\b", corpus))):
        if acronym in TECH_TERMS and acronym not in terms and len(acronym) <= 12:
            terms[acronym] = GlossaryTerm(acronym, acronym, "acronym", 0.75, first_seen_video, "auto-detected acronym")
    return terms


def extract_from_title(title: str, first_seen_video: str, existing: dict[str, GlossaryTerm] | None = None) -> dict[str, GlossaryTerm]:
    fake = [Segment(1, 0, 1, title)]
    return extract_glossary(fake, first_seen_video, existing)


def write_glossary_tsv(path: str | Path, terms: dict[str, GlossaryTerm]) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["source_term", "zh_term", "type", "confidence", "first_seen_video", "notes"],
            delimiter="\t",
        )
        writer.writeheader()
        for term in sorted(terms.values(), key=lambda t: (-t.confidence, t.source_term.lower())):
            writer.writerow(asdict(term))


def write_glossary_json(path: str | Path, terms: dict[str, GlossaryTerm]) -> None:
    write_json(path, [asdict(t) for t in sorted(terms.values(), key=lambda t: (-t.confidence, t.source_term.lower()))])
