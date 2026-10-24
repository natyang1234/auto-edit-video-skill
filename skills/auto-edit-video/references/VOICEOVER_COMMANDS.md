# Voiceover commands (Edge, Rumi, HyperFrames)

Moved verbatim from SKILL.md section 7. Placeholder `PROJECT` is the project directory; `$SKILL` is the installed skill directory.

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
