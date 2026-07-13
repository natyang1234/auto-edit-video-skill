# Voice and subtitle options

## Table of contents

- Subtitle modes
- Voice engines
- Rumi shared-system presets
- Built-in Edge presets
- Timing and mixing
- Privacy and consent

## Subtitle modes

| Mode | Output |
|---|---|
| `source` | Corrected subtitles in the spoken language |
| `zh` | Traditional Chinese subtitle track |
| `en` | English subtitle track |
| `bilingual` | Paired Chinese/English lines on one timing rail |
| `off` | No visible subtitles; keep transcript metadata |

For bilingual captions, the spoken line owns the timing. Keep both lines short;
split at the same semantic boundary. Preserve terminology with a project glossary.
Do not translate brand names, model names, URLs, or numbers unless explicitly
localized.

## Voice engines

| Selection | Behavior |
|---|---|
| `rumi` | Optional installed Rumi safe catalog; Fish Audio for named Chinese voices and Edge for neural fallbacks |
| `edge` | Online Microsoft Edge TTS; deterministic locale/gender presets; no key |
| `auto` | HyperFrames engine chooses HeyGen → ElevenLabs → Kokoro |
| `heygen` | Cloud; voice ID must be a Starfish voice UUID |
| `elevenlabs` | Cloud; pin the dashboard voice UUID |
| `kokoro` | Local after model/dependencies are installed; pin voice ID |

For `rumi`, set `RUMI_VOICE_SYSTEM` to an installed, audited `tts_voices.py` safe
catalog. A known OpenClaw workspace path may be auto-detected for compatibility.
Never assume a private JARVIS configuration exists, and never copy an API key or
clone reference into a project, manifest, log, or published skill.

For providers other than `rumi` and `edge`, require a separately installed and
discovered `hyperframes-media` audio engine. Do not download or copy provider
implementations implicitly.

## Rumi shared-system presets

When voiceover is enabled and no provider is specified:

| Language / gender | Provider | Voice ID | Backend |
|---|---|---|---|
| Chinese female/male with safe catalog | `rumi` | catalog-selected, pinned ID | Catalog-declared backend |
| Chinese female/male without safe catalog | `edge` | Locale/gender preset | Edge TTS |
| English female/male | `edge` | Locale/gender preset | Edge TTS |

Treat the output of `auto_edit.py voices` as authoritative because the optional
catalog may evolve. Never advertise a named private voice unless that installed
catalog actually exposes it and the operator is authorized to use it.

Do not substitute a similarly named legacy voice script for the selected safe
catalog; provider and voice identifiers must remain pinned and auditable.

## Built-in Edge presets

| Locale | Female | Male |
|---|---|---|
| `zh-TW` | `zh-TW-HsiaoChenNeural` | `zh-TW-YunJheNeural` |
| `zh-CN` | `zh-CN-XiaoxiaoNeural` | `zh-CN-YunxiNeural` |
| `en-US` | `en-US-AvaMultilingualNeural` | `en-US-AndrewMultilingualNeural` |
| `en-GB` | `en-GB-SoniaNeural` | `en-GB-RyanNeural` |

Run `auto_edit.py voices --live` to refresh the current Edge catalog. A manifest
always stores the resolved voice ID, never only `male` or `female`.

## Timing and mixing

- Generate/save `narration.txt`, the exact spoken text after pronunciation
  substitutions.
- Rumi/Fish/cloud/Edge output must be treated as newly recorded audio and timed again.
- HeyGen native word timestamps may be used directly after validation.
- Fish, ElevenLabs, Kokoro, and Edge output should be transcribed to word timings for
  final captions.
- `replace` replaces the dialogue track; `add` is for footage with no competing
  speech. Do not layer two intelligible voices.
- Keep voice speed normally between `0.8` and `1.2`.

## Privacy and consent

- Rumi/Fish, Edge, HeyGen, and ElevenLabs send narration text to an external service.
- Rumi synthesis requires `--allow-cloud`; the command never prints credentials
  and dry-run output redacts narration text.
- Edge synthesis requires `--allow-cloud`.
- HyperFrames authentication preflight must be shown before cloud generation.
- Kokoro is the offline choice, but may download model files during setup.
- Never send an unpublished transcript or confidential script externally without
  explicit authorization.
