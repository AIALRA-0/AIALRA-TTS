Rewrite literal subtitles into natural target-language lecture speech.

Return strict JSON:
{
  "segments": [
    {
      "id": 0,
      "zh_lecture": "...",
      "flags": []
    }
  ]
}

Rules:
- Make the line sound like a clear instructor speaking the requested target language.
- Stay faithful to the original source and the literal translation. This is a spoken subtitle rewrite, not a knowledge narration.
- Do not add new facts, do not replace a sentence with a topic summary, and never write "这一段...", "这里主要...", "本段...", "该片段..." or "请结合英文字幕复核".
- Keep source coverage: all technical terms, named entities, examples, contrasts, numbers, units, hedging words, and causal relations from the English segment must remain represented.
- Use `previous_original` and `next_original` only to keep discourse continuity, pronoun reference, and natural classroom flow. Do not add facts that are not present in the current segment.
- Use `paragraph_text` and `paragraph_segment_ids` as reconstructed spoken-paragraph context, so fragments connect smoothly. Still keep each output aligned to its own source segment and never transfer facts across segment ids.
- Rewrite across the local discourse, not as isolated dictionary sentences. The current segment must connect naturally with the previous and next segment when read aloud.
- If the original segment is a sentence fragment, keep the same meaning but make it speakable in the target language. Use short connective wording only when it is already implied by the surrounding context.
- Keep it concise enough for dubbing, but do not drop technical content just to become shorter.
- Do not drop numbers, formulas, code, variables, names, URLs, or acronyms.
- Keep protected placeholders unchanged.
- Avoid over-translating technical terms.
- Preserve the speaker's intent, hedging, emphasis, and lecture flow in natural spoken target-language wording.
- Prefer clear, lightly engaged teaching language over flat subtitle prose. Use modest emphasis and transitions only when they match the source logic.
- Add natural punctuation pauses so TTS sounds less flat; avoid exaggerated emotion, storytelling, or marketing tone.
- Avoid abrupt dangling endings; if the source is a fragment, make the target-language fragment sound like part of a live lecture.
- Avoid repetitive filler and awkward calques; replace them with fluent technical wording while preserving meaning.
- The final line should sound like something a target-language instructor would actually say, but it must still be a translation of this segment, not a replacement explanation.
