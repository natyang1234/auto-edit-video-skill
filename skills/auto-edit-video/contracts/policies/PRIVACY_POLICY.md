# Privacy Policy（最小揭露）

1. **Frames 不出本機**：全片抽樣 frames、OCR、人物畫面一律本機處理；不做人物身分辨識。
2. **對外查詢最小化**：provider 搜尋只送短關鍵詞（≤6 詞），不送 transcript 原文、
   不送人名／品牌（除非使用者明示）、不送任何 frame。
3. **Provider consent 粒度**：per-project per-provider-kind 明示同意
   （`provider_interface.consent_required`）；未同意的 provider 直接 skip，不 prompt 疲勞轟炸。
4. **語意分析**：agent-managed 模式下 transcript 會進入 agent context——這是使用者啟動
   agent 時的既有信任邊界；loopback Ollama 模式 transcript 不出本機。
5. **Telemetry：無**。本工具不回傳任何使用資料。
6. 憑證／token 永不寫入任何 artifact、receipt、log。
