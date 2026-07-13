# Local page editor

## What is implemented

The bundled editor is a local, project-scoped workstation rather than a hosted
service. It reads `project.json`, transcript/cut proposals, emphasis/visual
plans, source QA, and `working/editor_state.json`.

- Live source-video preview with timed editable layers.
- Hybrid editorial workstation: warm source/inspector panels, dark preview and
  multitrack timeline, with vermilion actions and amber emphasis.
- Project-scoped editing brief, selectable director cards, and quick actions for
  subtitles, emphasis text, title cards, animations, images, GIFs, and video.
- Up to ten clickable clip-navigation sections derived from the current caption
  timeline, or from reviewed `state.highlights` when an agent supplies them.
- Font family, size, fill/accent color, X/Y position, visibility, timing, and
  `none` / `fade` / `pop` / `slide-up` motion.
- Text, emphasis, title, card, image, GIF, and inserted-video layers.
- Instagram Reels, YouTube Shorts, YouTube 16:9, TikTok, and two editorial
  Xiaohongshu working presets with platform safe-zone guides.
- Five visual direction presets. These are typography/motion/density presets,
  not yet five independent semantic recut algorithms.
- Five-track source/animation/card/subtitle/audio timeline. Double-clicking a
  text block selects its timed layer and focuses the subtitle editor.
- Deterministic transcript-based publishing-copy draft and cover-frame render.
- Preview MP4, final MP4, and approval gates bound to the render-state revision.

The Xiaohongshu 3:4 and 9:16 entries are working presets. Do not claim they are
verified current official video-post requirements without a fresh primary-source
check.

## Launch

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

1. Correct Whisper text and reject wrong cut suggestions.
2. Approve destructive-edit decisions before any source cut.
3. Review auto emphasis/title/card layers; remove claims created from bad ASR.
4. Pick the social canvas and inspect its safe zone.
5. Adjust typography, positions, motions, and uploaded licensed assets.
6. Render a preview MP4 and run video QA/contact-sheet review.
7. Approve the current timeline revision.
8. Render final; run final QA; approve final only if QA and human review pass.

Changing render-affecting state after step 7 automatically invalidates timeline
and final approvals. Publishing text or inspector selection alone does not alter
the render revision.

## Security and provenance

- Server routes are scoped to the selected project and assets/renders cannot
  traverse into other project files.
- Loopback Host headers and mutating browser origins are checked to block DNS
  rebinding and cross-site writes.
- Uploads are size/type limited and recorded in `assets/provenance.json`.
- Uploaded or generated media must still be owned/licensed and fact-checked.
- Cloud TTS is never invoked by opening the editor. Rumi/Edge voice generation
  remains an explicit opt-in workflow.

## Known boundaries

- The source panel displays the A-roll already staged in the project. Creating a
  new project, copying a newly chosen source, transcribing it, and generating a
  semantic recut still belongs to the agent/CLI workflow; the browser does not
  pretend that a local file preview alone completed those steps.
- The ten-section navigator is a review/navigation surface, not ten rendered
  semantic highlight videos. When a future agent writes reviewed highlight
  ranges to `state.highlights`, the same surface displays those ranges directly.
- Local copy generation is a conservative draft, not a full brand-trained
  platform strategist.
- Content-related stock/GIF retrieval is not automatic; the local planner makes
  transcript-grounded text-card proposals and accepts licensed uploads.
- A source that already contains burned captions will show duplicate captions
  if generated caption layers remain visible. Use clean source footage for a
  production edit.
- CapCut is intentionally absent.
