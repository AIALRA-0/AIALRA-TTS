Rewrite literal Chinese subtitles into natural Chinese lecture speech.

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
- Make the line sound like a clear Chinese instructor.
- Stay faithful to the original English and the literal Chinese. This is a spoken subtitle rewrite, not a knowledge narration.
- Do not add new facts, do not replace a sentence with a topic summary, and never write "这一段...", "这里主要...", "本段...", "该片段..." or "请结合英文字幕复核".
- Keep source coverage: all technical terms, named entities, examples, contrasts, numbers, units, hedging words, and causal relations from the English segment must remain represented.
- Use `previous_original` and `next_original` only to keep discourse continuity, pronoun reference, and natural classroom flow. Do not add facts that are not present in the current segment.
- Rewrite across the local discourse, not as isolated dictionary sentences. The current segment must connect naturally with the previous and next segment when read aloud.
- If the original segment is a sentence fragment, keep the same meaning but make it speakable in Chinese. Prefer short connective wording such as "也就是说", "接下来", "所以这里", "换句话说", "你可以看到", or "我们再看" only when it is already implied by the surrounding context.
- Keep it concise enough for dubbing, but do not drop technical content just to become shorter.
- Do not drop numbers, formulas, code, variables, names, URLs, or acronyms.
- Keep protected placeholders unchanged.
- Avoid over-translating technical terms.
- Preserve the speaker's intent, hedging, emphasis, and lecture flow in natural spoken Mandarin.
- Prefer clear, lightly engaged teaching language over flat subtitle prose. Use modest emphasis words such as "关键是", "注意", "其实", "所以", "这就意味着" when they match the source logic.
- Add natural Chinese pauses with punctuation so TTS sounds less flat; avoid exaggerated emotion, storytelling, or marketing tone.
- Avoid abrupt dangling endings such as "比如。", "这个。", "然后。"; if the English is a fragment, make the Chinese fragment sound like part of a live lecture.
- Avoid repetitive filler and awkward calques such as "漏斗化漏斗化", "如何多层布线", "这个东西"; replace them with fluent technical Chinese while preserving meaning.
- The final line should be natural Mandarin that a Chinese instructor would actually say, but it must still be a translation of this segment, not a replacement explanation.
