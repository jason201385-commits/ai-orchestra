# 貢獻指南

感謝你的協助。`ai-orchestra` 刻意維持小巧且零相依 —— 貢獻內容應維持這個原則。

## 基本規則

- **只用標準函式庫。** 不引入任何第三方執行期相依。如果某個功能需要套件，它大概應該獨立成一個明確標示的可選腳本。
- **機密絕不落地留存。** 金鑰來自環境變數。絕不把金鑰（或其任何片段）寫進設定、log、記帳檔或任何提交進版控的檔案。所有可能夾帶機密的輸出，都必須經過 `quota_common.redact()`。
- **誠實優先於完整。** 不要用貌似合理的預設值去掩蓋未知。如果某個值無法驗證，就把它呈現為「unknown」並說明該如何修正。
- **跨平台。** `dispatch.py`、`verify.py` 與 `config.py` 必須能在 Windows、macOS 與 Linux 上執行。平台專屬的功能（例如即時額度探測）在其他平台上必須優雅降級。

## 適合新手的 issue

- **將儀表板在地化（localize）。** `usage_report.py` / `quota_probe.py` 的標籤目前是中文／雙語 —— 非常歡迎做一次乾淨的 i18n（或改成英文預設）。
- **更多經過驗證的供應商配方**，用於 `providers.example.toml` 以及 [docs/providers.md](docs/providers.md)。
- **為其他供應商寫一個即時額度探測**，遵循 `quota_common.py` 裡的誠實規則（標示來源、評定新鮮度、絕不猜測）。

## 新增供應商轉接器

大多數供應商**不需要寫任何程式碼** —— 一個 `type = "openai"` 區塊就能涵蓋所有 OpenAI 相容的 API。只有在遇到真正不同的 CLI 時才新增程式碼：

1. 撰寫 `call_yourcli(spec, prompt, model, timeout)`，回傳 `(rc, text, err, duration, usage, rate)`。
2. 在 `scripts/dispatch.py` 的 `CLI_ADAPTERS` 中註冊它。
3. 從 `spec["command"]` 讀取命令名稱（不要寫死）。
4. 在 `providers.example.toml` 加一個範例區塊，並在供應商文件中加一列。

## 執行檢查

```bash
python scripts/config.py                 # config resolves, providers parse
python scripts/quota_common.py           # freshness + redaction self-check
python scripts/dispatch.py --list        # providers enumerate
python tests/test_quota_display.py       # dashboard/display invariants
python tests/test_dispatch_stream.py     # streaming-timeout state machine
```

請在開 PR 前執行這些檢查，並說明你如何測試任何供應商轉接器（它們會打真實服務，所以我們無法在 CI 中執行）。

## 提交風格

小而聚焦的提交，訊息要清楚。解釋*為什麼*，而不只是*做了什麼*。
