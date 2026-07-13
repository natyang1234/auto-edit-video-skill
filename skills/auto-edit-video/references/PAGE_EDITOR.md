# Local page editor

## What is implemented

The bundled editor is a local, project-scoped workstation rather than a hosted
service. It reads `project.json`, transcript/cut proposals, emphasis/visual
plans, source QA, and `working/editor_state.json`.

- Live source-video preview with timed editable layers.
- Font family, size, fill/accent color, X/Y position, visibility, timing, and
  `none` / `fade` / `pop` / `slide-up` motion.
- Text, emphasis, title, card, image, GIF, and inserted-video layers.
- Instagram Reels, YouTube Shorts, YouTube 16:9, TikTok, and two editorial
  Xiaohongshu working presets with platform safe-zone guides.
- Five visual direction presets. These are typography/motion/density presets,
  not yet five independent semantic recut algorithms.
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

- Local copy generation is a conservative draft, not a full brand-trained
  platform strategist.
- Content-related stock/GIF retrieval is not automatic; the local planner makes
  transcript-grounded text-card proposals and accepts licensed uploads.
- A source that already contains burned captions will show duplicate captions
  if generated caption layers remain visible. Use clean source footage for a
  production edit.
- CapCut is intentionally absent.
