# SVG Threat Model 與 Sanitizer 契約

## 攻擊面（對應 `fixtures/svg_threat_corpus.json`）
script 標籤／event handler 屬性（on*）、`foreignObject`、外部資源引用
（image/use/href 指向 http(s)、@import）、data: URI 夾帶、XML entity expansion
（billion laughs）、DOCTYPE/外部 DTD、CSS 內 url() 外聯、超大 viewBox／path（DoS）。

## Sanitizer 規則（fail closed）
1. 解析：禁 DTD、禁 entity 展開、深度／節點數上限（10k nodes）、檔案大小上限（2MB）。
2. 元素 allowlist：svg/g/path/rect/circle/ellipse/line/polyline/polygon/text/tspan/defs/
   linearGradient/radialGradient/stop/clipPath/mask/use(僅 #local)/style(僅無外聯)。
   其餘（script/foreignObject/animate 系/iframe…）→拒收整檔，不做「清掉繼續」。
3. 屬性：拒任何 `on*`；href/xlink:href 僅允許 `#fragment`；style 內含 `url(` 非 #local→拒。
4. Rasterize 邊界：sanitize 通過後仍**只以 rasterized PNG 進 render 管線**（候選 resvg），
   SVG 原檔僅存查；瀏覽器 preview 同樣只給 raster。
5. 任何拒收記 `asset_provenance.review_status: rejected`＋原因。

## 測試
corpus 內 `expect:reject` 全部被拒、`expect:accept` 通過且 raster 成功，缺一即 FAIL。
