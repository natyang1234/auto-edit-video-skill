# Font security policy

Policy version: `font-security-policy/1`
Validator version: `font-security/1`
Receipt version: `font-validation-receipt/1`

Only the bytes accepted by `scripts/font_security.py::validate_font_bytes` may
be registered as a project font. The validator accepts standalone sfnt TTF
(`00010000`) and static CFF OTF (`OTTO`). It rejects TTC, WOFF, WOFF2, CFF2,
SVG glyphs, color glyph tables, embedded bitmap tables, duplicate or overlapping
tables, bad checksums, trailing data, and table ranges outside the file.

The byte envelope is closed: only the zero-filled 0–3 bytes required to align
the first table may occur between the directory and that table. After each
table's own zero padding, the next table must start immediately; inter-table
gaps are forbidden. The final padded table must end at EOF. Any non-zero gap is
rejected as `FONT_TABLE_GAP_NONZERO`; an oversized all-zero alignment gap or any
all-zero inter-table gap is rejected as `FONT_TABLE_GAP`.

## Bounds

- file: 32 MiB; tables: 64; individual table: 24 MiB;
- pre-table alignment gap: at most 3 zero bytes; inter-table gap: 0 bytes;
- glyphs: 60,000; cmap mappings: 200,000; cmap subtables: 32;
- name records: 256; encoded name string: 4 KiB; name table: 256 KiB;
- variation axes: 16; named instances: 64;
- one TrueType glyph: 10,000 points, 512 contours, 64 components, composite
  depth 16; all simple glyph points: 2,000,000;
- one instruction program or CFF charstring: 64 KiB; all CFF charstrings:
  8 MiB; local/global CFF subroutines: 4,096 each;
- NFC required text: 100,000 Unicode scalars.

The validator requires pinned fontTools `4.62.1`, constructs `TTFont` with `lazy=False`, then forces table
decompilation. It parses outlines for structural bounds but never rasterizes,
shapes, executes TrueType instructions, or resolves a system font.

## Glyph coverage

`required_text` is normalized to NFC before checking. Unicode separators,
characters for which `str.isspace()` is true, and `Cc`/`Cf` controls do not
require glyphs; each exclusion is listed in `ignored_characters`. Assigned,
non-control scalars require an explicit non-`.notdef` cmap mapping. A variation
selector requires an explicit `(base, selector)` cmap format 14 entry. Required
glyph records include the code point, deterministic cluster index, glyph name,
and variation mapping where applicable. Any missing scalar fails with
`FONT_REQUIRED_GLYPH_MISSING`; system fallback is never attempted.

## Rights and receipts

`license_spdx` is mandatory and exact-match allowlisted to `OFL-1.1`,
`Apache-2.0`, or `Ubuntu-font-1.0`. `OS/2.fsType` restricted or bitmap-only
embedding is rejected. The validator does not treat font name-table license
claims as evidence: the import/final gate must independently preserve and
hash-bind the actual license text and provenance.

The returned receipt binds the raw font SHA-256, container/MIME, byte length,
license identifier, normalized required-text hash, complete Unicode coverage
hash/count, actual required-glyph trace hash, Unicode database version, limits
hash, fontTools version, policy/validator/receipt versions, and the assertions that hinting was not
executed and system fallback is forbidden. Renderer and final gates must resolve
the project font by the exact raw SHA-256, not by family name.
