Check translated lecture subtitles for consistency.

Return strict JSON:
{
  "issues": [
    {
      "segment_id": 0,
      "type": "term|number|formula|variable|code|name|omission|untranslated",
      "severity": "low|medium|high",
      "message": "..."
    }
  ]
}

Rules:
- Verify glossary terms are used consistently.
- Verify numbers, formulas, variables, paths, URLs, and acronyms are preserved.
- Flag likely untranslated or empty Chinese.
