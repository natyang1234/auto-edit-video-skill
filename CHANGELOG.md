# Changelog

## 1.8.0 - 2026-07-15

### Approval-bound batch highlight export

- Add a server-owned final-render queue for every highlight explicitly marked
  approved. The browser cannot inject or omit clip IDs, and pending/rejected
  proposals never enter the batch.
- Reuse the frozen per-clip renderer, mechanical QA, contact sheet, and
  SHA-256 receipt for every item. A failed item preserves the previous good
  delivery contract and prevents the partial batch from becoming approvable.
- Add a schema-v2 aggregate delivery receipt whose clip set and every MP4, QA
  report, contact sheet, render receipt, and ZIP archive are hash-verified by
  the existing final gate.
- Add GUI batch progress, per-clip QA links, and approval-gated ZIP/individual
  downloads while preserving the existing single-clip preview/final workflow.

## 1.7.0 - 2026-07-14

### Whole-transcript contextual semantic calibration

- Add a loopback-only Ollama pass that reviews every readable caption with the
  numbered whole-document transcript plus bounded previous/next context, then
  sends each proposed ASR patch through a separate verification pass. Coverage
  is derived from the transcript rather than trusted from model output.
- Apply only exact, time-scoped, high-confidence minimal patches. Deterministic
  guards reject sentence rewrites, unchanged context wrapped around a change,
  punctuation or number changes, unlisted English changes, overlapping edits,
  cross-caption duplicate characters, and verifier rejections; general
  word-choice edits remain pending.
- Preserve exact glossary words and full phrases before fuzzy matching so a
  valid `cigar` example cannot be overwritten by the similar `cigarette`
  example; safely scope zero-duration Whisper words with a bounded epsilon.
- Write `working/transcript_semantic_review.json` with complete/partial
  coverage, accepted/pending/rejected proposals, active rules, model errors,
  and actual applied correction counts. Re-import canonical SRT and GUI
  captions from immutable Whisper output and invalidate all transcript-bound
  approvals after accepted corrections.
- Enable the local whole-context pass by default in new Studio imports, stop
  planning when coverage is partial, and show checked/total, corrected, and
  pending counts in the editor instead of treating zero mechanical warnings as
  semantic completion.
- Use a 16K local context window for the numbered document, cap generated JSON
  to 1,536 tokens per call, persist per-batch progress, and expose live
  checked/total progress through the Studio pipeline endpoint.

## 1.6.2 - 2026-07-14

### Taiwan Traditional subtitle normalization

- Normalize `zh-TW`, `zh-en`, and auto-detected Chinese transcript truth with
  the bundled OpenCC Taiwan phrase dictionaries before writing SRT, GUI
  captions, emphasis, cards, or highlights. An explicit `zh-CN` source remains
  the supported opt-out.
- Convert complete timed Whisper word streams before projecting text back onto
  their original boundaries, so split phrases keep context (`联` + `系` becomes
  Taiwan `聯絡`) while English spelling and timestamps remain unchanged.
- Record the orthography backend and conversion counts in
  `working/transcript_orthography.json`, and ship the Apache-2.0 dictionary
  subset plus third-party notices inside the portable skill.

## 1.6.1 - 2026-07-14

### Variable-length semantic calibration

- Allow audited calibration rules whose canonical phrase and ASR alias have
  different character lengths, including Chinese/English code switches such as
  `It is=意思` and phrase repairs such as `例句=音譽句`.
- Preserve the full matched Whisper source interval, reuse existing word
  boundaries when possible, and label each audit entry as either
  `word_boundaries_preserved` or `source_span_preserved`.
- Treat a low-confidence multiword English correction as glossary-known when
  each component word is declared, avoiding a false warning for `It is`.
- Add public CLI regression coverage for `It is`, `不定詞片語`, and `例句`, and
  update Studio guidance so these corrections happen before SRT, highlights,
  cards, and GUI captions are generated.

## 1.6.0 - 2026-07-14

### Auditable semantic subtitle calibration

- Add explicit `canonical=alias|alias` transcription calibration rules for
  Chinese homophones and exact English misspellings. Equal-length replacements
  preserve every imported Whisper word timestamp, and optional time scopes
  safely handle ambiguous aliases only inside a reviewed source interval.
- Write `working/transcript_calibration.json` with every applied source span,
  replacement, timestamp, and rule; propagate corrected copy into canonical
  SRT, GUI captions, highlight titles, and designed cards.
- Separate mechanical glossary warnings from semantic calibration state.
  Chinese transcripts without rules now report `semantic_review_required`;
  applied rules report `applied_needs_review` and never imply human approval.
- Add the calibration field to loopback Studio import, regression coverage for
  homophones, English spelling, exact timestamp preservation, time scoping,
  and semantic-status reporting. The full suite now contains 89 tests.

## 1.5.1 - 2026-07-14

### Mixed-language accuracy regression

- Bound Whisper's mixed-language initial prompt to 180 characters, keep at
  most 12 short glossary terms, and exclude long example sentences that can
  over-bias the `base` model and degrade adjacent Chinese homophones.
- Repair the common `It`/`Ed` code-switch confusion only when `It` is in the
  project glossary and the token is low-confidence or locally tied to `to V`
  or `虛主詞`; preserve high-confidence person names outside that context.
- Merge a split first English word inside a multiword glossary phrase, such as
  `St` + `ay away from the smoke`, without leaving a duplicate fragment.
- Add differential and safety regression coverage for bounded prompts,
  Chinese preservation, contextual aliases, real names, and split phrases;
  the full local suite now contains 87 passing tests.

## 1.5.0 - 2026-07-14

### Mixed Chinese/English transcription

