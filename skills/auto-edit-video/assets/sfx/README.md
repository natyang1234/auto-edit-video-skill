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
