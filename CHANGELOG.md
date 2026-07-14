# Changelog

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
