# Local page editor

## What is implemented

The bundled editor is a local, project-scoped workstation rather than a hosted
service. It reads `project.json`, transcript/cut proposals, emphasis/visual
plans, source QA, and `working/editor_state.json`.

- Loopback-only new-project Studio with local File import, owned immutable copy,
  media validation, local Whisper, and visible pipeline progress.
- Chinese/English code-switch mode with an optional terminology glossary,
  conservative close-spelling repair, readable word-timed caption chunks, and
  a project-local transcript review report.
- Default-on whole-transcript contextual calibration through loopback Ollama:
  every caption is checked against the numbered whole-document transcript plus
  previous/next context, each proposed patch is separately verified, and the
  GUI reports checked/total, corrected, and
  pending counts. Pending captions receive an amber layer marker with the
  source → proposed wording so the existing subtitle editor can resolve them.
  During a long local pass, pipeline status reports live checked/total progress.
  Removing the flagged source through a manual caption edit clears that layer's
  pending marker. Partial coverage stops highlight planning.
- Live source-video preview with timed editable layers.
- Hybrid editorial workstation: warm source/inspector panels, dark preview and
  multitrack timeline, with vermilion actions and amber emphasis.
- Project-scoped editing brief, selectable director cards, and quick actions for
  subtitles, emphasis text, title cards, animations, images, GIFs, and video.
- Explicit-consent Openverse and Wikimedia Commons still-image search. Results
  render as local metadata rows with source links; approved CC0/CC BY 4.0
  imports become project-private files with SHA-256 provenance, deterministic
  `ATTRIBUTION.md`, and final-rights-gate checks.
- Per-project explicit network-disclosure consent for Studio provider search.
  Font search shows metadata only: Google Fonts uses pinned-commit
  `(ofl|apache|ufl)/family` queries and Fontsource uses exact `id@x.y.z` semver.
  Explicit import strictly validates and stores project-private TTF/OTF plus its
  license and SPDX evidence. The exact `font_asset_id` must cover actual
  rendered glyphs; missing, tampered, or uncovered fonts block preview/final.
  `AUTO_EDIT_FONT` is an explicitly unverified legacy fallback only without a
  selected project font ID.
- SVG metadata search for Heroicons, Lucide, Tabler, and Wikimedia. Raw SVG
  never reaches the DOM or timeline: strict sanitization and bounded PNG
  rasterization are required. If pinned local `resvg` is absent, production UI
  labels SVG import unavailable and fails closed.
- Independent video-template catalog: three fixed-camera layouts with no source
  transform tween, two opt-in dynamic layouts, and three local subject-cutout
  layouts for solid, owned-image, or owned-looping-video backgrounds. Frame
  X/Y/width/height and cutout subject X/Y/scale are editable.
- Up to ten transcript-grounded semantic highlights with source timing,
  evidence, score, strategy, per-clip keep/reject, title/range editing, and a
  clip-scoped timeline.
- Verified project font, size, fill/accent color, X/Y position, maximum width,
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
- Versioned per-clip preview/final MP4s plus one-queue final rendering for all
  approved highlights, audible-audio normalization, frozen render snapshots,
  SHA-256 receipts, automatic QA/contact sheets, and approval-gated individual
  or ZIP downloads.
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
5. Pick the social canvas and video template. For cutout, position the subject,
   choose the background, and render a preview MP4 to inspect real mask edges.
6. Inspect the safe zone and collision status, select
   any inline emphasis words, then adjust typography, positions, card size,
   motions, and licensed assets.
7. Render and watch a versioned preview MP4 for the selected clip.
8. Approve the current timeline revision, then render the selected final clip
   or batch-render every highlight marked keep.
9. Inspect every final playback and QA contact sheet. Approve final only when
   the single or aggregate QA contract is current and every output is correct;
   individual MP4 and batch ZIP downloads then become available.

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
- Subject extraction uses only the detected local rembg environment and local
  model. Background asset hashes are revision-bound; missing, changed, or
  symlinked assets fail closed before render.
- Cloud TTS is never invoked by opening the editor. Rumi/Edge voice generation
  remains an explicit opt-in workflow.
- Final approval validates every current output, render receipt, QA report,
  contact-sheet hash, and the batch ZIP when applicable; changed artifacts make
  the approval non-current.

## Known boundaries

- The Studio creates semantic proposals, not guaranteed editorial truth. Local
  Whisper errors and strategy scores require human review.
- Batch final renders only highlights explicitly marked keep. Pending or
  rejected proposals remain outside the queue, and any failed item prevents a
  partial batch from replacing the previous successful delivery contract.
- Reviewed interior delete decisions must use the destructive cut renderer
  before page-editor final render; the page renderer fails closed instead of
  silently ignoring them.
- Local copy generation is a conservative draft, not a full brand-trained
  platform strategist.
- Openverse/Wikimedia still-image search requires provider disclosure consent
  and an explicit import click. Font and SVG provider search have the bounded
  project-private workflows above; provider metadata/evidence is not legal
  advice or a guarantee of rights. Tests use fake transport/rasterizer and do
  not perform live provider search. Arbitrary URLs, GIF, and B-roll provider
  retrieval remain unsupported; licensed local uploads are still supported.
- A source that already contains burned captions will show duplicate captions
  if generated caption layers remain visible. Use clean source footage for a
  production edit.
- Burned-in source graphics may also be retained by the person mask. Use clean
  A-roll when cutout quality matters; the browser view is positioning-only and
  the rendered preview is the mask-quality truth.
- CapCut is intentionally absent.
