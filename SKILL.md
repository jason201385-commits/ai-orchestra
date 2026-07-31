---
name: ai-orchestra
description: >-
  把你所有的 AI 訂閱當成一個樂團來指揮。由一位總指揮（Claude
  Code）將工作分派給你手上的每一個 AI 方案 —— Codex、Grok、Gemini，或任何
  OpenAI 相容的 API（OpenAI、DeepSeek、Groq、OpenRouter、NVIDIA NIM、本機的
  Ollama/LM Studio……）—— 全透過單一指令完成，每次調用都會記帳，並以一套
  對抗式交叉查核把「模型都同意了」視為還不夠好。
  當某項任務適合把工作拆分到多個模型時使用（研究、審查、
  批量生成、第二意見），當你想把粗活從稀缺的總指揮額度上移開時使用，
  或當你想透過接地 + 對抗式驗證來降低幻覺時使用。觸發詞：「multi-AI」、「dispatch to
  grok/codex/gemini/deepseek」、「cross-check」、「verify this claim」、「AI usage」、
  「how much quota left」。
---

# ai-orchestra —— 多 AI 派工與反幻覺

一套零相依的工具組（Python 3.11+ 標準函式庫），讓一位總指揮
可以把工作分派給你手上的每一個 AI 訂閱、為每次調用記帳，並以
對抗方式交叉查核宣稱。完整文件放在 `docs/`。

## 首次設定

```bash
cp providers.example.toml config/providers.toml   # then enable your providers
python scripts/dispatch.py --list                  # confirm they load
```

供應商在 `config/providers.toml` 中宣告 —— 參見
[docs/providers.md](docs/providers.md)。兩種類型幾乎涵蓋一切：
`cli`（claude/codex/grok/gemini/generic）與 `openai`（任何 OpenAI 相容的
端點）。API key 來自環境變數，絕不寫進設定檔。

## 核心指令

```bash
# Call one provider (prompt via stdin):
echo "PROMPT" | python scripts/dispatch.py <provider> [--label NAME] [--model M] [--timeout S]

# Windows PowerShell — use the UTF-8 wrapper so CJK survives the pipe:
'PROMPT' | & .\scripts\dispatch.ps1 <provider>

# Check which providers are installed / keyed / ready (offline):
python scripts/dispatch.py --doctor

# Adversarially cross-check a claim; --check-evidence runs the named proof
# (earns exit 0); --json for agents:
echo "CLAIM" | python scripts/verify.py --critics <a,b> [--check-evidence] [--json]

# Verify a whole answer: split into atomic claims, check each:
cat answer.md | python scripts/verify.py --from-answer --splitter <p> --critics <a,b>

# Proof-of-work / replay gate — prove a claim with a LOCAL check, not an opinion:
python scripts/prove.py --cmd "pytest -q" --expect-rc 0    # or --files / --url

# See spend / success rates:
python scripts/usage_report.py [--html]
```

- **平行工作：** 在背景啟動多個 `dispatch.py`（Claude Code
  Bash `run_in_background: true`）並收集結果。每次調用都會自行記帳。
- **Claude 轉接器的額外功能：** `--claude-profile review`（唯讀的獨立
  審查方，遵守 repo 的 `AGENTS.md`）、`--effort`、`--max-budget-usd`。
- **不要在 Claude Code 對話內部派工給 `claude`** —— 在那裡巢狀執行
  `claude -p` 會回傳 401。請改用 subagent/Workflow；只在單純的 shell 中
  才派工給 `claude`。

## 如何真正降低幻覺

閱讀 [docs/anti-hallucination.md](docs/anti-hallucination.md)。簡短版本，
來自啟發本工具的社群：

1. **把每個宣稱接地**到一個真實來源；要求引用／`file:line`。
2. **要求工作證明**，而非斷言 —— 一段 diff、一個測試、一段指令輸出。
3. **一個好的對手勝過五輪一致的附和** —— 把答案通過
   `verify.py`；它的職責是*反駁*，不是投票。
4. **以回放驗證完成度**，而不是靠模型自稱「done」。
5. **共識不等於真相。** 模型可能一起犯錯。`verify.py` 不會
   為一致但無來源的同意蓋橡皮圖章。

## 標準作業流程

1. **先檢查額度** —— `usage_report.py`；把工作從任何偏低的一家移開。
2. **拆分與分派** —— 把粗活（翻譯、批量草稿、摘要）
   送給便宜／免費的供應商；把總指揮留給需要判斷的部分。
