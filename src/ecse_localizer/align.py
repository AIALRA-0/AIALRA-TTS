from __future__ import annotations

from .subtitle_io import Segment


def overlap_count(segments: list[Segment]) -> int:
    return sum(1 for a, b in zip(segments, segments[1:]) if b.start < a.end)


def max_timeline_end(segments: list[Segment]) -> float:
    return max((s.end for s in segments), default=0.0)
