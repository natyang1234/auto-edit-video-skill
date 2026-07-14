---
name: auto-edit-video
description: >-
  Automatically edit a local video file supplied by the user. In the default
  agent-first mode, read the video, create the internal project and local
  transcript, apply conservative reviewed cuts, export a new MP4, run QA, and
  return the result without requiring a UI or user-supplied Whisper files. Use
  when the user gives a video path and asks to auto edit, clean up speech,
  remove silence/fillers/stutters/repetitions/false starts, or make a short
  highlight. Support platform-aware short, medium, long, automatic, full, and
  custom-second targets without hard-coding 30 seconds. When the user asks for
  an interface, launch the loopback Studio for local import, editing intent,
  five deterministic director profiles, up to ten reviewed highlights,
  captions/layers, preview, QA-bound final approval, and download. Never uploads
  externally by default and never uses CapCut.
---

# Auto Edit Video

Create a non-destructive, reviewable project before rendering. Use the bundled
FFmpeg cut, page-editor, timeline-render, and QA core. Discover optional installed
skills for premium captions, visual packages, creator profiles, or cloud voices;
their absence must not block the standalone core.

## Agent-first mode (default)

Read [references/AGENT_FIRST.zh-TW.md](references/AGENT_FIRST.zh-TW.md). The
user-facing contract has one required input: a local video file that the agent
can read. The agent owns every internal artifact and must return an edited MP4,
not instructions asking the user to prepare Whisper JSON, SRT, a manifest, or a
browser preview.

Example user request:

```text
Use $auto-edit-video to automatically edit /path/to/input.mp4 and return the MP4.
```

Do not launch the page editor in this mode unless the user explicitly asks for
an interface. Do not upload or deliver the result to another service unless that
is a separate authorized request.

Internally, run preflight, initialize a project, generate a local word-timed
transcript with an installed Whisper-compatible engine, import it, analyze
edits, write conservative decisions, record the user's auto-edit request as the
low-risk destructive-edit authorization, render, and run QA. High-risk cuts
default to keep.

If no highlight or length is requested, clean the full video conservatively.
When the user selects a platform and `short`, `medium`, or `long`, use the
platform matrix in the Agent-first specification. A user-specified number of
seconds overrides the profile. Use `auto` only when the user asks the agent to
choose: after transcription, select the smallest profile that preserves a
complete idea. Never pad, stretch, or change playback speed merely to hit a
target.

## Local Studio GUI (only when explicitly requested)

Read [references/PAGE_EDITOR.md](references/PAGE_EDITOR.md), then launch:

```bash
python3 "$SKILL/scripts/auto_edit.py" studio \
  --projects-root /absolute/path/to/auto-edit-projects \
  --host 127.0.0.1 --port 8765 --open
```

The Studio accepts one browser File stream over loopback, validates its size,
extension, MIME, container, streams, duration, resolution, and frame rate, then
atomically creates an immutable project-owned source copy. It runs local
Whisper, low-risk edit analysis, overlay planning, and transcript-grounded
highlight planning without setting any human approval.

The GUI supports platform and duration presets, a natural-language editing
brief, five deterministic strategy profiles, up to ten sourced highlight
proposals, per-clip keep/reject and timing/title edits, editable captions,
title/cards/animations and licensed project assets, clip-scoped timeline
preview, and separate clip rendering. These profiles are tested heuristic
strategies, not five autonomous LLM agents.

