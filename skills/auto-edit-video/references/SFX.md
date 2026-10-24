# Built-in SFX layer

Moved verbatim from SKILL.md section 7.1. Placeholder `PROJECT` is the project directory; `$SKILL` is the installed skill directory.

The skill ships a deterministic sound-effect layer with six semantic
categories: `cash_coin` (money/results beats), `click_pop` (UI reveals, list
steps), `whoosh` (scene/section transitions), `impact_hit` (emphasis on a key
point), `success_ding` (completion/success beats), and `error_glitch`
(mistakes/warnings). The 7 fixed trigger roles (`title_enter`, `row_reveal`,
`count_tick`, `typing`, `transition`, `grid_fill`, `complete`) always resolve
to a fixed category via `scripts/sfx_mapping.ROLE_TO_CATEGORY`; this
role-to-category mapping is unchanged across catalog profiles, but which
asset fills that category depends on the profile: `v1` fixes each role to
its one pinned procedural asset, while `v2` selects the asset from the
frozen catalog per role, preferring external assets.

Two catalog profiles control what non-trigger content-hint events can select
from:

- `v1` (`phase1-local-procedural`): the original 7 procedurally generated WAVs
  only. Fully deterministic, no external assets.
- `v2` (the default profile for the `kinetic-explainer` director profile): the
  same 7 plus 2 additional procedural assets (`coin-blip-v1`,
  `glitch-buzz-v1`), plus 12 Pixabay-licensed WAVs in `assets/sfx/<category>/`
  tracked with source provenance, a hash chain, and approval metadata in
  `assets/sfx/LIBRARY.json`. `scripts/sfx_delivery.load_sfx_library()`
  re-verifies every record's evidence chain on every load and never trusts the
  JSON file at face value.

Select the profile through director_mode `experience.sfx.catalog_profile`
(`"v1"` or `"v2"`). `catalog_profile: "v2"` is rejected when
`experience.sfx.mode` is `"disabled"`.

For explicit event placement, write `working/sfx_hints.json` (contract
`sfx_hints`, schema v1):

```json
{
  "schema_version": 1,
  "hints": [
    {"onset_sample": 480000, "category": "success_ding", "reason": "closing beat"}
  ]
}
```

`onset_sample` is measured on the post-cut, final timeline at 48kHz, and hints
must be strictly increasing by onset. Pass the file to the cut CLI and it is
copied into the project's `working/` directory:

```bash
python3 "$SKILL/scripts/auto_edit.py" cut \
  --manifest PROJECT/project.json \
  --sfx-hints PROJECT/sfx_hints.json
```

`scripts/sfx_suggest.py` is an advisory helper: it scans the final captions
against [references/sfx_keywords.json](sfx_keywords.json) and
writes `working/sfx_suggestions.json`. It only proposes candidate
category/onset pairs; it never edits `sfx_hints.json` or the delivered
timeline, so a suggestion requires an explicit copy into `sfx_hints.json` (or
manual placement) to take effect.

Manage the external asset library with `scripts/sfx_import.py`:

- `import` — normalize a licensed source file, place it under
  `assets/sfx/<category>/`, and append a hashed, approved record to
  `LIBRARY.json`.
- `verify` — recompute every record's hashes and fail with a non-zero exit on
  any tampering or drift, including a hand-edited `LIBRARY.json`.
- `list` — print the current library contents.

All current external assets use the Pixabay Content License (commercial use
allowed, no attribution required, no standalone redistribution of the raw
asset); see [assets/sfx/README.md](../assets/sfx/README.md) for the full
evidence chain.

Density limits apply regardless of profile: a 120ms minimum gap between cues,
at most 2 overlapping cues, and at most 40 cues per minute. Catalog v2
additionally caps `whoosh` to at most 1 cue per 10 seconds and every other
category to at most 1 cue per 5 seconds.

Studio audio-event edits (`audio_event_edits`, moving or re-gaining a cue in
the Studio timing editor) are only supported under `catalog_profile: "v1"`.
A project with `catalog_profile: "v2"` (the `kinetic-explainer` default)
rejects rendering with existing `audio_event_edits`; clear the edits or move
the project to a v1-catalog director before rendering.