3. **交叉盤問** —— 讓一個*不同的*供應商或 subagent 攻擊
   輸出；聚焦於事實、數字與來源。
4. **以證據定案** —— 修正或附上證明，然後重新驗證。
5. **檢視記帳檔** —— 確認多跑的那幾輪買到的是準確度，而不只是 token。

## 靜默失敗：`rc=0` 不是模型的工作證明

這是本專案付出最貴代價學到的一課，值得單獨一節。

**症狀**：dispatch 回 exit 0、記帳檔 `ok=true`、`prompt_chars` 跟你的來源檔完全吻合，
但模型回覆的是「請貼上完整內容／你的訊息中沒有出現…」。換一家供應商、同樣的 prompt 卻正常。

**根因**：在 Windows 上，npm 安裝的 CLI（`codex`、`gemini` 等）是 `.cmd` 包裝器。
CreateProcess 看到 `.cmd` 不會直接執行它，而是交給 `cmd.exe`；
**`cmd.exe` 的解析器把換行視為命令結束**，`\n` 之後的內容整段丟掉，
不報錯、子行程照樣 exit 0。所以把多行 prompt 當命令列參數傳，模型只會收到第一行。

實測（用一個會傾印 argv 的假 `.cmd` 包裝器量的）：

| 傳法 | 送出 | 子行程實際收到 |
|---|---|---|
| argv，多行 | 87 字元 / 4 行 | **17 字元，只剩第 1 行** |
| argv，單行 | 95 字元 | 95 字元完整（`% & \| ^ < >` 都沒事） |
| stdin | 全長 | 完整 |

不是編碼問題、不是 emoji、不是長度、不是指示順序 —— 就是換行 × `.cmd`。

**因應（已內建）**：

1. `codex` 與 `gemini` 轉接器一律把 prompt 送 **stdin**，不放 argv。
2. `run_cmd()` / `run_streaming_json_cmd()` 前置 `argv_newline_truncation_risk()`：
   只要執行檔是 `.cmd`／`.bat` 且任一參數含換行，直接回 `rc=-3` 明確報錯，
   **不讓它靜默截斷**。任何新增的轉接器都自動受保護。
3. 成功判定加上 `detect_empty_input_reply()`：送出 ≥200 字元卻收到 ≤400 字元、
   且內容命中「請貼上／沒有收到／appears empty／didn't receive…」等樣式時，
   判為傳輸失敗（`ok=false`、`err_kind=empty_input_reply`、exit 1），
   回覆仍印到 stdout 供人工判讀。三個門檻同時成立才觸發，避免誤殺
   「真的在請你補資料」的長答案。

**通則**：`dispatch.py --doctor` 只驗 CLI 在不在 PATH，驗不出傳輸層問題。
懷疑某家「有回但像沒收到」時，先查它解析到什麼 ——
`python -c "from shutil import which; print(which('codex'))"`，
結尾是 `.CMD`／`.BAT` 就是高風險。

**同一族的第三種**：把背景派工的輸出接 `| tail -N` 也會靜默截斷，
而且記帳檔只存 metadata、不留全文，截掉就救不回來。背景派工一律導向檔案，
跑完再讀。**凡是「內容經過某個東西」的地方，都要有辦法驗證它有沒有全部通過。**

回歸測試：`tests/test_dispatch_transport.py`。

## 反模式

- ❌ 對每個模型問同一個問題（浪費額度）—— 價值在於
  *分工*與*獨立對抗式*查核，而不是齊聲合唱。
- ❌ 相信任何單一模型的「事實」—— 把宣稱送去驗證。
- ❌ 忘了記帳 —— 一律走 `dispatch.py`，讓儀表板保持誠實。
- ❌ 對一個清楚的 prompt + 一個有能力的模型就能搞定的任務過度編排。
- ❌ 新增轉接器時把多行 prompt 塞進 argv —— Windows 的 `.cmd` 包裝器會靜默
  截斷（見上節），一律走 stdin。
- ❌ 拿 `rc=0` 當「模型收到了」—— 那只證明行程結束；傳輸層壞掉一樣是 exit 0。

## 設定

- `config/providers.toml` —— 你的供應商（已 gitignore；key 留在環境變數）。
- `data/` —— 記帳檔、額度快取、儀表板（已 gitignore）。
- `$AI_ORCHESTRA_HOME` —— 覆寫 config/data 的位置（預設為 repo 根目錄）。
