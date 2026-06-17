Apply a light dialect/accent flavor to Chinese lecture subtitles.

Return strict JSON:
{
  "segments": [
    {
      "id": 0,
      "zh_dialect": "...",
      "flags": []
    }
  ]
}

Rules:
- Dialect intensity is light.
- Do not alter standard technical terms.
- Preserve formulas, code, variables, file names, URLs, names, and acronyms.
- If dialect would reduce clarity, keep standard Mandarin.
