---
name: ai-orchestra
description: >-
  Conduct all your AI subscriptions like one orchestra. A coordinator (Claude
  Code) delegates work to every AI plan you have — Codex, Grok, Gemini, or any
  OpenAI-compatible API (OpenAI, DeepSeek, Groq, OpenRouter, NVIDIA NIM, local
  Ollama/LM Studio, …) — through one command, with every call metered and an
  adversarial cross-check that treats "the models agreed" as not good enough.
  Use when a task benefits from splitting work across models (research, review,
  bulk generation, second opinions), when you want to route grunt work off your
  scarce coordinator budget, or when you want to reduce hallucination by
  grounding + adversarial verification. Triggers: "multi-AI", "dispatch to
  grok/codex/gemini/deepseek", "cross-check", "verify this claim", "AI usage",
  "how much quota left".
---

# ai-orchestra — multi-AI dispatch & anti-hallucination

A dependency-free toolkit (Python 3.11+ standard library) that lets one
coordinator delegate to every AI subscription you have, meters every call, and
cross-checks claims adversarially. Full docs live in `docs/`.

## First-time setup

```bash
cp providers.example.toml config/providers.toml   # then enable your providers
python scripts/dispatch.py --list                  # confirm they load
```

Providers are declared in `config/providers.toml` — see
[docs/providers.md](docs/providers.md). Two types cover almost everything:
`cli` (claude/codex/grok/gemini/generic) and `openai` (any OpenAI-compatible
endpoint). API keys come from environment variables, never the config file.

## Core commands

```bash
# Call one provider (prompt via stdin):
echo "PROMPT" | python scripts/dispatch.py <provider> [--label NAME] [--model M] [--timeout S]

# Windows PowerShell — use the UTF-8 wrapper so CJK survives the pipe:
'PROMPT' | & .\scripts\dispatch.ps1 <provider>

# Adversarially cross-check a claim (anti-hallucination):
echo "CLAIM" | python scripts/verify.py --critics <a,b>

# See spend / success rates:
python scripts/usage_report.py [--html]
```

- **Parallel work:** launch several `dispatch.py` in the background (Claude Code
  Bash `run_in_background: true`) and collect results. Each call meters itself.
- **Claude adapter extras:** `--claude-profile review` (read-only independent
  reviewer that obeys the repo's `AGENTS.md`), `--effort`, `--max-budget-usd`.
- **Don't dispatch to `claude` from inside a Claude Code conversation** — a
  nested `claude -p` there returns 401. Use a subagent/Workflow instead; dispatch
  to `claude` only from a plain shell.

## How to actually reduce hallucination

Read [docs/anti-hallucination.md](docs/anti-hallucination.md). The short version,
from the community that inspired this:

1. **Ground every claim** in a real source; require citations / `file:line`.
2. **Demand proof-of-work**, not assertions — a diff, a test, a command output.
3. **One good adversary beats five agreeable rounds** — pipe answers through
   `verify.py`; its job is to *refute*, not to vote.
4. **Verify completion by replay**, not by the model's claim of "done".
5. **Consensus is not truth.** Models can be wrong together. `verify.py` will not
   rubber-stamp unanimous-but-unsourced agreement.

## Standard operating procedure

1. **Check budget first** — `usage_report.py`; route away from whatever is low.
2. **Split & delegate** — send grunt work (translation, bulk drafts, summaries)
   to a cheap/free provider; keep the coordinator for judgment.
3. **Cross-examine** — have a *different* provider or a subagent attack the
   output; focus on facts, numbers, and sources.
4. **Resolve on evidence** — fix or attach proof, then re-verify.
5. **Review the ledger** — confirm the extra rounds bought accuracy, not just tokens.

## Anti-patterns

- ❌ Asking every model the same question (wasted budget) — the value is in
  *division of labour* and *independent adversarial* checks, not a chorus.
- ❌ Trusting any single model's "facts" — route claims through verification.
- ❌ Forgetting to meter — always go through `dispatch.py` so the dashboard stays honest.
- ❌ Over-orchestrating a task a clear prompt + one capable model would nail.

## Config

- `config/providers.toml` — your providers (gitignored; keys stay in env).
- `data/` — ledger, quota cache, dashboard (gitignored).
- `$AI_ORCHESTRA_HOME` — override where config/data live (defaults to the repo root).
