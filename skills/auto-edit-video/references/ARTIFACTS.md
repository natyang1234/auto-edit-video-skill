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
│   ├── transcript_semantic_review.json # whole-context coverage and safe patches
│   ├── transcript_orthography.json # Taiwan Traditional conversion audit
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
│   ├── delivery_qa/                # per-render and aggregate batch receipts
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
│   ├── <clip>-<version>-final.mp4
│   └── <batch>-final.zip
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

Calibration never implies human approval. Canonical text and aliases may have
different character lengths. Equal-length replacements retain each imported
word boundary; variable-length replacements retain the complete matched source
interval and reuse Whisper boundaries where possible. Optional `start`/`end`
values scope an otherwise ambiguous alias to a reviewed source interval.

```json
{
  "schema_version": 1,
  "status": "not_configured|applied_needs_review",
  "rule_count": 1,
  "correction_count": 1,
  "human_review_required": true,
  "rules": [
    {"canonical": "例句", "aliases": ["音譽句"]}
  ],
  "corrections": [
    {
      "segment_id": 10,
      "from": "音譽句",
      "to": "例句",
      "start": 32.56,
      "end": 32.96,
      "source_word_count": 3,
      "timing_mode": "source_span_preserved"
    }
  ]
}
```

`transcript_review.json.issue_count` is the mechanical issue count. Read
`semantic_calibration.status` and `risk_status` separately; zero mechanical
issues must not be described as semantically clear.

### `transcript_semantic_review.json`

The contextual pass reviews every caption with the numbered whole-document
transcript plus bounded previous/next context, then separately verifies every
proposed patch. `coverage_status=complete` means
every current caption unit was checked; it does not mean the transcript is
human-approved. Only accepted minimal patches become time-scoped calibration
rules. Pending wording stays visible for human confirmation, and rejected
proposals are never applied.

```json
{
  "schema_version": 1,
  "status": "complete_needs_review|partial_needs_review",
  "coverage_status": "complete|partial",
  "provider": "ollama",
  "model": "qwen2.5:7b",
  "document_context_unit_count": 116,
  "reviewed_unit_count": 116,
  "total_unit_count": 116,
  "accepted_count": 3,
  "pending_count": 4,
  "rejected_count": 7,
  "applied_correction_count": 3,
  "accepted": [],
  "pending": [],
  "rejected": [],
  "rules": [],
  "model_errors": [],
  "human_review_required": true
}
```

### `transcript_orthography.json`

This artifact records deterministic orthography normalization separately from
semantic calibration. For `zh-TW`, `zh-en`, and auto-detected Chinese, the
vendored OpenCC `s2twp` dictionaries run after declared-rule/glossary correction
but before contextual review and any downstream subtitle or visual artifact.
`changed_strings` is an audit count, not a quality score. Explicit `zh-CN`
records `not_requested` and preserves the source orthography.

```json
{
  "schema_version": 1,
  "status": "applied|not_requested",
  "variant": "zh-TW",
  "configuration": "s2twp",
  "backend": "vendored-opencc-python-reimplemented-0.1.7",
  "changed_strings": 42,
  "changed_characters": 97,
  "source_language_mode": "zh-en"
}
```

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

This is the current single- or batch-delivery contract used by the `final`
gate. Every referenced artifact must remain inside its project scope and match
its declared SHA-256. Schema version 1 represents one clip. Schema version 2
uses both `kind: "batch"` and `delivery_kind: "batch"`, requires the exact
current set of approved highlight IDs, stores one complete schema-v1-style QA
item per clip, and adds a hash-bound ZIP archive. A partial or failed batch
never replaces the prior successful file.

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
