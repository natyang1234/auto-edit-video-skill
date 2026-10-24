# Visual asset providers (Openverse, fonts, SVG)

Moved verbatim from SKILL.md section 6.

The local Studio searches Openverse/Wikimedia still-image metadata after the project's explicit network-disclosure consent. Explicit import of allowlisted `CC0-1.0`/`CC-BY-4.0` stores a private copy, source/creator/license/hash, regenerates `ATTRIBUTION.md`, and binds it to the final rights gate; evidence is not legal advice.

Font search is metadata-only: Google Fonts uses the pinned-commit grammar `(ofl|apache|ufl)/family`, `github-fonts` an allowlisted font id (contracts/github_fonts.json, blob-SHA pinned), Fontsource exact `id@x.y.z` semver. Explicit import strictly validates and stores private TTF/OTF, license, and SPDX evidence. Exact `font_asset_id` coverage of rendered glyphs is required: missing/tampered/uncovered fonts block preview/final; `AUTO_EDIT_FONT` is an explicitly unverified legacy fallback only without a project font ID.

SVG search shows Heroicons/Lucide/Tabler/Wikimedia metadata; raw SVG never enters DOM or timeline, only strictly sanitized bounded rasterized PNG. Production never trusts `PATH`: after installing `resvg`, explicitly run `python3 scripts/configure_resvg.py --resvg <canonical-absolute-path>` from the skill directory to create the owner-only manifest and reviewed deny-network sandbox profile. Missing, drifted, or unsafe configuration labels SVG import unavailable and fails closed. Tests use fake transport/rasterizer, never live provider search. Arbitrary URLs, GIF, and B-roll provider retrieval remain unsupported; licensed local uploads work.