- Add `zh-en` Studio/CLI source mode and an optional project terminology
  glossary. Chinese and auto-detected transcripts now prompt local Whisper to
  retain English spelling instead of producing Chinese phonetic text.
- Add conservative glossary correction for close English spellings and split
  subword tokens, canonical readable SRT generation, and a transcript-review
  artifact for missing terminology or unknown low-confidence Latin tokens.
- Split long Whisper segments into readable timed GUI captions while retaining
  original word timing and transcript evidence.

### Review integrity and tests

- Preserve English spaces and word boundaries in transcript-grounded highlight
  titles, including safe truncation that never cuts an English word in half.
- Invalidate all four review gates when the source transcript changes; prior
  versioned MP4s remain untouched.
- Expand the full local GUI, security, renderer, transcription, and browser
  regression suite to 83 passing tests.

## 1.4.0 - 2026-07-14

### Multi-template video composition

- Separate director typography/card strategy from video framing and add three
  fixed-camera, two dynamic-camera, and three subject-cutout templates.
- Guarantee that fixed templates emit no source zoom/reframe tween while the
  dynamic templates retain explicit craft-reframe or controlled-punch motion.
- Add GUI controls for template groups, source frame X/Y/width/height, cutout
  subject X/Y/scale, solid color, and project-owned image/video backgrounds.

### Local subject cutout and safety

- Add a local rembg/ISNet compositor with solid, image, and looping-video
  backgrounds; preserve original audio through the final timeline renderer.
- Hash template background assets into editor revisions, reject symlinks and
  paths outside `assets/`, and fail closed when the local engine, model, or
  required background is missing.
- Add fixed/dynamic HTML contracts, template state migration, browser
  round-trip coverage, compositor tests, and real fixed/cutout MP4 smoke tests.

## 1.3.0 - 2026-07-14

### Caption and layout editing

- Add exact character-range caption effects with editable pop, highlight, and
  underline styles, color, and scale; derive sparse transcript-grounded terms
  from reviewed highlight/card copy when no manual span exists.
- Add drag-to-position caption/card layers, caption/card width and card-height
  controls, platform safe-zone checks, and timed-layer collision warnings.
- Persist card layout in the render package instead of using role-only hardcoded
  coordinates.

### Render parity and validation

- Bake designed captions and inline effect spans into the same HTML/GSAP visual
  package used by the final MP4; prevent the FFmpeg path from drawing a second,
  inconsistent caption layer.
- Let full-screen hook cards replace their overlapping caption interval and
  reserve a lower subtitle band under the recap card.
- Add browser round-trip coverage for inline effects and card layout, strict
  state validation for span offsets/styles, and graphic-package regression
  tests against preview/final divergence.

## 1.2.0 - 2026-07-14

### Features

- Add a loopback-only Studio that imports one local browser File into an
  immutable owned project copy and runs local transcription/planning with
  visible status.
- Add five deterministic semantic director profiles and a validated
  `highlight_plan.json` with up to ten transcript-grounded, scored, reviewable
  source ranges.
- Add per-highlight keep/reject, title/range editing, clip-scoped captions and
  timeline layers, versioned per-clip preview/final MP4s, and reviewed download.
- Normalize audible render audio toward -16 LUFS while safely bypassing silent
  or missing audio tracks.

### Safety

- Add revision-bound ordered `destructive_edit`, `highlight_selection`,
  `timeline`, and `final` gates with stale-write rejection and downstream
  invalidation.
- Freeze source, asset, clip, state, and approval hashes in render snapshots;
  write atomic output/render receipts and bind final approval to output, QA
  report, and contact-sheet SHA-256 values.
- Fail closed when page-editor final render would otherwise ignore reviewed
  interior delete decisions; require the destructive cut renderer first.
- Add CSRF/Host/Origin checks, project-scoped paths, media limits, container and
  ffprobe validation, atomic project creation, and tamper-aware approval status.

### Documentation and tests

- Document Studio launch, highlight artifacts, editor review order, QA-bound
  final approval, current limitations, and the unchanged Agent-first default.
- Expand the regression suite to 46 tests and complete a real 492.874-second
  local-video import → Whisper → 10 highlights → review → preview/final → QA
  validation without changing the source file.

## 1.1.0 - 2026-07-14

### Features

- Add an agent-first workflow where the user supplies one local video and the agent owns transcription, conservative edit decisions, MP4 export, and QA without requiring a UI.
- Add selectable platform-aware `short`, `medium`, `long`, `auto`, `full`, and custom-second targets, persisted in `project.json` through `init` and `set-target`.
- Add a machine-readable `duration-presets` command for Instagram Reels, YouTube Shorts, TikTok, Xiaohongshu, generic vertical, and YouTube landscape output.
- Expose local Whisper and whisper.cpp command availability in preflight capabilities.

### Documentation

- Add the Traditional Chinese phase-one contract for one-video input, platform-aware duration selection, official-source distinctions, safety rules, completion criteria, and explicit non-goals.
- Clarify that UI, LINE delivery, uploads, and social publishing are outside the current phase.

### Tests

- Make optional Rumi integration tests deterministic in clean CI environments and cover local-transcription capability reporting, duration matrices, manifest persistence, automatic resolution, and custom-second overrides.

## 1.0.0 - 2026-07-13

### Features

- Add a reviewed, non-destructive automatic video-editing workflow with three approval gates.
- Bundle a local page editor, deterministic FFmpeg timeline renderer, approved-cut renderer, and delivery QA/contact-sheet tool.
- Add portable skill discovery and a universal installer for Codex, Claude Code, Grok, OpenClaw, Hermes, and Agent Skills compatible runtimes.

### Security

- Keep the editor loopback-only by default and require explicit consent for cloud TTS.
- Ship no credentials, media, private creator profiles, or machine-specific paths.
