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

# Adversarially cross-check a claim (anti-hallucination); --json for agents:
echo "CLAIM" | python scripts/verify.py --critics <a,b> [--json]

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

## 反模式

- ❌ 對每個模型問同一個問題（浪費額度）—— 價值在於
  *分工*與*獨立對抗式*查核，而不是齊聲合唱。
- ❌ 相信任何單一模型的「事實」—— 把宣稱送去驗證。
- ❌ 忘了記帳 —— 一律走 `dispatch.py`，讓儀表板保持誠實。
- ❌ 對一個清楚的 prompt + 一個有能力的模型就能搞定的任務過度編排。

## 設定

- `config/providers.toml` —— 你的供應商（已 gitignore；key 留在環境變數）。
- `data/` —— 記帳檔、額度快取、儀表板（已 gitignore）。
- `$AI_ORCHESTRA_HOME` —— 覆寫 config/data 的位置（預設為 repo 根目錄）。
