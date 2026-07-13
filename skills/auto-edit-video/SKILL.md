---
name: auto-edit-video
description: >-
  Build and run a reviewed AI video-editing core for local source videos: remove
  silence, filler words, stutters, repetitions and false starts; re-transcribe
  the cut; create Chinese, English or bilingual subtitles; emphasize keywords;
  plan and place content-matched assets, title cards and animations; and
  optionally generate Chinese or English male/female voiceover with installed
  Edge, Rumi/Fish, HyperFrames, or other providers. Use for automatic talking-head editing,
  long-video-to-short preparation, subtitle-driven cuts, visual enrichment,
  bilingual captioning, selectable narration, or preparing the core project
  that the bundled local page editor can open for live preview, social-format
  adaptation, cover/copy drafting, approval, and deterministic export. Never
  uses CapCut.
---

# Auto Edit Video

Create a non-destructive, reviewable project before rendering. Use the bundled
FFmpeg cut, page-editor, timeline-render, and QA core. Discover optional installed
skills for premium captions, visual packages, creator profiles, or cloud voices;
their absence must not block the standalone core.

## Quick start

```bash
SKILL="/absolute/path/to/the/installed/auto-edit-video"

python3 "$SKILL/scripts/auto_edit.py" preflight

python3 "$SKILL/scripts/auto_edit.py" init \
  --input /absolute/path/source.mp4 \
  --project-dir /absolute/path/project \
  --source-language zh-TW \
  --subtitle-mode bilingual \
  --target-language en \
  --voice-language zh-TW \
  --voice-gender female \
  --voice-provider rumi

python3 "$SKILL/scripts/auto_edit.py" validate \
  --manifest /absolute/path/project/project.json

python3 "$SKILL/scripts/auto_edit.py" import-whisper \
  --manifest /absolute/path/project/project.json \
  --whisper-json /absolute/path/whisper.json \
  --srt /absolute/path/whisper.srt \
  --model base

python3 "$SKILL/scripts/auto_edit.py" analyze-edits \
  --manifest /absolute/path/project/project.json

python3 "$SKILL/scripts/auto_edit.py" plan-overlays \
  --manifest /absolute/path/project/project.json

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /absolute/path/project \
  --port 8765 --open
```

Omit every `--voice-*` flag to keep the original voice. Voiceover is opt-in.

## Required workflow

### 1. Preflight and creator context

Run `preflight`. Stop when `ready` is false or `missing_required` is non-empty.
`mode: standalone` is valid; `mode: extended` means every legacy production
bridge was also discovered. Missing optional integrations are capability notes,
not installation failures.

Materialize exactly one `video-profile-context.md` before analysis. If an
installed creator-profile bridge is available, use it. Otherwise write a short
local context containing only user-provided audience, brand, tone, and platform
facts; mark all unknown fields as unknown. Never infer blank profile markers.

Use named creator presets only when the user selected them or the current project
contains verified profile data.

### 2. Initialize the project

Run `init`, then treat `project.json` as the single source of truth. Keep the
original media immutable. Read [references/ARTIFACTS.md](references/ARTIFACTS.md)
before writing pipeline artifacts.

Defaults are deliberately conservative:

- balanced edit preset;
- destructive-edit review required;
- original audio retained;
- clean rail subtitles with sparse keyword emphasis;
- content-matched cards, assets, and animations enabled;
- CapCut disabled.

### 3. Transcribe and propose cuts

Transcribe locally with AutoClip/Whisper or HyperFrames Whisper. Always pin a
multilingual model for non-English audio; never use `small.en` on Chinese.

Apply [references/EDITING_RULES.md](references/EDITING_RULES.md). If
`chengfeng-cut-narration` is installed, its review UI may be used, but do not use
any temporary public upload path unless the user explicitly authorizes sending
audio to that service.

Write:

- `working/transcript_words.json`
- `working/edit_candidates.json`
- `working/edit_decisions.json`

Detect silence, filler, stutter, repetition, and false starts separately. Apply
the rule **delete the earlier failed attempt and retain the later complete one**.
Do not cut a word tail or unique claim.

