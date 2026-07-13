# Artifact contract

## Table of contents

- Project tree
- Truth hierarchy
- Artifact schemas
- State and approvals

## Project tree

```text
PROJECT/
├── project.json                    # single source of truth
├── video-profile-context.md        # one isolated creator/brand context
├── source/                         # immutable source links/copies
├── working/
│   ├── transcript_words.json       # original word timings
│   ├── edit_candidates.json        # machine proposals
│   ├── edit_decisions.json         # approved keep/delete decisions
│   ├── cut_map.json                # source time → final time mapping
│   ├── transcript_final.json       # re-transcribed cut
│   ├── emphasis_plan.json          # timed keyword treatments
│   └── visual_plan.json            # timed cards/assets/animations
├── review/
│   ├── edit-review.html
│   └── timeline-preview.html
├── subtitles/
│   ├── source.srt
│   ├── zh-Hant.srt
│   ├── en.srt
│   └── bilingual.ass
├── voice/
│   ├── narration.txt               # exact text sent to TTS
│   ├── audio_request.json
│   ├── audio_meta.json
│   ├── narration.mp3|wav
│   └── narration.vtt|words.json
├── assets/                         # provenance-bearing licensed assets
├── renders/
│   ├── source_cut.mp4
│   ├── preview.mp4
│   └── final.mp4
└── qa/
    ├── final-contact.png
    └── qa-report.json
```

Directories may be empty until their stage starts. Do not create alternate names
for the same truth without recording them in `project.json.artifacts`.

## Truth hierarchy

1. Actual final audio and re-transcribed word timings.
2. Approved edit decisions and final cut map.
3. Corrected transcript/subtitles.
4. Visual/emphasis plans.
5. Original script or marketing brief.

Never let a draft script overwrite something actually spoken.

## Artifact schemas

### `edit_candidates.json`

```json
{
  "schema_version": 1,
  "source": "working/transcript_words.json",
  "items": [
    {
      "id": "edit-001",
      "type": "silence|filler|stutter|repetition|false_start",
      "start": 1.2,
      "end": 1.8,
      "text": "呃",
      "risk": "low|high",
      "reason": "standalone filler",
      "default_action": "delete|keep",
      "review_status": "pending|approved|rejected"
    }
  ]
}
```

### `emphasis_plan.json`

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "em-001",
      "start": 8.2,
      "end": 8.9,
      "text": "三倍",
      "reason": "numeric payoff",
      "treatment": "accent-pop",
      "scope": "word"
    }
  ]
}
```

### `visual_plan.json`

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "visual-001",
      "start": 12.0,
      "end": 16.0,
      "type": "asset|title_card|data_card|animation|broll",
      "purpose": "explain comparison",
      "transcript_evidence": "A 跟 B 的差別是…",
      "source": "assets/comparison.png",
      "provenance": "user-owned",
      "fallback": "HTML comparison card",
      "review_status": "pending"
    }
  ]
}
```

## State and approvals

Pipeline stages use `pending`, `in_progress`, `needs_review`, `complete`,
`failed`, or `skipped`. Only the `approve` command or a clearly recorded user
decision may set these gates:

- `destructive_edit`
- `timeline`
- `final`

An approval records UTC time, confirmer, and optional note. Approval does not
perform the cut or render by itself.
