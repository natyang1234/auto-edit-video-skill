# Changelog

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
