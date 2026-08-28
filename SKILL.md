---
name: ai-orchestra
description: >-
  Coordinate a Codex or Claude Code primary session, native subagents, and
  external AI providers through task-specific delegation, parallel execution,
  adversarial review, proof replay, usage accounting, quota visibility, and
  evidence-aware routing. Use for multi-AI collaboration, AI delegation,
  cross-provider checking, deep reviews, bulk work, or usage/quota reporting.
---

# ai-orchestra —— 多 AI 派工與反幻覺

一套零相依的工具組（Python 3.11+ 標準函式庫）。目前的 Codex 或 Claude Code
主任務是總指揮；native subagents 負責有界分工，外部 provider 負責跨供應商
specialist／critic。最終整合、權限決策與完成宣稱永遠留在主任務。

## Core contract

1. 派工前固定 objective、deliverables、限制、authoritative evidence 與 done checks。
2. 使用最小有效 roster；不要把同一個廣泛問題丟給所有模型投票。
3. 每個 agent 都要有 bounded role、input／output contract、read／write boundary。
4. 每個 external dispatch 都要有唯一 label、task category、timeout／retry／budget 邊界。
5. 獨立工作才平行；依賴工作循序執行。
6. 模型輸出是分析，不是證據；用檔案、指令、primary source、API 或 replay 查證。
7. 未明確授權時，delegated agents 保持唯讀；不得修改 production、寄信或擴權。
8. 一個跨供應商反方勝過多輪投票；總指揮不能兼任自己的獨立 critic。
9. 最終回報 roster、證據、失敗／fallback、未驗證項目與 task-specific ledger delta。

完整流程見 [docs/multi-agent-workflow.md](docs/multi-agent-workflow.md)；涉及事實或完成
判定時，先完整閱讀 [docs/anti-hallucination.md](docs/anti-hallucination.md)。

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

# Evidence/subscription/quota/reliability-aware recommendation (no dispatch):
python scripts/route.py --task implementation --coordinator codex --needs-tools
python scripts/route.py --task code_review --coordinator codex --role critic

# Adversarially cross-check a claim; --check-evidence runs the named proof
# (earns exit 0); --json for agents:
echo "CLAIM" | python scripts/verify.py --critics <different-provider> [--check-evidence] [--json]

# Verify a whole answer: split into atomic claims, check each:
cat answer.md | python scripts/verify.py --from-answer --splitter <p> --critics <different-provider>

# Proof-of-work / replay gate — prove a claim with a LOCAL check, not an opinion:
python scripts/prove.py --cmd "pytest -q" --expect-rc 0    # or --files / --url

# See spend / success rates:
python scripts/usage_report.py [--html]
```

- **平行工作：** 優先使用目前 runtime 的 native collaboration；只有外部
  cross-provider 工作才平行啟動 `dispatch.py`。相依工作不可平行。
- **Claude 轉接器的額外功能：** `--claude-profile review`（唯讀的獨立
  審查方，遵守 repo 的 `AGENTS.md`）、`--effort`、`--max-budget-usd`。
- **不要用同 provider 的第二個 CLI process 模擬獨立性。** Native subagents
  可增加覆蓋率，但 independent critic 必須來自不同模型家族。

## 選擇執行路徑

### Native collaboration 優先

Codex 主任務使用 Codex native subagents；Claude Code 主任務使用 Claude Code
subagents。把 repo inventory、UI／安全 audit、bounded implementation、測試等互不依賴
工作拆開，只傳該角色需要的 context。

### External provider

需要跨供應商 specialist／critic、或必須留下 dispatcher ledger 時，才使用
`dispatch.py`／Windows UTF-8 wrapper `dispatch.ps1`。外部 agent 預設唯讀，prompt
只帶最小 evidence packet，不得含 secret、credential、完整 env dump 或不必要個資。

### Single agent

任務小、緊密耦合、規格已清楚，或獨立觀點不會改變結果時，保持 single-agent。
不要為了看起來像 orchestra 而增加無效層級。

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

1. **Preflight** —— 從目前 skill 目錄執行 `dispatch.py --doctor`／`--list`；
   quota 影響路由時才跑 `quota_probe.py`。Ready 只是環境提示，不是登入或完成證明。
2. **寫 dispatch matrix** —— 為每個角色固定 scope、forbidden scope、inputs、
   output contract、done、write boundary、review method、timeout 與 retry policy。
3. **Route** —— 非簡單派工先跑 `route.py`；`executor` 可留在目前 coordinator，
   `critic` 必須排除 coordinator family。分數是 recommendation，不是 evidence。
4. **Execute** —— safe independent tasks 平行；相依任務循序。每個外部派工傳
   `--task` 與唯一 `--label`，大型審查只送最小 evidence packet。
5. **Challenge** —— 使用一個跨供應商 adversarial reviewer，要求 disconfirming case
   與可裁決證據；明確傳 `verify.py --critics <different-provider>`。
6. **Prove and replay** —— 重讀 changed files、跑測試、檢查最終外部狀態。
   `prove.py` 通過才支持完成宣稱；模型一致仍是 `UNVERIFIED`。
7. **Integrate** —— 主任務裁決衝突，回報 accepted／rejected／unresolved findings、
   測試、provider failures、fallback 與本次 ledger delta。
8. **Record outcome carefully** —— 只有 deterministic proof 或明確人類驗收後，
   才能用 `record_outcome.py` 記錄。現行 proof-label 尚未綁 artifact hash；label 必須唯一，
   高風險 release 不可只靠這個 metadata 作最終 gate。

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
