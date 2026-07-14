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
│   ├── transcript_calibration.json # exact homophone/spelling replacements
│   ├── transcript_review.json      # mechanical issues + semantic status
│   ├── edit_candidates.json        # machine proposals
│   ├── edit_decisions.json         # approved keep/delete decisions
│   ├── pipeline_status.json        # local Studio processing state
│   ├── highlight_plan.json         # 1–10 transcript-grounded proposals
│   ├── editor_state.json           # reviewed highlights and renderable layers
│   ├── cut_map.json                # source time → final time mapping
│   ├── transcript_final.json       # re-transcribed cut
│   ├── emphasis_plan.json          # timed keyword treatments
│   ├── visual_plan.json            # timed cards/assets/animations
│   ├── render_snapshots/           # frozen render inputs/authorization
│   ├── render_receipts/            # output SHA/bytes/snapshot identity
│   ├── delivery_qa/                # per-render pass/fail receipts
│   └── latest_final_qa.json        # current successful final QA contract
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
│   ├── <clip>-<version>-preview.mp4
│   └── <clip>-<version>-final.mp4
└── qa/
    ├── <render-id>-contact.png
    └── <render-id>-qa-report.json
```

Directories may be empty until their stage starts. Do not create alternate names
for the same truth without recording them in `project.json.artifacts`.

## Truth hierarchy

1. Current final output bytes plus matching render/delivery-QA receipts.
2. Actual final audio and re-transcribed word timings when destructive cuts run.
3. Approved edit decisions, reviewed highlight ranges, and final cut map.
4. Corrected transcript/subtitles.
5. Visual/emphasis plans.
6. Original script or marketing brief.

Never let a draft script overwrite something actually spoken.

## Artifact schemas

### `transcript_calibration.json`

Calibration never implies human approval. Rules use equal-length aliases so
the imported word timing remains unchanged; optional `start`/`end` values
scope an otherwise ambiguous alias to a reviewed source interval.

```json
{
  "schema_version": 1,
  "status": "not_configured|applied_needs_review",
  "rule_count": 1,
  "correction_count": 1,
  "human_review_required": true,
  "rules": [
    {"canonical": "雪茄", "aliases": ["學家", "雪家"]}
  ],
  "corrections": [
    {"segment_id": 10, "from": "學家", "to": "雪茄", "start": 195.44, "end": 196.06}
  ]
}
```

`transcript_review.json.issue_count` is the mechanical issue count. Read
`semantic_calibration.status` and `risk_status` separately; zero mechanical
issues must not be described as semantically clear.

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

### `highlight_plan.json`

The deterministic local planner writes at most ten items. Titles and evidence
must be transcript extracts; strategy scores never replace source evidence.

```json
{
  "schema_version": 1,
  "status": "needs_review",
  "source": {
    "artifact": "working/transcript_words.json",
    "sha256": "<transcript-sha256>",
    "source_sha256": "<video-sha256>",
    "timebase": "source_media",
    "duration_s": 492.874
  },
  "configuration": {
    "director_profile": "teacher-punch|high-energy|news-drama|pov-observer|minimal-editor",
    "requested_count": 10,
    "duration_profile": "short|medium|long|auto|full|custom",
    "editing_brief": "user-provided local brief"
  },
  "items": [
    {
      "id": "highlight-abcdef123456",
      "start": 4.271,
      "end": 33.267,
      "duration_s": 28.996,
      "title": "exact transcript-derived title",
      "evidence": {
        "text": "exact transcript extract",
        "segment_ids": ["segment-0002"],
        "word_ids": ["word-0008"],
        "exact_transcript_extract": true
      },
      "score": 0.81,
      "score_components": {},
      "selection_tags": ["hook", "completeness"],
      "risk_flags": [],
      "review_status": "pending|approved|rejected"
    }
  ],
  "plan_revision": "<sha256>"
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

### `latest_final_qa.json`

This is the current delivery contract used by the `final` gate. Every referenced
artifact must remain inside its project scope and match its declared SHA-256.

```json
{
  "schema_version": 1,
  "render_id": "render_<uuid>",
  "quality": "final",
  "clip_id": "highlight-abcdef123456",
  "state_revision": "<editor-state-sha256>",
  "status": "pass",
  "output": "renders/<clip>-<version>-final.mp4",
  "output_sha256": "<sha256>",
  "report": "qa/<render-id>-qa-report.json",
  "report_sha256": "<sha256>",
  "contact_sheet": "qa/<render-id>-contact.png",
  "contact_sheet_sha256": "<sha256>",
  "render_receipt": "working/render_receipts/<render-id>.json",
  "render_receipt_sha256": "<sha256>",
  "human_review_required": true
}
```

## State and approvals

Pipeline stages use `pending`, `in_progress`, `needs_review`, `complete`,
`failed`, or `skipped`. Only the `approve` command or a clearly recorded user
decision may set these gates:

- `destructive_edit`
- `highlight_selection`
- `timeline`
- `final`

An approval records UTC time, confirmer, note, and the exact expected revision.
The four gates are ordered and stale revisions are rejected. Highlight/editor
changes invalidate downstream gates; a new final render invalidates the previous
final approval. Approval does not perform the cut or render by itself.
