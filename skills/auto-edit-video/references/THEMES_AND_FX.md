# Colour themes and effect layers

Moved verbatim from SKILL.md (Local Studio GUI section). `$SKILL` is the installed skill directory.

Colour themes (2026-10-21): besides the three base style packs, palette-catalog
themes are available as `theme-<no>-paper` (paper cards, ink text) and
`theme-<no>-navy` (dark cards, light text). They use a serif display face
(Noto Serif TC, falling back to Songti TC) for titles and stat values. Pin one
for a new project with `init --style-pack theme-075-paper` (or
`cut --style-pack …`). A pinned pack overrides the director's default pack,
and its `caption_emphasis` replaces the director's keyword colour. List the
packs with `python3 "$SKILL/scripts/theme_packs.py" list`. Generate more with
`python3 "$SKILL/scripts/theme_packs.py" build 011 044`. This merges the new
packs into the manifest `contracts/theme_packs.manifest.json`. Add `--replace`
to rewrite the manifest with only the listed themes. Only manifest ids are
registered, and unknown ids fail closed. Theme roles and
recommended presets are documented in `~/.claude/skills/palette-catalog/SKILL.md`.

Effect layers (2026-10-22): the 115 HyperFrames effect blocks of
`~/.claude/skills/edit-style-profiles` (the 風格菜單: hooks, text cards,
person composites, camera transitions, UI overlays, infographics, CTAs,
filters) are one more timed overlay type, `fx`. In the Studio, "加入特效"
adds one at the playhead; the inspector shows the thumbnail menu (category
tabs, search), the block's text variables, placement (top/bottom/left/right,
keeps the face clear) and an optional card scheme. The layer stores only
`code`, `values`, `placement`, `scheme` on the source axis; at render time
`scripts/fx_overlay.py` calls that skill's `compose.py clip` (HyperFrames
`render --format mov`, ProRes 4444 alpha, cached under `working/fx/`) and
the timeline overlays it as a full-frame `video` layer. Colours follow the
project style pack: `theme-<no>-paper` → that palette theme with paper
cards, `theme-<no>-navy` → night cards, anything else → the kwons palette.
Effects marked ⓜ need a transparent person: the renderer runs
`matte_video.py` (RobustVideoMatting, local) on exactly that window and
caches `working/fx/person-<key>.webm`. A failed effect render is reported on
stderr as `fx_fallback` and skipped; it never blocks the delivery. Preview
in the Studio canvas is a thumbnail placeholder; the real pixels appear in
the rendered preview/final. Disable with `AUTO_EDIT_DISABLE_FX=1`; point at
another copy of the skill with `EDIT_STYLE_PROFILES_SKILL_DIR`. Draft
render cost: about 12 s per 3 s effect, plus about 20 s for a ⓜ matte.
The 風格菜單 page itself is served inside the Studio at `/fx-menu`
("風格菜單點菜" in the quick tools): pick a preset or cards there, then
"套用到 Studio" (`POST /api/fx/apply`) lays the chosen codes over the
captions (hook on the first, CTA on the last, the rest spread between),
pins the matching `theme-<no>-paper|navy` pack, and the Studio tab reloads
with the new effect layers.