Studio source language includes `zh-en` for Chinese/English code switching and
an optional semicolon-separated terminology glossary. The separate subtitle
calibration field accepts audited `canonical=alias|alias` rules such as
`複數=富數;It is=意思;例句=音譽句`; replacements preserve the matched source time
span, reuse Whisper word boundaries where possible, and optional manifest
`start`/`end` scopes constrain ambiguous aliases. Auto and Chinese source modes also prompt local Whisper to retain
incidental English spelling. Import writes both
`working/transcript_review.json`, `working/transcript_calibration.json`, and
`working/transcript_orthography.json`,
bounds the prompt and excludes long glossary sentences so English hints do not
degrade adjacent Chinese, safely repairs close spellings or split tokens, and
creates readable caption chunks from word timings. After semantic/glossary
correction and before any SRT, GUI, highlight, or card artifact is built,
`zh-TW`, `zh-en`, and auto-detected Chinese transcripts are normalized with the
vendored OpenCC `s2twp` Taiwan dictionary. Latin text and word timings remain
unchanged. Explicit `zh-CN` is the supported opt-out.

Director profiles control copy, captions, cards, and motion language. The
separate video-template selector controls source framing and compositing: three
templates are fixed (`camera_motion=none`), two opt into reframe/punch motion,
and three use a local rembg subject mask over solid, project-owned image, or
project-owned looping-video backgrounds. Cutout controls include subject X/Y/
scale. Treat the browser cutout view as a positioning guide and render a preview
MP4 to review real mask edges. Never substitute a cloud background-removal API.
If the local rembg environment or model is absent, leave those templates
disabled and report the missing capability.

Keep the approval order:
`destructive_edit` → `highlight_selection` → `timeline` → `final`. Every request
uses the server-provided current revision. A final render freezes state/source/
asset/clip/approval hashes, renders to an atomic versioned MP4, normalizes
audible audio, runs mechanical QA, and creates a contact sheet. The GUI exposes
the final download only after the current delivery receipt and contact sheet are
human-approved. If reviewed decisions contain interior `delete` actions, the
page renderer fails closed; run `render_cut.py` first or change those decisions
to keep.

## Internal quick start

```bash
SKILL="/absolute/path/to/the/installed/auto-edit-video"

python3 "$SKILL/scripts/auto_edit.py" preflight

python3 "$SKILL/scripts/auto_edit.py" duration-presets

python3 "$SKILL/scripts/auto_edit.py" init \
  --input /absolute/path/source.mp4 \
  --project-dir /absolute/path/project \
  --platform youtube-shorts \
  --duration-profile long \
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

python3 "$SKILL/scripts/auto_edit.py" plan-highlights \
  --manifest /absolute/path/project/project.json \
  --director teacher-punch --count 10 \
  --brief "Keep the clearest complete teaching points"

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /absolute/path/project \
  --port 8765 --open
```

Omit every `--voice-*` flag to keep the original voice. Voiceover is opt-in.
The `editor` command is optional and is not part of the current agent-first
acceptance path.

For an automatic duration decision, initialize with `--duration-profile auto`,
transcribe locally, then persist the semantic decision before editing:

```bash
python3 "$SKILL/scripts/auto_edit.py" set-target \
  --manifest /absolute/path/project/project.json \
  --platform tiktok \
  --duration-profile medium
```

Use `--target-duration 75` on `init` or `set-target` when the user gives an
explicit approximate number of seconds. The target is editing metadata only;
this phase exports an MP4 and does not publish it.

## Advanced reviewed workflow

The sections below define the full captions/cards/voice/editor workflow. The
current agent-first cut-only path uses sections 1–4, re-checks the rendered MP4,
then runs the QA step in section 8 without opening the page editor.

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

Resolve `output_target` from the user's natural language. Default to `full` when
the request is only cleanup. Use `short`, `medium`, or `long` when explicitly
selected. Use `auto` only when the user requests a semantic choice, then call
`set-target` after reading the transcript so the manifest contains the chosen
platform, profile, range, and target seconds.

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
For Chinese/English teaching or interview footage, use source language `zh-en`
and supply known English terms through `--transcription-glossary`. When exact
ASR homophones are known, add `--transcription-calibration
"複數=富數;雪茄=學家|雪家"`. A mechanical issue count of zero is not semantic
clearance: `not_configured`, `applied_needs_review`, and human approval are
separate states. Do not treat the local transcript as reviewed merely because
glossary checks are clear or calibration rules were applied.

