# Changelog

本專案所有重要變更皆記錄於此。
格式依循 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

源自用 ai-orchestra 自我審查（Grok 外部審查 + Claude 交叉查核）後的第一批改進。

### Fixed

- **Windows 上多行 prompt 會被靜默截斷（嚴重）** —— npm 安裝的 CLI 是 `.cmd`
  包裝器，CreateProcess 會轉交 `cmd.exe`，而 `cmd.exe` 把換行當命令結束。
  結果是模型只收到 prompt 的第一行、回覆「請貼上完整內容」，但 dispatch 仍
  exit 0、記帳檔記 `ok=true`，**完全沒有徵兆**。實測 87 字元／4 行的 prompt
  只有 17 字元送達。
  - `codex` 轉接器改走 stdin（`codex exec --json -`）。
  - `gemini` 轉接器改走 stdin（`gemini --skip-trust -p ""` + prompt 走 stdin）。
    ⚠️ 依據是讀 gemini-cli 0.42.0 的原始碼（空的 `-p` 是 falsy，stdin 內容原封
    不動成為 prompt），並用假的 `.cmd` 包裝器實測傳輸位元組相同；
    但**尚未跑過一次真實模型回合**驗證。
  - 新增 `argv_newline_truncation_risk()`，在 `run_cmd()` /
    `run_streaming_json_cmd()` 前置檢查：`.cmd`／`.bat` + 參數含換行 → 回 `rc=-3`
    明確報錯。往後新增的轉接器自動受保護。
- **成功判定太寬鬆** —— `rc == 0 and text` 只證明行程正常結束，不證明模型收到
  內容。新增 `detect_empty_input_reply()`：大 prompt 換來一個只在抱怨沒收到
  輸入的短回覆時，記成 `ok=false`／`err_kind=empty_input_reply`／exit 1，
  回覆仍印到 stdout 供人工判讀。三個門檻同時成立才觸發，避免誤殺「真的在請你
  補資料」的長答案。
- `classify_ledger_err()` 新增 `prompt_truncated` 與 `empty_input_reply` 兩個
  分類桶，讓儀表板的「失敗原因」看得出根因，而不是落進「其他（未分類）」。

### Added
- **`tests/test_dispatch_transport.py`** — 15 個回歸測試，釘住 codex／gemini 的
  stdin 傳輸契約、`.cmd` 包裝器守門、以及「不要誤殺正常長答案」的邊界。
- **`agy` 轉接器**（Antigravity CLI，Google OAuth／Gemini 家族）。它是 agentic
  CLI 會主動讀工作目錄，所以固定在空沙盒目錄執行；`providers.example.toml`
  已附範例區塊與兩個注意事項。
- **派工前的額度閘門**：主窗口用量過高時當場擋下派工（`--ignore-quota` 可逃生），
  以及 Claude Code session 內誤派 `claude` 的守門（`--allow-claude-in-session`
  可逃生）—— 後者在對話內會 401 並與當前 session 搶佔。
- **`prove.py`** — 回放／工作證明閘門：用本機檢查（跑指令＋比對 rc/輸出、
  檔案存在且非空、檔案含某字串、URL 回 2xx）證明宣稱，而不是再問一個模型。
  check 函式可被匯入；每次執行記進 ledger（provider `prove`）。
- **`verify.py --check-evidence`** — 實際去跑每個對手指名的 `EVIDENCE_SPEC`
  （檔案／URL；加 `--run-commands` 才執行指令）。**這是唯一能掙到 exit 0 的路**：
  全數支持 ＋ 證據檢查通過才回 0（VERIFIED）；證據沒過回 2。單靠共識永遠不是 0。
- **`verify.py --from-answer`** — 把一整段長答案（研究／設計／bug 分析）用
  `--splitter` 拆成原子事實宣稱，逐條對抗式查核再彙總；任一條被駁斥即整體
  DO_NOT_SHIP，並把該修的宣稱列最前面。真實用途是查 agent 剛寫的整段，不是一句話。
- `dispatch.py --doctor` — 離線檢查每個已啟用供應商是否就緒（CLI 在 PATH、
  API key 是否設定、base_url 是否安全），解決冷啟動最大的卡點。
- 供應商設定新增 `adversary = true` 旗標，明確指定 `verify.py` 的預設對手，
  取代脆弱的 `role` 子字串比對（舊行為保留為後備）。
- `verify.py --json` — 給 agent／腳本用的機器可讀報告。
- `tests/test_verify.py`（含證據迴路）與 `tests/test_prove.py` — 補上旗艦功能
  過去缺的測試（判定解析、退出碼矩陣、證據掙 exit 0、對手選擇、prove 原語）。

### Changed
- `verify.py` 的對手改為**平行**執行（先前為序列，是純粹的延遲成本）。
- 退出碼決策抽成純函式 `synthesize()`，可測試、可被 `--json` 重用。
- **退出碼語意**：exit 0 從「永不出現」改為「唯有 `--check-evidence` 且證據
  檢查通過才出現」——回應「共識不是證據，但證明是」。

### Fixed
- `prove.py` / `verify.py` 不再於 import 時覆寫 `sys.stdout/stderr`（改成只在
  `main()` 內冪等設定），修掉互相 import 造成的雙重包裹與關閉期 I/O 錯誤。

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
