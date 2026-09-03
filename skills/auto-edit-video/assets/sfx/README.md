# assets/sfx

External, externally-licensed SFX imported by `scripts/sfx_import.py`. Each
delivered WAV in `<category>/<asset_id>.wav` has a matching record in
`LIBRARY.json` with source provenance, three hashes (source mp3,
delivered wav, decoded PCM), normalizer params, audio metrics, license
evidence, and approval metadata. Original source mp3 files are not stored in
this repo; only their sha256 is retained in each record's `source_sha256`.

## Add a new asset

```
python3 scripts/sfx_import.py import \
  --source <path-to-source-mp3-or-wav> \
  --asset-id <slug>-v<n> \
  --category <cash_coin|click_pop|whoosh|impact_hit|success_ding|error_glitch> \
  --source-page <url> \
  --file-url <url> \
  --author <name> \
  --license pixabay-content-license \
  --license-evidence assets/sfx/licenses/pixabay-content-license-2026-09-03.md \
  --library assets/sfx/LIBRARY.json
```

`asset-id` must match `^[a-z0-9]+(-[a-z0-9]+)*-v[0-9]+$`. Duplicate
`asset-id` values are rejected. Normalization is deterministic: importing the
same source twice produces byte-identical WAV output.

## Verify

```
python3 scripts/sfx_import.py verify --library assets/sfx/LIBRARY.json
```

Recomputes `delivery_sha256`, `decoded_pcm_sha256`, license
`evidence_sha256`, and `record_sha256` for every record and fails (non-zero
exit) on any mismatch, including tampering with a delivered WAV or a hand
edited `LIBRARY.json`.

## License

All current assets use the Pixabay Content License
(https://pixabay.com/service/license-summary/), snapshotted at
`licenses/pixabay-content-license-2026-09-03.md`. Content is used mixed into
video deliveries, not redistributed standalone; attribution is not required
but the source author is still recorded in each record's `source.author`.

## How this library reaches a delivered video (sfx_catalog v2)

`scripts/sfx_delivery.load_sfx_library()` re-verifies every record's full
evidence chain (record hash, delivered-wav hash, decoded-PCM hash, license
evidence hash; rejects symlinks and path escapes) on every load — it never
trusts `LIBRARY.json` at face value. `stage_multi_event_delivery(...,
catalog_profile="v2")` then merges these 12 external-licensed assets with the
9 built-in procedural assets into one `sfx_catalog` (each entry tagged with
one of the six semantic categories), copies the wavs into the stage
directory, and `verify_delivery(...)` independently re-checks every staged
external wav's bytes and decoded PCM against the catalog before accepting the
delivery. Pass `include_external=False` to reproduce the Phase-2-only
9-procedural-asset catalog (used by v1-shape regression tests).

Note: in `catalog_profile="v2"`, the 7 trigger-role events (e.g.
`transition` -> the `whoosh` category) now resolve via
`select_asset(category, catalog, event_index=..., prefer="external")` over
the full catalog, so an external-licensed asset is chosen whenever one is
available for that role's category — `role_for_trigger`'s invariant in
`contract_registry.py` was relaxed from a fixed `(role, asset_id)` pin to a
`(role, category)` pin to allow this. Explicit `sfx_hints` (director-mode
content hints, e.g. a detected cash-register cue) are staged the same way as
`role="content_hint"` events, each resolving its own asset via
`select_asset(hint.category, catalog, ..., prefer="external")`. In
`catalog_profile="v1"` (schema_version 2) none of this applies: the 7
trigger-role events still resolve to their exact pinned procedural asset id
as before, `sfx_hints` are silently ignored, and the staged bytes are
unchanged.