`import-whisper` accepts a local Whisper JSON with word timestamps.
`analyze-edits` currently auto-proposes only the deterministic low-risk subset:
silence, standalone filler, and immediate short stutter. Longer repetition and
false-start decisions remain routed through the existing cut-review workflow.

### 4. Stop at the destructive-edit gate

Generate the existing review page and stop. The user may toggle every proposed
deletion. Do not call the cut endpoint or simulate the approval click.

After explicit approval, record it:

```bash
python3 "$SKILL/scripts/auto_edit.py" approve \
  --manifest PROJECT/project.json \
  --gate destructive_edit \
  --confirmed-by user
```

Only then render `source_cut.mp4`. Re-transcribe `source_cut.mp4`; never derive
final subtitle timings by subtracting deleted ranges from the original SRT.

The bundled renderer consumes only explicitly approved decisions:

```bash
python3 "$SKILL/scripts/render_cut.py" \
  --manifest PROJECT/project.json
```

### 5. Build subtitle and emphasis tracks

Use the manifest mode:

- `source`: corrected subtitles in the spoken language;
- `zh`: Traditional Chinese output;
- `en`: English output;
- `bilingual`: paired Chinese/English lines on one timing rail;
- `off`: no visible subtitle track.

Keep translations semantically faithful and preserve names, figures, units, and
claims. The audio/final transcript remains the timing truth.

Write `working/emphasis_plan.json`. Emphasize meaningful nouns, figures,
contrasts, promises, and conclusions. Default to at most one emphasized phrase
per caption line; do not animate every word.

Run `plan-overlays` for the conservative local baseline. It derives sparse
numeric/contrast emphasis, one opening title proposal, and a few transcript-
grounded data/contrast cards. Every item is `pending` and labeled as an
unverified transcript proposal. It does not fetch arbitrary GIFs or stock media.

Use the bundled page editor and FFmpeg renderer for ordinary rail captions.
Use `video-autopilot-macos` Path 1 only when discovered and selected. Use
`embedded-captions` only when installed and suitable for a single-subject shot
where the user wants premium occlusion/VFX captions; follow its identity gate.

### 6. Match visuals to meaning

Write `working/visual_plan.json` with a timed entry for each proposed asset,
title card, data card, illustration, animation, or B-roll insert. Every entry
must include transcript evidence, purpose, source/provenance, timing, and a
fallback. Never choose a merely similar-looking asset when it contradicts the
spoken content.

Create transcript-grounded text cards in the bundled editor. When installed,
route richer designed cards to `talking-head-recut`, finished narration footage
with screenshots/HTML explanations to `chengfeng-narration-to-video`, and
synthetic motion scenes to HyperFrames. Use exactly one primary renderer and
treat every other workflow as an asset/overlay producer.

Generate the timeline preview and stop at the `timeline` gate before final render.

### 6.1 Open the local page editor

Read [references/PAGE_EDITOR.md](references/PAGE_EDITOR.md), then launch `editor`.
The editor is loopback-only unless the operator explicitly passes
`--allow-remote`. It supports live video/timeline preview, font/color/size and
position controls, text motions, PNG/JPG/WEBP/GIF/MP4/MOV layers, platform safe
zones, local publishing-copy drafts, cover frames, preview render, and final
render.

Treat the HTML preview as an editing surface, then render an MP4 preview for
truth checking. A timeline approval is bound to the render-affecting state hash;
changing a caption, layer, canvas, or render style invalidates the old approval.

### 7. Add optional voiceover

Read [references/VOICE_AND_SUBTITLES.md](references/VOICE_AND_SUBTITLES.md).

- `rumi`: an optional installed Rumi voice system. Chinese female defaults to the
  JARVIS-matched Fish Audio `rumi` clone; Chinese male defaults to the selected
  Fish `溫暖磁性男聲旁白`. It reads the existing JARVIS configuration and never
  copies credentials into the project. Require cloud consent.
- `edge`: deterministic Chinese/English male/female presets and the free Chinese
  fallback. It is an online Microsoft service; require explicit cloud consent.
- `heygen`, `elevenlabs`, `kokoro`, or `auto`: require an installed
  `hyperframes-media/scripts/audio.mjs` engine; write an `audio_request.json`
  adapter and call that engine.
  Run `npx hyperframes auth status` first and obey its sign-in decision gate.

