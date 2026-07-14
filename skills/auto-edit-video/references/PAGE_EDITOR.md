# Local page editor

## What is implemented

The bundled editor is a local, project-scoped workstation rather than a hosted
service. It reads `project.json`, transcript/cut proposals, emphasis/visual
plans, source QA, and `working/editor_state.json`.

- Loopback-only new-project Studio with local File import, owned immutable copy,
  media validation, local Whisper, and visible pipeline progress.
- Live source-video preview with timed editable layers.
- Hybrid editorial workstation: warm source/inspector panels, dark preview and
  multitrack timeline, with vermilion actions and amber emphasis.
- Project-scoped editing brief, selectable director cards, and quick actions for
  subtitles, emphasis text, title cards, animations, images, GIFs, and video.
- Up to ten transcript-grounded semantic highlights with source timing,
  evidence, score, strategy, per-clip keep/reject, title/range editing, and a
  clip-scoped timeline.
- Font family, size, fill/accent color, X/Y position, maximum width,
  visibility, timing, and `none` / `fade` / `pop` / `slide-up` motion.
- Character-range caption effects: select exact text and add editable
  `pop`, `highlight`, or `underline` spans with color and scale controls.
- Drag-to-position for the selected caption/card layer, card width/height
  controls, and live safe-zone/timed-layer collision warnings.
- Designed-mode captions and effect spans are compiled into the same
  HTML/GSAP package as the five visual cards. Full-screen hook cards replace
  their overlapping caption interval instead of drawing two text systems on
  top of one another.
- Text, emphasis, title, card, image, GIF, and inserted-video layers.
- Instagram Reels, YouTube Shorts, YouTube 16:9, TikTok, and two editorial
  Xiaohongshu working presets with platform safe-zone guides.
- Five deterministic director profiles: professional teaching, viral short,
  gossip/current-affairs, POV/hidden-camera, and concise editor. They alter
  semantic-selection duration/pacing/signal weights and visual typography/
  motion/density; they are not autonomous LLM agents.
- Five-track source/animation/card/subtitle/audio timeline. Clicking a
  text block selects its timed layer and focuses the subtitle editor.
- Deterministic transcript-based publishing-copy draft and cover-frame render.
- Versioned per-clip preview/final MP4s, audible-audio normalization, frozen
  render snapshots, SHA-256 receipts, automatic QA/contact sheet, and approved
  final download.
- Revision-bound `destructive_edit` → `highlight_selection` → `timeline` →
  `final` gates with stale-write rejection and downstream invalidation.

The Xiaohongshu 3:4 and 9:16 entries are working presets. Do not claim they are
verified current official video-post requirements without a fresh primary-source
check.

## Launch a new-project Studio

```bash
SKILL="/absolute/path/to/the/installed/auto-edit-video"

python3 "$SKILL/scripts/auto_edit.py" studio \
  --projects-root /absolute/path/to/auto-edit-projects \
  --host 127.0.0.1 \
  --port 8765 \
  --open
```

The Studio File picker is not an external upload. The browser streams the file
to the loopback process, which creates a project-owned copy and never changes
the selected source.

## Reopen one existing project

```bash
SKILL="/absolute/path/to/the/installed/auto-edit-video"

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /absolute/path/to/project \
  --host 127.0.0.1 \
  --port 8765 \
  --open
```

If the port is occupied, choose another port. Keep the loopback default. A
non-loopback bind requires `--allow-remote` and a trusted network.

## Review order

1. Wait for local transcription/planning; correct Whisper text and inspect every
   cut suggestion.
2. Approve destructive-edit decisions. The page renderer does not apply
   interior deletes: set them keep or run the destructive cut renderer first.
3. Review all highlight candidates, mark at least one keep, reject the rest, and
   approve the current highlight-selection revision.
4. Review auto emphasis/title/card layers; remove claims created from bad ASR.
5. Pick the social canvas, inspect its safe zone and collision status, select
   any inline emphasis words, then adjust typography, positions, card size,
   motions, and licensed assets.
6. Render and watch a versioned preview MP4 for the selected clip.
7. Approve the current timeline revision, then render final.
8. Inspect the full final playback and QA contact sheet. Approve final only when
   QA is current and the output is correct; download then becomes available.

Changing render-affecting state automatically invalidates affected highlight,
timeline, and final approvals. Publishing text or merely selecting a clip/layer
does not alter the render revision. Re-rendering final invalidates the prior
final approval even if the timeline did not change.

## Security and provenance

- Server routes are scoped to the selected project and assets/renders cannot
  traverse into other project files.
- Studio File imports use CSRF, Host/Origin checks, byte and disk limits,
  filename/MIME/container validation, ffprobe stream limits, SHA-256, and atomic
  project creation.
- Loopback Host headers and mutating browser origins are checked to block DNS
  rebinding and cross-site writes.
- Uploads are size/type limited and recorded in `assets/provenance.json`.
- Uploaded or generated media must still be owned/licensed and fact-checked.
- Cloud TTS is never invoked by opening the editor. Rumi/Edge voice generation
  remains an explicit opt-in workflow.
- Final approval validates current output, render receipt, QA report, and
  contact-sheet hashes; changed artifacts make the approval non-current.

## Known boundaries

- The Studio creates semantic proposals, not guaranteed editorial truth. Local
  Whisper errors and strategy scores require human review.
- Studio can propose up to ten highlights, but renders one selected clip at a
  time. Batch-exporting all ten in one click is not implemented yet.
- Reviewed interior delete decisions must use the destructive cut renderer
  before page-editor final render; the page renderer fails closed instead of
  silently ignoring them.
- Local copy generation is a conservative draft, not a full brand-trained
  platform strategist.
- Content-related stock/GIF retrieval is not automatic; the local planner makes
  transcript-grounded text-card proposals and accepts licensed uploads.
- A source that already contains burned captions will show duplicate captions
  if generated caption layers remain visible. Use clean source footage for a
  production edit.
- CapCut is intentionally absent.
