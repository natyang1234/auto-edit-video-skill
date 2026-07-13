# Changelog

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
