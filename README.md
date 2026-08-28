# ai-orchestra 🎻

**把你所有的 AI 訂閱當成一個樂團來指揮 —— 並且不讓它們集體產生幻覺。**

`ai-orchestra` 是一套小巧、零相依的工具組，讓目前的主任務（Codex 或 Claude Code）
擔任總指揮，把工作分派給 native subagents 與你已經在付費的 AI 方案 —— Codex/ChatGPT、Grok、Gemini，
或任何 OpenAI 相容 API（OpenAI、DeepSeek、Groq、OpenRouter、Mistral、
NVIDIA NIM、本地的 Ollama/LM Studio……）—— 全部透過**一個指令**，
**每一次調用都記帳**，並內建一個**對抗式交叉查核**，把「模型都同意了」
當成*還不夠好*。

它起初只是 [Claude Code](https://claude.com/claude-code) 的一個個人 skill，
後來從一則[公開討論串](https://www.threads.com/@jasonchiou2016/post/Da-bv1Xk4Wj)
成長茁壯 —— 那串底下有幾十位實戰者在爭論到底該怎麼真正打敗幻覺。
他們的結論塑造了整個設計 —— 詳見
[docs/anti-hallucination.md](docs/anti-hallucination.md)。

> **English summary at the bottom ↓ ([jump to English](#english))**

---

## 為什麼

- **一個介面，涵蓋所有方案。** 你付費的訂閱都是閒置產能。在一份設定檔裡宣告一次；
  之後用同一種方式呼叫其中任何一個。
- **把稀缺的預算花在刀口上。** 如果你的主力座位是 Claude Code，那麼 *Claude
  token 才是稀缺資源*。把粗活（翻譯、批量草稿、摘要）路由到更便宜或免費的供應商，
  總指揮則留給判斷。
- **誠實記帳。** 每一次調用都在本地記錄（耗時、token、成功/失敗）。沒有任何儀表板
  會騙你：如果某個數字無法驗證，它會標示「未知」，而不是給你一個看起來很篤定的 0。
- **反幻覺是設計的一部分。** 一個「單一對手」的 `verify.py` 交叉查核，要求證據，
  拒絕替「只是達成共識」蓋橡皮圖章。
- **有界分工，而不是模型投票。** Native subagents 處理可平行的 repo 工作；跨供應商
  critic 專門找反例；最終由測試、檔案、URL 或 browser replay 裁決。

**零相依。** 純 Python 3.11+ 標準庫。沒有任何東西需要 `pip install`。

---

## 安裝（60 秒）

```bash
git clone https://github.com/jason201385-commits/ai-orchestra.git
cd ai-orchestra

# 1. declare the subscriptions you actually have
cp providers.example.toml config/providers.toml
#    edit config/providers.toml — enable your providers, set models

# 2. sanity check — 列出供應商，並檢查哪些「準備好了」
python scripts/dispatch.py --list
python scripts/dispatch.py --doctor     # CLI 在 PATH？key 有設？base_url 安全？
```

需要 **Python 3.11+**（因為要用標準庫的 `tomllib`）。若使用 CLI 型供應商，
請安裝並登入對應的 CLI（`claude`、`codex`、`grok`、`gemini`）。若使用 API 型
供應商，請在設定檔指定的環境變數（例如 `OPENAI_API_KEY`）中設好 API key。

---

## 使用

### 標準 multi-agent workflow

完整作法見 [docs/multi-agent-workflow.md](docs/multi-agent-workflow.md)。最短版本是：

1. 先固定 objective、deliverables、權限邊界與 done checks。
2. 可平行的 repo 子題優先交給目前產品的 native subagents。
3. 需要外部 specialist／critic 時才走 dispatcher，且每一個 agent 都有不同的有界角色。
4. 用一個跨供應商反方找破綻，不做多數決。
5. 用 `prove.py` 與最終狀態 replay 驗收；模型自稱完成不算證據。

需要 provider 建議時，可使用 evidence-aware router：

```bash
python scripts/route.py --task implementation --coordinator codex --needs-tools
python scripts/route.py --task code_review --coordinator codex --role critic
```

Router 只做建議；它不會自行 dispatch，也不能證明 provider 已登入或任務已完成。

### 呼叫單一供應商

```bash
# CLI providers (already logged in):
echo "Summarize the tradeoffs of optimistic locking" | python scripts/dispatch.py codex

# OpenAI-compatible APIs (key from env):
echo "Translate to French: good morning" | python scripts/dispatch.py deepseek

# pick a model / label / timeout inline:
echo "..." | python scripts/dispatch.py openrouter --model anthropic/claude-sonnet-5 --label triage
```

在 **Windows PowerShell** 上，請透過 UTF-8 wrapper 傳送，讓非 ASCII 的提示詞
在管線裡不會壞掉：

```powershell
'把這段翻成英文：早安' | & .\scripts\dispatch.ps1 deepseek
```

### 平行工作

優先使用目前 Codex／Claude Code 的 native collaboration；只有跨供應商工作才從 shell
平行啟動 `dispatch.py`。獨立工作才可平行，依賴前一步輸出的任務必須循序執行。
每次 external dispatch 都要有唯一 label、穩定的 `--task` 分類、最小 evidence packet
與明確 timeout／retry 規則。

### 交叉查核一個宣稱（反幻覺的部分）

挑一個已啟用、而且與總指揮不同模型家族的供應商當對手（審查方）：

```bash
echo "Postgres SERIALIZABLE isolation is implemented with two-phase locking" | \
  python scripts/verify.py --critics <different-provider>
```

每個對手（審查方）都被要求去**駁斥**這個宣稱，並指出那唯一一項能一槌定音的證據。
因為這個宣稱是假的（Postgres 用的是可序列化快照隔離），一個正常運作的評審團會駁斥它：

```
Tally: 0 supported · 1 refuted · 0 unsupported · 0 uncertain · 0 error
RESULT (exit 2): DO NOT SHIP AS-IS — a critic refuted the claim...
```

退出碼替判定背書。單靠模型一致**永遠拿不到 exit 0**——即使全數支持，也只回
非零的「UNVERIFIED — 共識不是證據」碼，因為模型彼此同意只是假設，不是驗證。

**exit 0 要用證據掙**：加上 `--check-evidence`，verify.py 會實際去跑每個對手
指名的 `EVIDENCE_SPEC`（檔案／URL；加 `--run-commands` 才會執行指令），
**只有那個證據檢查真的通過**才回 exit 0（VERIFIED），否則證據沒過就回 exit 2。
底層用 [`prove.py`](scripts/prove.py)（回放／工作證明閘門），也可獨立使用：

```bash
python scripts/prove.py --cmd "pytest -q" --expect-rc 0   # 跑測試當證明
python scripts/prove.py --files src/a.py src/b.py         # 檔案存在且非空
```

真實情況通常不是查一句話，而是查 agent 剛寫的**一整段**。用 `--from-answer`
先把長答案拆成原子宣稱，逐條查核，任何一條被駁斥就整體 DO_NOT_SHIP：

```bash
cat answer.md | python scripts/verify.py --from-answer --splitter codex --critics grok
```

> 請明確傳 `--critics`。設定檔不知道目前總指揮是 Codex 或 Claude Code，無法可靠判斷
> 跨供應商獨立性；同一模型家族的第二個 process 只能增加覆蓋率，不算獨立驗證。

### 看看你花了多少

```bash
python scripts/usage_report.py            # terminal table from the local ledger
python scripts/usage_report.py --html     # writes data/dashboard.html
```

記帳檔（`data/ledger.jsonl`）是唯一真相來源，且對**每一個**供應商都有效。
一個選用、盡力而為的額度探測器，能在 Windows 上為少數幾個供應商顯示即時剩餘用量 ——
見 [docs/quota.md](docs/quota.md)。在其他所有地方，它會優雅地退回到本地計數。

---

## 加入任何供應商

兩種類型基本上就涵蓋了市場上每一個 AI 方案。完整指南：
[docs/providers.md](docs/providers.md)。

```toml
# config/providers.toml

# A CLI you're logged into:
[providers.codex]
type = "cli"
kind = "codex"          # claude | codex | grok | gemini | generic
command = "codex"

# Any OpenAI-compatible API — one adapter, ~every hosted + local server:
[providers.deepseek]
type = "openai"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-chat"
```

Key **一律**從環境變數讀取 —— 絕不會寫進設定檔或記錄檔。

---

## 專案怎麼組織

```
ai-orchestra/
├─ providers.example.toml      # copy to config/providers.toml, declare your plans
├─ scripts/
│  ├─ dispatch.py              # call one provider (CLI or OpenAI-compatible)
│  ├─ dispatch.ps1             # Windows PowerShell UTF-8 wrapper
│  ├─ route.py                 # evidence/quota/reliability-aware recommendation
│  ├─ verify.py                # adversarial cross-check (anti-hallucination)
│  ├─ record_outcome.py        # record proof/human-backed local outcomes
│  ├─ config.py                # loads providers.toml, resolves home/data dirs
│  ├─ usage_report.py          # ledger-based usage table + HTML dashboard
│  ├─ quota_probe.py           # OPTIONAL live-quota probe (best-effort)
│  ├─ quota_common.py          # honest freshness/redaction primitives
│  └─ statusline_quota.py      # OPTIONAL Claude Code statusline + quota export
├─ config/routing_profiles.json # dated task weights and capability priors
├─ docs/                       # workflow, routing evidence, providers, architecture
├─ tests/                      # self-checks
└─ data/                       # local ledger & caches (gitignored)
```

---

## 設計原則

- **誠實優先於完整。** 「未知」是一個有效、第一級的答案。絕不用一個看起來很篤定的 0
  去填補缺口。
- **共識不等於真相。** 多個模型達成一致是一個假設，不是一次驗證。是證據在做驗證。
- **總指揮不兼任自己的獨立 critic。** 同家族 subagents 是工作分工，不是外部佐證。
- **路由是建議，proof 才是驗收。** Provider readiness、quota 與歷史成功率都只是排序訊號。
- **一切都記帳。** 你無法對看不見的花費做推理。
- **秘密只留在環境變數裡。** 絕不進設定檔、絕不進記錄檔、絕不進記帳檔、
  只要能避免就絕不進行程參數。
- **要壞就大聲壞。** 當某個來源失效時，說出來，並說明該怎麼修。

完整說明：[docs/architecture.md](docs/architecture.md)。

---

## 致謝

**靈感來源 🙏：** 這個專案的點子源自 Ted Huang（[@teddashh](https://github.com/teddashh)）的
[**Multi-AI Chat Desktop**](https://github.com/teddashh/multi-ai-chat-desktop) —— 一個用 Tauri
打造、把已登入的 ChatGPT / Claude / Gemini / Grok 網頁整進單一控制台、讓它們協作互審的桌面 App
（零 API key、MIT）。ai-orchestra 從那個「讓多個 AI 互相審查」的核心概念出發，走 CLI／API 派工
＋ 對抗式證據查核這條互補的路。**謝謝 Ted 的啟發。**

誕生自一則關於打敗 AI 幻覺的 [Threads 討論串](https://www.threads.com/@jasonchiou2016/post/Da-bv1Xk4Wj)。
這套反幻覺協定濃縮了以下這些人的回覆：**quant_david、lasxt1995、kanisleo328、
ovveai_api、paul.chen.pwc、lin081626（CloverAI-Family）、pukpuklouis、solitude6060、
harry58892、mat.vmk3s_、jackyyyso** 以及其他人。謝謝你們。他們分享的專案見
[docs/anti-hallucination.md](docs/anti-hallucination.md#credits)。

## 授權

[MIT](LICENSE) © 2026 Jason Chiou ([@jasonchiou2016](https://www.threads.com/@jasonchiou2016))

---

<a name="english"></a>

# English

**Conduct all your AI subscriptions like one orchestra — and stop them from hallucinating in unison.**

- **One conductor in the current Codex or Claude Code session**, with native subagents for bounded parallel work and external providers for cross-provider specialist or critic roles.
- **One interface for every plan** you already pay for (Codex/ChatGPT, Grok, Gemini, any OpenAI-compatible API), through one command.
- **Honest metering** — every call is logged locally; unverifiable numbers say "unknown", never a confident zero.
- **Anti-hallucination by design** — a one-adversary `verify.py` cross-check that demands evidence and refuses to rubber-stamp mere consensus.
- **No dependencies** — pure Python 3.11+ standard library.

**Inspired by** [Multi-AI Chat Desktop](https://github.com/teddashh/multi-ai-chat-desktop) by Ted Huang ([@teddashh](https://github.com/teddashh)) — a Tauri app that orchestrates logged-in ChatGPT/Claude/Gemini/Grok webviews to collaborate and review each other. ai-orchestra takes that "let multiple AIs cross-check each other" idea down a complementary CLI/API + evidence-verified path. Thank you, Ted. 🙏

Full documentation above is in Traditional Chinese (Taiwan). [MIT](LICENSE) © 2026 Jason Chiou.
