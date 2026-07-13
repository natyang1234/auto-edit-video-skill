# Editing rules

## Table of contents

- Candidate classes
- Deletion rules
- Risk tiers
- Timing and re-transcription

## Candidate classes

Analyze each class independently so one heuristic does not hide another:

- `silence`: internal pause above the preset threshold; preserve intentional
  rhetorical breaths.
- `filler`: standalone hesitation with no semantic payload.
- `stutter`: immediate accidental syllable/word restart.
- `repetition`: earlier incomplete/duplicated clause followed by a complete one.
- `false_start`: abandoned sentence or technical-term retry.

Do not classify discourse markers such as「所以」「但是」「其實」as filler just
because they are frequent.

## Deletion rules

1. Delete the earlier failed attempt; retain the later complete attempt.
2. Preserve unique names, figures, qualifications, negations, and examples.
3. Include internal gaps when deleting an entire abandoned range.
4. Add short handles around cuts only when required to protect consonant/word
   tails; never cut exactly through a phoneme.
5. Compare video duration with the last transcript word. Inspect untranscribed
   heads/tails for room noise, gestures, and intentional holds.
6. Keep a source-to-final `cut_map.json` even though final subtitles are created
   by re-transcription.

## Risk tiers

Low risk, still visible on the review page:

- clear silence;
- isolated hesitation;
- short literal stutter where the kept phrase fully contains the deleted text.

High risk, require adversarial review:

- full-sentence deletion;
- long repetition;
- two similar sentences with different nouns, figures, negation, or qualifiers;
- technical-term retries;
- any edit that changes a claim or narrative order.

When uncertain, default to keep.

## Timing and re-transcription

- Perform destructive cuts on frame-accurate time ranges and match source video
  parameters.
- After the cut, re-extract audio and transcribe the rendered cut.
- Correct words while preserving the new timestamps.
- Treat ASR output as a draft; never add speech that is absent from the audio.
