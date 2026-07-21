# Changelog

本專案所有重要變更皆記錄於此。
格式依循 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

源自用 ai-orchestra 自我審查（Grok 外部審查 + Claude 交叉查核）後的第一批改進。

### Added
- `dispatch.py --doctor` — 離線檢查每個已啟用供應商是否就緒（CLI 在 PATH、
  API key 是否設定、base_url 是否安全），解決冷啟動最大的卡點。
- 供應商設定新增 `adversary = true` 旗標，明確指定 `verify.py` 的預設對手，
  取代脆弱的 `role` 子字串比對（舊行為保留為後備）。
- `verify.py --json` — 給 agent／腳本用的機器可讀報告。
- `tests/test_verify.py` — 補上旗艦功能過去缺的測試（判定解析、退出碼矩陣、
  對手選擇），共 14 個測試。

### Changed
- `verify.py` 的對手改為**平行**執行（先前為序列，是純粹的延遲成本）。
- 退出碼決策抽成純函式 `synthesize()`，可測試、可被 `--json` 重用。

## [0.1.0] — 2026-07-21

首次開源發行。從一個個人的 Claude Code skill 通用化為任何人都能對接自己 AI 訂閱的工具。

### Added
- **設定驅動的供應商**（`config/providers.toml`）。可宣告任意數量的 AI 方案，分為兩種類型：`cli`（claude / codex / grok / gemini / generic）與 `openai`（任何 OpenAI 相容的 HTTP API — OpenAI、DeepSeek、Groq、OpenRouter、Mistral、xAI、NVIDIA NIM、本機 Ollama / LM Studio…）。新增供應商不需要寫任何程式碼。
- **`verify.py`** — 一種對抗式交叉查核，要求提供證據，並拒絕把單純的共識當成已驗證。實作了源自社群的抗幻覺協定。
- **`docs/anti-hallucination.md`** — 該協定，從一場公開討論中提煉而成，並附上致謝。
- 完整文件：供應商、額度／記帳、架構；雙語 README（EN + 繁中）。
- MIT 授權、`.gitignore`、貢獻指南。

### Changed
- `dispatch.py` 從原本寫死的 6 個供應商清單重構為設定驅動的調度器，同時保留了 Claude 轉接器的串流／逾時／行程樹／序列化機制。
- 額度腳本現在透過 `config.py` 解析其家目錄（尊重 `$AI_ORCHESTRA_HOME`），不再使用固定路徑。
- 即時額度探測現在被定位為選用的、盡力而為的功能，在無法使用時會退回到通用的記帳檔計數。

### Removed
- 個人資料與特定安裝環境的寫死內容（原作者的用量記帳檔、機器路徑，以及一個客製的桌面應用程式供應商）。