Pin both provider and voice ID. Gender is a selection constraint, not a voice ID.
For providers without native word timings, transcribe the generated narration
and use those timings for captions.

For Edge voiceover:

```bash
python3 "$SKILL/scripts/auto_edit.py" synthesize-edge \
  --manifest PROJECT/project.json \
  --script PROJECT/voice/narration.txt \
  --allow-cloud
```

For the shared Rumi voice system:

```bash
python3 "$SKILL/scripts/auto_edit.py" synthesize-rumi \
  --manifest PROJECT/project.json \
  --script PROJECT/voice/narration.txt \
  --allow-cloud
```

Do not copy voice credentials or clone identifiers into this skill. Locate the
optional Rumi system through `RUMI_VOICE_SYSTEM`; the known OpenClaw workspace
path is only a compatibility fallback. If no safe catalog is present, do not
offer Rumi and use an explicitly approved available provider instead.

For the shared HyperFrames engine, first build its request:

```bash
python3 "$SKILL/scripts/auto_edit.py" audio-request \
  --manifest PROJECT/project.json \
  --script PROJECT/voice/narration.txt
```

Then call the exact discovered engine documented by `hyperframes-media`; do not
download or vendor it implicitly.

### 8. Render and verify

Render original-resolution assets with the bundled page-editor FFmpeg renderer,
or choose one discovered primary renderer before building the timeline. Run the
bundled final delivery QA and inspect its contact sheet:

```bash
python3 "$SKILL/scripts/qa_video.py" \
  --video PROJECT/renders/final.mp4 \
  --report PROJECT/qa/qa-report.json \
  --contact PROJECT/qa/final-contact.png
```

Do not mark the project final when QA returns exit code 2. Treat mechanical QA
as necessary but not sufficient: inspect the full contact sheet, captions,
claims, transitions, and audio before requesting final approval. If
`video-autopilot-macos` is installed, its deeper audio/scene/dead-air QA may be
run as an additional gate.

## Routing table

| Need | Primary route |
|---|---|
| Detect/propose silence, filler, short stutters | bundled analyzer + local Whisper JSON |
| Render explicitly approved cuts | bundled `render_cut.py` |
| Clean timed captions and final render | bundled editor + deterministic FFmpeg renderer |
| Premium subject-occluded caption climax | optional `embedded-captions` |
| Timed title/data/quote/PiP cards | bundled editor; optional `talking-head-recut` |
| Existing cut + SRT + screenshots/HTML scenes | optional `chengfeng-narration-to-video` |
| Chinese Rumi/Fish voice | optional safe catalog via `RUMI_VOICE_SYSTEM` |
| Other TTS, word timing, BGM, SFX | Edge or optional `hyperframes-media` |
| Delivery QA/contact sheet | bundled `qa_video.py`; optional deeper QA bridge |
| Live page editing/social presets/cover draft | bundled `editor_server.py` |

Read [references/ROUTING.md](references/ROUTING.md) before selecting a renderer.

## Gates and boundaries

- Require explicit approval for `destructive_edit`, `timeline`, and `final`.
- Treat all AI outputs as editable proposals.
- Use only owned/licensed media, fonts, music, and voices; store provenance.
- Do not fabricate B-roll evidence, quotes, product screens, or numerical cards.
- Never invoke CapCut, write CapCut drafts, or call CapCut helpers that mutate data.
- Keep voice generation opt-in and disclose whether text leaves the machine.
- Report project path, manifest, selected route, voice provider/ID, preview URL,
  final MP4, QA report, contact sheet, and unresolved risks.
- Do not claim that a browser-only AI surface can execute this skill without a
  local filesystem, Python, FFmpeg, and permission to access the source video.
- Do not describe the five visual director presets as full semantic recut
  directors. In the current core they control typography, motion, and visual
  density; pacing/cut-strategy personas are a later layer.

## Output Contract

- Output a filesystem project and report its paths, verification, and remaining
  risks in the conversation.
- Require `project.json`, stage states, all three human approval gates, selected
  renderer, voice provider/ID or explicit voice-disabled state, asset provenance,
  final QA report, and contact sheet.
