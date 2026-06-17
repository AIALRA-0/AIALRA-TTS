from ecse_localizer.glossary import extract_glossary
from ecse_localizer.subtitle_io import Segment


def test_extract_seed_terms():
    segments = [
        Segment(1, 0, 1, "Lithography uses photoresist and OPC for semiconductor manufacturing."),
        Segment(2, 1, 2, "CMP and CD-SEM appear later."),
    ]
    terms = extract_glossary(segments, "video.mp4")
    assert terms["lithography"].zh_term == "光刻"
    assert terms["photoresist"].zh_term == "光刻胶"
    assert "CMP" in terms
    assert "CD-SEM" in terms
