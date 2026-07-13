# Production routing

## Table of contents

- Primary rule
- Stage routing
- Route exclusions

## Primary rule

Choose exactly one primary renderer. Supporting workflows may create an overlay,
caption layer, audio asset, or review artifact, but must not each produce a
competing final timeline.

## Stage routing

| Stage | Route | Notes |
|---|---|---|
| Creator context | bundled local context; optional `video-autopilot-macos` | Isolate one verified brand/profile snapshot |
| Local GUI import | bundled loopback Studio | Owned copy, local validation/transcription, no external upload |
| Local source transcription | AutoClip/HyperFrames Whisper | Pin multilingual model for Chinese |
| Cut proposal/review | bundled analyzer/editor | Optional `chengfeng-cut-narration`; avoid public upload by default |
| Semantic highlight proposal | bundled deterministic highlight planner | 1–10 transcript-grounded ranges; human selection required |
| Approved cut render | bundled `render_cut.py` | Requires approved decisions and destructive-edit gate |
| Final cut subtitles | Re-transcribe cut | Do not offset original SRT |
| Rail captions/keyword emphasis | bundled page editor | Deterministic default |
| Premium occluded captions | optional `embedded-captions` | Single subject and explicit identity approval only |
| Timed cards/PiP/data overlays | bundled editor or optional `talking-head-recut` | Source plays unchanged inside the optional route |
| Existing cut + SRT + mixed HTML/screenshots | optional `chengfeng-narration-to-video` | Use its storyboard/timeline gates |
| Synthetic motion insert | optional HyperFrames workflow | Render as an asset or selected primary composition |
| Default Chinese voice | optional Rumi safe catalog | Use only when discovered and explicitly approved |
| Other TTS/BGM/SFX | Edge or optional `hyperframes-media` | No implicit downloads or vendored provider code |
| Final QA | bundled `qa_video.py` | Studio binds report/contact/output hashes; human inspection still required |

When destructive cutting is required, perform it before overlay/card rendering.
The page-editor renderer intentionally rejects a final render when approved
interior delete decisions have not been applied by `render_cut.py`.
When generated TTS replaces the voice, create timing from the generated audio
before caption and visual timing are finalized.

## Route exclusions

- `embedded-captions` is not a general subtitle renderer for multi-speaker or
  already-captioned footage.
- `talking-head-recut` does not retime or delete source footage.
- `chengfeng-narration-to-video` is not the transcription/deletion stage.
- HyperFrames TTS provider IDs are not interchangeable.
- Rumi voice aliases are resolved only by the shared `tts_voices.py` whitelist;
  never substitute a HyperFrames ID or copy a Fish clone reference into the manifest.
- CapCut is disabled in every route.
