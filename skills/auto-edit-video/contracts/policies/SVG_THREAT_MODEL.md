# SVG Threat Model 與 Sanitizer 契約（v2）

遠端 SVG、provider metadata、檔名與 license claim 一律視為 hostile。同 UID project
filesystem 是既有 trusted boundary；未簽章 receipt 只提供 stale／consistency 防護，不宣稱能抵抗
同 UID 偽造。raw SVG 與 sanitized SVG 永不注入 Studio DOM、永不成為 editor source；timeline
只接受本契約產生且驗證完成的 bounded PNG。

## 固定安全管線

```text
rasterizer preflight（失敗時不得觸網）
→ allowlisted provider／license／brand gate
→ hardened HTTPS download
→ raw container／UTF-8 lexical gate
→ bounded hostile XML parse
→ new allowlist typed tree（不保留／序列化來源 DOM）
→ ID／reference graph 驗證與 deterministic rewrite
→ canonical SVG
→ isolated pinned rasterizer
→ strict PNG validation
→ PNG-only asset／timeline publication
```

任何階段失敗都拒絕整份，不做「刪掉危險節點後繼續」。拒絕只能回 stable error code，
不得反射 hostile XML、query、token、URL 或 provider echo。失敗不得留下可見 PNG、approved
provenance、timeline item 或 `.part`。

## Parser 與 complexity limits

- raw：2 MiB，strict UTF-8；拒絕 SVGZ/gzip、archive、NUL、非 UTF-8。
- XML：拒絕 DTD、ENTITY、非 XML declaration processing instruction、comment、CDATA、
  XInclude、XSLT、foreign/mixed namespace。
- depth 32、elements 5,000、attributes 20,000、decoded text 64 KiB。
- path data總計1 MiB、單 path 256 KiB、expanded path commands 20,000；path grammar必須
  完整 token/arity/arc-flag 消費，不可只數 command letters。
- raster單邊4096 px、總像素16 MiP、PNG 64 MiB、reference chain最多8 edges。

## Allowlist

元素只允許：`svg`、`g`、`defs`、`path`、`rect`、`circle`、`ellipse`、`line`、
`polyline`、`polygon`、`linearGradient`、`radialGradient`、`stop`、`clipPath`、
`title`、`desc`。

屬性採 per-element allowlist，只含幾何、有限 paint/stroke、opacity、transform、viewBox、
gradient、clip-path 與 `id`。全面拒絕：

- `script`、`foreignObject`、`iframe`、`object`、`embed`、`image`、`a`。
- `style`、`class`、CSS、任何 `on*` event handler。
- `href`／`xlink:href`，即使值是 `#local`。
- `text`／`tspan`、`mask`、`use`、`symbol`、`animate*`、`set`、filter／`fe*`、
  `pattern`、`marker` 與未知元素。
- HTTP、HTTPS、file、javascript、data、blob、相對 URL。

Fragment reference只允許完整匹配的 `fill="url(#id)"`、`stroke="url(#id)"` 與
`clip-path="url(#id)"`。paint target只能是 gradient，clip target只能是 `clipPath`。
duplicate ID、unresolved/wrong-type target、cycle、超過8層全部拒絕。

## Canonicalization 與 identity

Sanitizer以 typed values重建新 tree；固定 SVG namespace、element order、attribute order、
數字格式、paint格式與 whitespace。所有 ID依 depth-first traversal改名為 `s0001...`，只重寫
已驗證 reference。`sanitized_sha256` 是 canonical bytes hash；sanitize cache key另綁 raw hash、
policy version、sanitizer version與 limits hash。Raster receipt後續還必須綁 rasterizer binary／
version、sandbox profile、requested dimensions與 PNG hash；任一 identity變更使 approval stale。

## Rasterizer 與 PNG

Production只接受 manifest指定的 absolute、non-symlink regular executable，且 binary/version、
sandbox executable/profile hashes全匹配。無可靠 local rasterizer或preflight失敗時 provider必須
顯示 unavailable，search/import不得觸網；禁止回退瀏覽器、QuickLook、ImageMagick或未釘版
FFmpeg。本機目前沒有可見 SVG/librsvg FFmpeg decoder，也沒有 resvg，因此預期 fail closed。

PNG validator必須驗 signature、chunk framing/order、CRC、IHDR exact dimensions、8-bit RGB/RGBA、
non-interlaced、bounded zlib exact scanlines、filter bytes、critical chunks與 trailing bytes。

## Provider rule

Heroicons僅固定 MIT source；Lucide 的 ISC 未進 license policy前必須 unavailable；Tabler
`brand-*` 與 brand/logo/trademark metadata全部拒絕；Wikimedia逐檔 license evidence通過才可用。

## 測試

`fixtures/svg_threat_corpus.json` 每個 reject case都必須得到宣告的 stable code；accept case必須
canonicalize。Hostile case的 rasterizer call count必須為0。大型 byte/node/path bomb由測試
deterministic生成，不把巨大 payload提交到 corpus。
