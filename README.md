# Auto Edit Video Skill

A portable [Agent Skill](https://agentskills.io/) for reviewed, non-destructive automatic A-roll video editing. Give a local agent one source video; the agent creates its own local word-timed transcript and project, then produces cut proposals, an edited MP4, and delivery QA. An optional loopback-only Studio adds local video import, editing intent, five deterministic director profiles, up to ten transcript-grounded highlights, editable captions/layers, preview, final QA, and reviewed download.

[繁體中文](README.zh-TW.md)

[Agent-first phase specification (Traditional Chinese)](skills/auto-edit-video/references/AGENT_FIRST.zh-TW.md)

## Default mode: give the agent one video

No user interface is required for the default agent-first path. Give a local coding agent one readable video:

```text
Use auto-edit-video to automatically edit ./input.mp4 and return the MP4.
```

The agent owns project setup, local transcription, conservative edit decisions,
rendering, and QA. The user does not need to prepare Whisper JSON, SRT, a
manifest, or a preview page. Without a requested duration, the default is a
conservative full-length cleanup. Duration is not fixed at 30 seconds: the user
can select a platform plus `short`, `medium`, or `long`, ask the agent to choose
`auto` after transcription, or provide explicit seconds. UI, LINE delivery,
external uploads, and social publishing are not required by this path. LINE or
other external delivery remains a separate explicitly authorized action.

## Optional local Studio GUI

When the user explicitly asks for an interface, launch the bundled Studio:

```bash
SKILL=/absolute/path/to/auto-edit-video
python3 "$SKILL/scripts/auto_edit.py" studio \
  --projects-root /absolute/path/to/auto-edit-projects \
  --port 8765 --open
```

The browser sends the selected local File only to the loopback Studio. Studio
validates it, creates an immutable owned project copy, runs local Whisper, and
produces up to ten transcript-grounded highlight proposals. The user can choose
short/medium/long platform targets, describe the desired edit, select one of
five deterministic director profiles, keep or reject clips, correct caption
text/timing, select words inside a caption for exportable pop/highlight/
underline effects, drag caption/card layers, adjust their width/height, and see
safe-zone or timed-layer collision warnings. Users can also add title/cards/
animations or licensed media, preview one clip, and render it separately. In
designed mode, captions and effect spans are baked into the same HTML/GSAP
graphic package used by the rendered MP4.

For mixed Chinese/English footage, select `Chinese + English` and optionally
enter semicolon-separated terminology such as `It; to V; cigarette`. Auto and
Chinese modes also tell local Whisper to preserve incidental English spelling.
The prompt is deliberately bounded and excludes long glossary sentences so an
English hint cannot degrade adjacent Chinese recognition. The project stores a
transcript-review report, applies conservative spelling/split-token corrections,
and generates readable timed captions instead of exposing 30-second Whisper
blocks in the GUI. A separate calibration field accepts audited rules such as
`複數=富數; It is=意思; 例句=音譽句`. Canonical text and aliases may have different
lengths; replacements preserve the matched source interval, reuse Whisper word
boundaries where possible, write a correction audit, and may be time-scoped in the manifest. A zero
mechanical-warning count is never presented as semantic review; calibration
remains `applied_needs_review` until a person checks the captions.

After those corrections, Chinese transcript truth is normalized before SRT,
GUI, emphasis, card, and highlight generation. `zh-TW`, `zh-en`, and
auto-detected Chinese use a bundled OpenCC Taiwan phrase dictionary, while an
explicit `zh-CN` source remains Simplified. English tokens and all word timings
are preserved, and `working/transcript_orthography.json` records the conversion.

Video layout is selected independently from the director profile. The editor
ships three fixed-camera templates, two opt-in dynamic-camera templates, and
three local subject-cutout templates for solid, project-owned image, or
project-owned looping-video backgrounds. Fixed templates emit no source zoom or
reframe tween. Cutout templates expose subject X/Y/scale controls and preserve
the original source audio in the final timeline render. The canvas labels its
browser view as a positioning preview; actual cutout edges are shown by the
rendered preview MP4.

Approvals are revision-bound and ordered:
`destructive_edit` → `highlight_selection` → `timeline` → `final`. Final render
automatically creates a frozen render snapshot, SHA-256 receipt, mechanical QA
report, and contact sheet. Download is exposed only after the current QA result
and final artifact are manually approved. Reviewed interior delete decisions
are intentionally fail-closed in the page renderer; use `render_cut.py` first,
or mark those proposals keep, until the two render paths are composed.

## Platform duration presets

Parentheses show the editorial target. See the [phase-one specification](skills/auto-edit-video/references/AGENT_FIRST.zh-TW.md) for selection rules and official platform references.

| Platform | Short | Medium | Long |
|---|---:|---:|---:|
| Generic vertical / Instagram Reels | 15–30s (30) | 45–90s (60) | 120–180s (180) |
| YouTube Shorts | 15–30s (30) | 45–60s (60) | 90–180s (180) |
| TikTok | 15–30s (30) | 45–90s (60) | 120–300s (180) |
| Xiaohongshu 3:4 / 9:16 | 15–30s (30) | 45–60s (60) | 90–180s (120) |
| YouTube landscape | 60–180s (120) | 300–600s (480) | 600–1200s (900) |

These are editorial targets, not platform upload limits. `auto` chooses the
smallest profile that preserves a complete idea. A short source is never padded,
repeated, stretched, or speed-adjusted merely to hit the target.

## What ships in the standalone core

- Immutable source staging and a `project.json` source of truth.
- Low-risk silence, filler, and immediate-stutter proposals.
- Explicit `destructive_edit`, `highlight_selection`, `timeline`, and `final` approval gates.
- Approved-cut FFmpeg renderer and mandatory re-transcription workflow.
- Chinese, English, bilingual, or hidden subtitle modes.
- Mixed Chinese/English transcription with a project glossary, auditable
  homophone calibration, and separate mechanical/semantic review states.
- Loopback Studio import plus a local page editor with semantic highlights,
  timed text/media layers, and social canvas presets.
- Deterministic MP4/cover rendering plus mechanical QA and a contact sheet.
- Optional Edge, Rumi/Fish, HyperFrames, premium captions, and visual-card integrations when those tools are already installed.

The skill never invokes CapCut. It does not include API keys, private voices, media, creator profiles, or machine-specific configuration.

## Compatible agents

| Agent/runtime | Install target | Status |
|---|---|---|
| OpenAI Codex | `$CODEX_HOME/skills` or `~/.codex/skills` | Native Agent Skill |
| Claude Code | `~/.claude/skills` | Native custom Skill |
| Grok Build | `~/.grok/skills` | Native Skill; also reads Claude/Agent Skills roots |
| OpenClaw | `~/.openclaw/skills` | Native managed/local Skill |
| Hermes Agent | `$HERMES_HOME/skills` or `~/.hermes/skills` | Native filesystem Skill |
| Other compatible agents | `~/.agents/skills` | Agent Skills standard layout |

This is designed for local coding agents with filesystem, Python, and FFmpeg access. Browser-only chat surfaces cannot edit local videos merely by installing the Markdown instructions.

## Requirements

- macOS, Linux, or Windows through WSL.
- Python 3.10 or newer.
- `ffmpeg` and `ffprobe` on `PATH`.
- A CJK-capable font for Chinese subtitles. Set `AUTO_EDIT_FONT=/absolute/font/path` when auto-discovery is insufficient.
- A local Whisper-compatible transcription engine that can produce word timestamps. The agent creates the transcript from the input video rather than asking the user for JSON or silently uploading source audio.

Optional: `edge-tts`, Node.js/HyperFrames, or separately installed visual/caption skills. The three subject-cutout templates additionally require a local `rembg` CPU environment and an already-downloaded `isnet-general-use.onnx` model; they disable themselves when either is missing and never upload frames. Cloud narration always requires explicit consent.

## Install

Clone once and install for every supported local agent:

```bash
git clone https://github.com/natyang1234/auto-edit-video-skill.git
cd auto-edit-video-skill
./install.sh --agent all
```

Install only one target:

```bash
./install.sh --agent codex
./install.sh --agent claude
./install.sh --agent grok
./install.sh --agent openclaw
./install.sh --agent hermes
./install.sh --agent shared
```

The installer refuses to overwrite an existing copy. Use `--force` to replace it; the old copy is timestamp-backed up first. Use `--dry-run` to inspect destinations.

Codex users can also ask the built-in skill installer to install the skill directory directly:

```text
$skill-installer install https://github.com/natyang1234/auto-edit-video-skill/tree/main/skills/auto-edit-video
```

Start a new agent session after installation so its skill catalog refreshes.

## Verify

Use the installed path for your agent:

```bash
python3 ~/.agents/skills/auto-edit-video/scripts/auto_edit.py preflight
```

A working standalone installation reports `"ready": true` and `"mode": "standalone"` or `"extended"`. Missing optional integrations do not block the bundled core.

## Internal / advanced workflow

These commands are for the agent or developer, not additional required user inputs:

```bash
SKILL=/absolute/path/to/auto-edit-video

python3 "$SKILL/scripts/auto_edit.py" init \
  --input /absolute/path/source.mp4 \
  --project-dir /absolute/path/project \
  --platform youtube-shorts \
  --duration-profile long \
  --source-language zh-TW \
  --transcription-calibration "複數=富數;雪茄=學家|雪家;cigar=ciger" \
  --subtitle-mode zh

python3 "$SKILL/scripts/auto_edit.py" import-whisper \
  --manifest /absolute/path/project/project.json \
  --whisper-json /absolute/path/whisper.json \
  --model large-v3-turbo

python3 "$SKILL/scripts/auto_edit.py" analyze-edits \
  --manifest /absolute/path/project/project.json

python3 "$SKILL/scripts/auto_edit.py" plan-highlights \
  --manifest /absolute/path/project/project.json \
  --director high-energy --count 10 \
  --brief "Lead with the clearest hook and keep claims complete"

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /absolute/path/project --port 8765 --open
```

Run `duration-presets` to inspect the complete matrix. If the project starts
with `--duration-profile auto`, the agent must call `set-target` after local
transcription to persist the selected profile. Use `--target-duration 75` when
the user supplies explicit approximate seconds.

After explicit destructive-edit approval and reviewed decisions:

```bash
python3 "$SKILL/scripts/render_cut.py" \
  --manifest /absolute/path/project/project.json
```

Re-transcribe the cut, finalize the editor timeline, obtain timeline approval, render `final.mp4`, and run:

```bash
python3 "$SKILL/scripts/qa_video.py" \
  --video /absolute/path/project/renders/final.mp4
```

Final approval is a separate human gate after QA and visual inspection.

## Optional integration discovery

The skill searches sibling and user-level roots for compatible optional skills. Add custom roots with an OS-path-separated list:

```bash
export AUTO_EDIT_SKILLS_ROOTS="/opt/agent-skills:$HOME/my-skills"
```

Specific overrides include `VIDEO_AUTOPILOT_SKILL_DIR`, `CUT_NARRATION_SKILL_DIR`, `NARRATION_VIDEO_SKILL_DIR`, `EMBEDDED_CAPTIONS_SKILL_DIR`, `TALKING_HEAD_RECUT_SKILL_DIR`, `HYPERFRAMES_MEDIA_SKILL_DIR`, and `RUMI_VOICE_SYSTEM`.

## Security model

- Original media remains immutable.
- The page editor binds to loopback unless remote access is explicitly enabled.
- Studio imports use a project-owned copy, CSRF/Host/Origin checks, media limits,
  container probing, and atomic project creation.
- Uploaded assets are project-scoped, type/size checked, and provenance-recorded.
- No package manager or system dependency is invoked by the installer.
- No cloud TTS runs without an explicit `--allow-cloud` decision.
- AI cut, subtitle, translation, and visual suggestions remain proposals until reviewed.
- Render snapshots bind the source, referenced assets, selected clip, editor
  revision, and approvals; final approval also binds output/QA/contact-sheet hashes.

## Development

```bash
python3 scripts/validate_skill.py
python3 -m unittest discover -s skills/auto-edit-video/tests -p 'test_*.py'
bash tests/test_install.sh
```

Licensed under the [MIT License](LICENSE).
