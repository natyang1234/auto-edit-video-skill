# Auto Edit Video Skill

A portable [Agent Skill](https://agentskills.io/) for reviewed, non-destructive automatic video editing. It turns a local source video plus word-timed Whisper JSON into an editable project with cut proposals, subtitles, emphasis, title/cards, animations, social canvases, deterministic FFmpeg export, and delivery QA.

[繁體中文](README.zh-TW.md)

## What ships in the standalone core

- Immutable source staging and a `project.json` source of truth.
- Low-risk silence, filler, and immediate-stutter proposals.
- Explicit `destructive_edit`, `timeline`, and `final` approval gates.
- Approved-cut FFmpeg renderer and mandatory re-transcription workflow.
- Chinese, English, bilingual, or hidden subtitle modes.
- Local page editor with timed text/media layers and social canvas presets.
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
- A local Whisper-compatible transcript with word timestamps. The skill accepts the JSON rather than silently uploading source audio.

Optional: `edge-tts`, Node.js/HyperFrames, or separately installed visual/caption skills. Cloud narration always requires explicit consent.

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

## Minimal workflow

```bash
SKILL=/absolute/path/to/auto-edit-video

python3 "$SKILL/scripts/auto_edit.py" init \
  --input /absolute/path/source.mp4 \
  --project-dir /absolute/path/project \
  --source-language zh-TW \
  --subtitle-mode zh

python3 "$SKILL/scripts/auto_edit.py" import-whisper \
  --manifest /absolute/path/project/project.json \
  --whisper-json /absolute/path/whisper.json \
  --model large-v3-turbo

python3 "$SKILL/scripts/auto_edit.py" analyze-edits \
  --manifest /absolute/path/project/project.json

python3 "$SKILL/scripts/auto_edit.py" editor \
  --project-dir /absolute/path/project --port 8765 --open
```

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
- Uploaded assets are project-scoped, type/size checked, and provenance-recorded.
- No package manager or system dependency is invoked by the installer.
- No cloud TTS runs without an explicit `--allow-cloud` decision.
- AI cut, subtitle, translation, and visual suggestions remain proposals until reviewed.

## Development

```bash
python3 scripts/validate_skill.py
python3 -m unittest discover -s skills/auto-edit-video/tests -p 'test_*.py'
bash tests/test_install.sh
```

Licensed under the [MIT License](LICENSE).