Apply [references/EDITING_RULES.md](references/EDITING_RULES.md). If
`chengfeng-cut-narration` is installed, its review UI may be used, but do not use
any temporary public upload path unless the user explicitly authorizes sending
audio to that service.

Write:

- `working/transcript_words.json`
- `working/transcript_calibration.json`
- `working/transcript_orthography.json`
- `working/transcript_review.json`
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
python3 "$SKILL/scripts/auto_edit.py" status \
  --manifest PROJECT/project.json

python3 "$SKILL/scripts/auto_edit.py" approve \
  --manifest PROJECT/project.json \
  --gate destructive_edit \
  --expected-revision REVISION_FROM_STATUS \
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
contrasts, promises, and conclusions. Default to one emphasized phrase per
caption line and allow at most two when both are essential to the idea; do not
animate every word.

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
Use `studio` to import a new A-roll or `editor` to reopen an existing project.
Both are loopback-only by default. The editor supports reviewed semantic
highlights, clip-scoped video/timeline preview, font/color/size and position
controls, selectable inline caption effects, drag-to-position caption/card
layers, card width/height, timed overlap warnings, text motions,
PNG/JPG/WEBP/GIF/MP4/MOV layers, platform safe zones, local publishing-copy
drafts, cover frames, preview render, QA-bound final render, contact-sheet
review, and approved download.

Treat the HTML preview as an editing surface, then render an MP4 preview for
truth checking. Highlight, timeline, and final approvals are bound to their
exact state revisions; changing a clip, caption, layer, canvas, or render style
invalidates the affected downstream approval. Final approval additionally
binds the output, render receipt, QA report, and contact-sheet hashes.

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
or choose one discovered primary renderer before building the timeline. Final
renders started from the Studio automatically run the bundled delivery QA and
bind its artifacts to the final gate. For a cut-only manual output where the
advanced editor final gate is not applicable, run QA and inspect its contact
sheet:

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

- In agent-first cut-only mode, the user's explicit request to auto-edit the
  supplied file may authorize conservative low-risk decisions when that request
  is recorded in `destructive_edit`; high-risk decisions still default to keep.
- Require `highlight_selection`, `timeline`, and `final` approvals when the
  optional Studio/page-editor highlight, captions/cards, or publishing workflow
  is used. Highlight selection is not applicable when no highlight plan exists;
  timeline/final may be marked not applicable for a cut-only output that is not
  externally delivered.
- Treat all AI outputs as editable proposals.
- Use only owned/licensed media, fonts, music, and voices; store provenance.
- Do not fabricate B-roll evidence, quotes, product screens, or numerical cards.
- Never invoke CapCut, write CapCut drafts, or call CapCut helpers that mutate data.
- Keep voice generation opt-in and disclose whether text leaves the machine.
- Report project path, manifest, selected route, voice provider/ID, preview URL,
  final MP4, QA report, contact sheet, and unresolved risks.
- Do not claim that a browser-only AI surface can execute this skill without a
  local filesystem, Python, FFmpeg, and permission to access the source video.
- Do not describe the five director profiles as autonomous LLM agents. They are
  deterministic, tested semantic-selection and visual-style strategies whose
  duration, pacing, signal weights, typography, motion, and density differ.

## Output Contract

- Output a filesystem project and report its paths, verification, and remaining
  risks in the conversation.
- In agent-first mode, the only required user input is the readable source
  video. Return a real edited MP4 after QA; do not return only a plan or preview.
- Agent-first cut-only output requires `project.json`, edit decisions, recorded
  low-risk authorization, an immutable source, the edited MP4, and a QA report.
- The advanced visual workflow additionally requires all four human approval
  gates when highlights are present, selected renderer, voice provider/ID or
  explicit voice-disabled state, asset provenance, a current delivery receipt,
  and contact-sheet inspection.
