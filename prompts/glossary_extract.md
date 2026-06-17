You are extracting a course glossary from English engineering lecture subtitles.

Return strict JSON:
{
  "terms": [
    {
      "source_term": "...",
      "zh_term": "...",
      "type": "concept|process|equipment|material|metric|acronym|person|paper|code",
      "confidence": 0.0,
      "notes": "..."
    }
  ]
}

Rules:
- Preserve formulas, code, variables, file names, URLs, person names, and acronyms.
- Prefer standard Simplified Chinese technical terms.
- Do not invent unsupported terms.
