# ai-orchestra 🎻

**Conduct all your AI subscriptions like one orchestra — and stop them from hallucinating in unison.**

`ai-orchestra` is a tiny, dependency-free toolkit that lets one coordinator
(Claude Code by default) delegate work to every AI plan you already pay for —
Codex/ChatGPT, Grok, Gemini, or any OpenAI-compatible API (OpenAI, DeepSeek,
Groq, OpenRouter, Mistral, NVIDIA NIM, local Ollama/LM Studio, …) — through
**one command**, with **every call metered** and a built-in **adversarial
cross-check** that treats "the models agreed" as *not good enough*.

It started as a personal skill for [Claude Code](https://claude.com/claude-code)
and grew out of a [public thread](https://www.threads.com/@jasonchiou2016/post/Da-bv1Xk4Wj)
where dozens of practitioners argued about how to actually beat hallucination.
Their conclusion shaped the whole design — see
[docs/anti-hallucination.md](docs/anti-hallucination.md).

> **繁體中文說明在最下方 ↓ ([跳到中文](#中文說明))**

---

## Why

- **One interface for every plan.** Your paid subscriptions are idle capacity.
  Declare them once in a config file; call any of them the same way.
- **Spend your scarce budget wisely.** If your main seat is Claude Code, *Claude
  tokens are the scarce resource*. Route grunt work (translation, bulk drafts,
  summaries) to a cheaper or free provider and keep the coordinator for judgment.
- **Honest metering.** Every call is logged locally (duration, tokens, ok/fail).
  No dashboard lies to you: if a number can't be verified, it says "unknown"
  instead of showing a confident zero.
- **Anti-hallucination by design.** A one-adversary `verify.py` cross-check that
  demands evidence and refuses to rubber-stamp mere consensus.

**No dependencies.** Pure Python 3.11+ standard library. Nothing to `pip install`.

---

## Install (60 seconds)

```bash
git clone https://github.com/<you>/ai-orchestra.git
cd ai-orchestra

# 1. declare the subscriptions you actually have
cp providers.example.toml config/providers.toml
#    edit config/providers.toml — enable your providers, set models

# 2. sanity check
python scripts/dispatch.py --list
```

Requires **Python 3.11+** (for the standard-library `tomllib`). For CLI
providers, install and log into the relevant CLI (`claude`, `codex`, `grok`,
`gemini`). For API providers, set the API key in the environment variable named
in your config (e.g. `OPENAI_API_KEY`).

---

## Use

### Call one provider

```bash
# CLI providers (already logged in):
echo "Summarize the tradeoffs of optimistic locking" | python scripts/dispatch.py codex

# OpenAI-compatible APIs (key from env):
echo "Translate to French: good morning" | python scripts/dispatch.py deepseek

# pick a model / label / timeout inline:
echo "..." | python scripts/dispatch.py openrouter --model anthropic/claude-sonnet-5 --label triage
```

On **Windows PowerShell**, pipe through the UTF-8 wrapper so non-ASCII prompts
survive the pipe:

```powershell
'把這段翻成英文：早安' | & .\scripts\dispatch.ps1 deepseek
```

### Run several in parallel

There's no special runner — that's the point. Launch each `dispatch.py` in the
background from your shell (or from Claude Code's Bash tool with
`run_in_background`) and collect the results. Each call meters itself
independently.

### Cross-check a claim (the anti-hallucination bit)

Pick a couple of providers you've enabled as adversaries (ideally *not* your
coordinator):

```bash
echo "Postgres SERIALIZABLE isolation is implemented with two-phase locking" | \
  python scripts/verify.py --critics codex,grok
```

Each critic is told to **refute** the claim and to name the one piece of
evidence that would settle it. Because that claim is false (Postgres uses
serializable snapshot isolation), a working panel refutes it:

```
Tally: 0 supported · 2 refuted · 0 unsupported · 0 uncertain
RESULT (exit 2): DO NOT SHIP AS-IS — a critic refuted the claim...
```

The exit code backs the verdict — and there is deliberately **no exit 0**: even
unanimous support returns a non-zero "UNVERIFIED — consensus is not proof" code,
because agreement between models is a hypothesis, not a verification.

> With no `--critics`, verify.py picks enabled providers whose `role` contains
> "review" (excluding the coordinator). If you've trimmed your config, pass
> `--critics a,b` explicitly, or add "review" to a provider's role.

### See what you're spending

```bash
python scripts/usage_report.py            # terminal table from the local ledger
python scripts/usage_report.py --html     # writes data/dashboard.html
```

The ledger (`data/ledger.jsonl`) is the source of truth and works for **every**
provider. An optional, best-effort quota probe can surface live remaining-usage
for a few providers on Windows — see [docs/quota.md](docs/quota.md). It degrades
gracefully to local counting everywhere else.

---

## Add any provider

Two types cover essentially every AI plan on the market. Full guide:
[docs/providers.md](docs/providers.md).

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

Keys are **always** read from environment variables — never written into the
config or the logs.

---

## How it's organized

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

## Design principles

- **Honest > complete.** Unknown is a valid, first-class answer. Never fill a
  gap with a confident-looking zero.
- **Consensus is not truth.** Multiple models agreeing is a hypothesis, not a
  verification. Evidence verifies.
- **Meter everything.** You can't reason about spend you can't see.
- **Secrets stay in the environment.** Never in config, never in a log, never in
  a ledger, never in a process argument you can avoid.
- **Degrade loudly.** When a source fails, say so and say how to fix it.

Full write-up: [docs/architecture.md](docs/architecture.md).

---

## Credits

Born from a [Threads discussion](https://www.threads.com/@jasonchiou2016/post/Da-bv1Xk4Wj)
on beating AI hallucination. The anti-hallucination protocol distills replies
from **quant_david, lasxt1995, kanisleo328, ovveai_api, paul.chen.pwc,
lin081626 (CloverAI-Family), pukpuklouis, solitude6060, harry58892, mat.vmk3s_,
jackyyyso** and others. Thank you. See
[docs/anti-hallucination.md](docs/anti-hallucination.md#credits) for the
projects they shared.

## License

[MIT](LICENSE) © 2026 Jason Chiou ([@jasonchiou2016](https://www.threads.com/@jasonchiou2016))

---

<a name="中文說明"></a>

# 中文說明

**把你所有的 AI 訂閱當成一個樂團來指揮 —— 並且不讓它們集體幻覺。**

`ai-orchestra` 是一套零相依、純 Python 標準庫的小工具。讓一個總指揮（預設是
Claude Code）把工作分派給你已經付費的每一個 AI 方案 —— Codex/ChatGPT、Grok、
Gemini，或任何 OpenAI 相容 API（OpenAI、DeepSeek、Groq、OpenRouter、Mistral、
NVIDIA NIM、本地的 Ollama / LM Studio……）—— 全部用**同一個指令**呼叫，**每次
調用都自動記帳**，並內建一個**對抗式交叉查核**，把「模型都同意」視為「還不夠」。

它源自一則[公開討論串](https://www.threads.com/@jasonchiou2016/post/Da-bv1Xk4Wj)，
幾十位實戰者在底下辯論「到底怎麼有效減少幻覺」。他們的結論塑造了整個設計 ——
詳見 [docs/anti-hallucination.md](docs/anti-hallucination.md)。

## 為什麼

- **一個介面，呼叫所有方案。** 你的訂閱都是閒置產能，宣告一次，用同一種方式呼叫。
- **把稀缺額度花在刀口上。** 如果你主力是 Claude Code，那 Claude 額度才是稀缺
  資源。把粗活（翻譯、批量草稿、摘要）丟給便宜或免費的供應商，總指揮留給判斷。
- **誠實記帳。** 每次調用都在本地記錄；儀表板不騙人：查不到就標「未知」，絕不用
  一個看起來很篤定的 0 來填空。
- **反幻覺是設計的一部分。** `verify.py` 用「一個對手」做交叉查核，要求證據，
  拒絕替「只是彼此同意」的結論背書。

**零相依**，只需 Python 3.11+。

## 快速開始

```bash
git clone https://github.com/<you>/ai-orchestra.git
cd ai-orchestra
cp providers.example.toml config/providers.toml   # 編輯它，打開你有的供應商
python scripts/dispatch.py --list
```

呼叫單一供應商：

```bash
echo "把這段翻成英文：早安" | python scripts/dispatch.py deepseek
```

Windows PowerShell 請走 UTF-8 wrapper，避免中文在管線裡變亂碼：

```powershell
'把這段翻成英文：早安' | & .\scripts\dispatch.ps1 deepseek
```

交叉查核一個宣稱（反幻覺核心）：挑兩個你已啟用、且非總指揮的供應商當對手。

```bash
echo "某個技術宣稱……" | python scripts/verify.py --critics codex,grok
```

刻意沒有 exit 0：連全數支持也回非零的「UNVERIFIED — 共識不等於證據」，
因為模型彼此同意只是假設，不是驗證。

看用量：

```bash
python scripts/usage_report.py --html
```

## 核心原則

- **誠實優先於完整** —— 取不到就標「未知」，不要拿舊值冒充即時，也不要用 0 填空。
- **共識不等於真相** —— 多個模型同意只是假設，證據才是驗證。
- **一律記帳** —— 看不到的花費就無法管理。
- **秘密只留在環境變數** —— 不進 config、不進 log、不進記帳檔。

授權：[MIT](LICENSE)。感謝 Threads 討論串上所有無私分享的朋友。
