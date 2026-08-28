# 多 AI agents 的標準作法

`ai-orchestra` 的目的不是讓模型投票，而是讓不同角色各自處理最適合的工作，最後用證據裁決。

核心流程：

```text
任務規格
  → 安全與權限硬篩選
  → 有界分工
  → provider / model 路由
  → 一個跨供應商反方
  → deterministic proof / replay
  → 記錄 proof／human-backed outcome（routing prior）
  → 更新未來路由依據
```

## 角色分工

| 角色 | 責任 | 不可做的事 |
|---|---|---|
| 目前主任務（Codex 或 Claude Code） | 拆解、權限判斷、整合、最終交付 | 不把自己的第二個 process 冒充獨立查核 |
| Native subagent | repo 盤點、獨立子題、UI／安全／測試等有界工作 | 未授權時不可修改外部系統或擴大讀寫範圍 |
| External provider | 跨供應商 specialist 或 critic | 模型輸出不可直接當事實或完成證明 |
| Deterministic gate | 測試、檔案、URL、browser replay 等可重跑檢查 | 不用「模型很有信心」代替檢查結果 |
| 人類 | 核准高影響副作用、必要的主觀驗收 | 不被模糊的 done 宣稱取代 |

同一家模型家族的 native subagent 可以增加覆蓋率，但不算跨供應商獨立查核。

## 1. 先寫任務契約

派工前先固定以下欄位：

- objective：真正要解決的問題
- deliverables：要產出的檔案、答案或外部狀態
- authoritative evidence：哪些檔案、primary source、API 或畫面可裁決
- done checks：哪些測試或 replay 通過才算完成
- data classification：可否送給外部 provider
- side effects：唯讀、本機寫入、GitHub push、部署、寄信等級
- allowed roots / tools / network：agent 可接觸的邊界
- timeout / retry / budget：最長時間、重試規則與成本上限

沒有這份契約時，多一個 agent 通常只會放大歧義。

## 2. Preflight 與路由

先確認 dispatcher 看得到哪些 provider；`--doctor` 只證明 CLI 或環境設定看起來就緒，不證明已登入，也不證明這次一定成功。

```powershell
python .\scripts\dispatch.py --doctor
python .\scripts\dispatch.py --list
python .\scripts\quota_probe.py --only codex claude --json
```

需要路由時，使用 evidence-aware router。它綜合：

- task-specific capability prior
- subscription / billing mode
- 新鮮的 quota 狀態；不知道就保持 unknown
- 本機近期 dispatch 成功率與錯誤類型
- 通過 proof 或人類明確驗收的 outcome

```powershell
python .\scripts\route.py --task implementation --coordinator codex --needs-tools
python .\scripts\route.py --task code_review --coordinator codex --role critic
python .\scripts\route.py --task bulk_text --coordinator claude --mode economy --json
```

路由分數是派工建議，不是 provider 可用、結果正確或任務完成的證明。能力來源與更新方式見 [model-routing-evidence.md](model-routing-evidence.md)。

## 3. 建立 dispatch matrix

每一個 agent 都要有不同、可驗收的任務，不要把同一個廣泛問題複製給所有模型。

| 欄位 | 範例 |
|---|---|
| agent / provider | native repo-inventory subagent |
| role | 找出受影響檔案與現有測試 |
| input boundary | 只讀 `src/`、`tests/`、`AGENTS.md` |
| output contract | 風險清單與精確 `file:line` |
| write boundary | read-only |
| done | 找到入口、測試與相容性風險 |
| reviewer / proof | 主任務重讀檔案並跑測試 |
| timeout / retry | 120 秒；失敗只重試一次或改走 fallback |

適合平行的工作：互不依賴的 repo 盤點、UI audit、安全審查、來源查核。依賴前一步輸出的工作必須循序執行。

## 4. 優先用目前產品的 native collaboration

Codex 主任務優先使用 Codex native subagents；Claude Code 主任務優先使用 Claude Code subagents。它們適合快速、有界的平行工作，也能保留目前 task 的協調狀態。

只有需要跨供應商 specialist／critic、或要留下統一 ledger 時，才使用 dispatcher：

```powershell
@'
Read only. Find one blocking flaw or disconfirming case.
Name the exact evidence that would settle it. Do not modify files.
'@ | & .\scripts\dispatch.ps1 claude `
  -Task code_review `
  -Label 'release-critic-20260828' `
  -Timeout 420
```

每次 dispatch 使用唯一 label，並用 `--task`／`-Task` 留下穩定任務分類。外部 prompt 只附最小證據包，不放 secret、credential、完整環境 dump 或不必要的個資。

## 5. 一個反方，不做模型投票

Builder 完成後，找一個不同 provider 的 critic，要求它：

1. 找出會推翻目前結論的案例。
2. 指出精確檔案、來源或命令。
3. 說明什麼證據能一槌定音。
4. 保持唯讀，不直接改 production 或外部系統。

```powershell
Get-Content .\answer.md -Raw |
  python .\scripts\verify.py --from-answer --critics claude --check-evidence
```

多個模型一致仍是 `UNVERIFIED`。只有實際檢查證據才能通過；反方只是幫忙找到應該檢查什麼。

## 6. Replay 才能宣稱完成

對所有副作用重讀最終狀態，並跑能重現的 gate：

```powershell
python .\scripts\prove.py --cmd "python -m unittest discover -s tests" --expect-rc 0
python .\scripts\prove.py --files .\README.md .\docs\multi-agent-workflow.md
```

常見證據邊界：

- `git diff` 證明內容改了，但不證明測試通過。
- 測試通過證明本機行為，但不證明 GitHub 已 push。
- `git push` exit 0 加上遠端 commit SHA，才證明 GitHub 狀態已更新。
- HTTP 200 只證明可連線，不證明頁面內容、互動或部署版本正確。
- `--doctor`／`--list` 只做 preflight，不代表某個 provider 真的完成了審查。

## 7. 只記錄有依據的 outcome

Router 可以學習 Jason 自己的實際工作結果，但只能餵已驗證 outcome：

```powershell
python .\scripts\record_outcome.py --provider claude `
  --model sonnet --task code_review --verdict pass `
  --label release-critic-20260828 `
  --proof-label release-tests-20260828 `
  --evidence "tests and final diff replayed"
```

`pass` 必須綁定成功的 `prove` ledger label，或在使用者已明確驗收時使用 `--human-accepted`。Provider 自評不能寫成 pass。`partial`／`fail` 也要照實記錄，避免 router 只看到成功樣本。

## 8. 最終報告

交付時至少說清楚：

- 使用過哪些 agents／providers，以及各自的有界角色
- 哪些意見被接受、拒絕或仍未解決，理由與證據
- 實際跑過哪些測試與 replay
- provider 失敗、fallback 與尚未驗證項目
- 這次 task 的 ledger delta；歷史總量要分開標示

最終判斷順序固定為：**權限與安全邊界 → authoritative evidence → deterministic proof → 必要的人類驗收 → 模型意見**。

## 失敗處理

- 缺 CLI／未登入：標示 unavailable；不自行安裝或改 credential。
- quota／rate limit：記錄錯誤後選可接受的 fallback；unknown 不等於零或無限。
- timeout／空輸出：視為失敗，不把 partial output 重命名為成功。
- critic 衝突：回到能裁決的檔案、primary source、命令或實際系統狀態。
- 外部 provider 需要讀檔但工具面不穩：改送 inline minimal evidence packet。
- production、寄信、付款、部署、刪除：即使 agent 建議，仍需要當下的明確授權。
