# ai-orchestra 🎻

**把你所有的 AI 訂閱當成一個樂團來指揮 —— 並且不讓它們集體產生幻覺。**

`ai-orchestra` 是一套小巧、零相依的工具組，讓一位總指揮（預設是 Claude Code）
把工作分派給你已經在付費的每一個 AI 方案 —— Codex/ChatGPT、Grok、Gemini，
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

### 平行跑好幾個

沒有特殊的 runner —— 這正是重點。從你的 shell（或從 Claude Code 的 Bash 工具，
搭配 `run_in_background`）把每個 `dispatch.py` 丟到背景執行，然後收集結果。
每一次調用都獨立替自己記帳。

### 交叉查核一個宣稱（反幻覺的部分）

挑幾個你已啟用、要當作對手（審查方）的供應商（最好*不是*你的總指揮）：

```bash
echo "Postgres SERIALIZABLE isolation is implemented with two-phase locking" | \
  python scripts/verify.py --critics codex,grok
```

每個對手（審查方）都被要求去**駁斥**這個宣稱，並指出那唯一一項能一槌定音的證據。
因為這個宣稱是假的（Postgres 用的是可序列化快照隔離），一個正常運作的評審團會駁斥它：

```
Tally: 0 supported · 2 refuted · 0 unsupported · 0 uncertain · 0 error
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

> 若沒有給 `--critics`，verify.py 會挑選那些已啟用、標了 `adversary = true`
> 的供應商（沒有的話退回 `role` 含「review」的），一律排除總指揮。精簡過設定就
> 明確傳 `--critics a,b`，
> 或在某個供應商的 role 加上「review」。

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
│  ├─ verify.py                # adversarial cross-check (anti-hallucination)
│  ├─ config.py                # loads providers.toml, resolves home/data dirs
│  ├─ usage_report.py          # ledger-based usage table + HTML dashboard
│  ├─ quota_probe.py           # OPTIONAL live-quota probe (best-effort)
│  ├─ quota_common.py          # honest freshness/redaction primitives
│  └─ statusline_quota.py      # OPTIONAL Claude Code statusline + quota export
├─ docs/                       # anti-hallucination, providers, quota, architecture
├─ tests/                      # self-checks
└─ data/                       # local ledger & caches (gitignored)
```

---

## 設計原則

- **誠實優先於完整。** 「未知」是一個有效、第一級的答案。絕不用一個看起來很篤定的 0
  去填補缺口。
- **共識不等於真相。** 多個模型達成一致是一個假設，不是一次驗證。是證據在做驗證。
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

- **One interface for every plan** you already pay for (Codex/ChatGPT, Grok, Gemini, any OpenAI-compatible API), through one command.
- **Honest metering** — every call is logged locally; unverifiable numbers say "unknown", never a confident zero.
- **Anti-hallucination by design** — a one-adversary `verify.py` cross-check that demands evidence and refuses to rubber-stamp mere consensus.
- **No dependencies** — pure Python 3.11+ standard library.

**Inspired by** [Multi-AI Chat Desktop](https://github.com/teddashh/multi-ai-chat-desktop) by Ted Huang ([@teddashh](https://github.com/teddashh)) — a Tauri app that orchestrates logged-in ChatGPT/Claude/Gemini/Grok webviews to collaborate and review each other. ai-orchestra takes that "let multiple AIs cross-check each other" idea down a complementary CLI/API + evidence-verified path. Thank you, Ted. 🙏

Full documentation above is in Traditional Chinese (Taiwan). [MIT](LICENSE) © 2026 Jason Chiou.
