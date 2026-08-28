# Model routing evidence

Checked: 2026-08-28 (Asia/Taipei)

This file explains the priors in `config/routing_profiles.json`. A prior is a
starting preference, not proof that a model will succeed on the current task. The
router must combine it with fresh quota state, this machine's dispatch ledger,
and verified local outcomes.

## Evidence tiers

1. **Official capability** says what the vendor designed or positioned a model
   to do. It is useful for supported features and model tiers, but remains a
   vendor claim.
2. **Independent/community empirical** gives directional comparisons. Harness,
   prompt, task mix and model version can change rankings, so the router does
   not copy leaderboard percentages into its capability scale.
3. **Local empirical** measures the exact CLI/API surface used here. Dispatch
   success and latency come from `data/ledger.jsonl`; quality comes only from
   `data/outcomes.jsonl` after human acceptance or deterministic proof.

The 0–5 capability values are deliberately coarse curator priors: `5` means a
primary fit supported by official positioning plus a relevant empirical signal;
`4` a strong general fit; `3` a usable generalist; `2` a surface-limited fit;
`1` a poor fit; and `0` unsupported. Leaderboard percentages are never converted
arithmetically into these values. Each profile's `prior_basis` states the mapping
rationale, while live ledger/outcome data can move the final route.

## Official sources and routing consequences

- [OpenAI GPT-5.6 guidance](https://developers.openai.com/api/docs/guides/latest-model)
  separates Sol (frontier), Terra (balanced) and Luna (efficient/high-volume),
  and recommends testing lower reasoning effort for token efficiency. Routing:
  Codex uses quality/balanced/economy model hints instead of always spending the
  highest tier.
- [OpenAI GPT-5-Codex](https://developers.openai.com/api/docs/models/gpt-5-codex)
  is explicitly optimized for agentic coding. Routing: Codex gets a strong
  coding prior, but another OpenAI process is not an independent critic of a
  Codex coordinator.
- [Anthropic model overview](https://platform.claude.com/docs/en/about-claude/models/overview)
  positions Opus for complex agentic coding, Sonnet for speed/intelligence
  balance, and Claude generally for reasoning, coding, multilingual and
  long-context work. Routing: Sonnet is the subscription-efficient default
  critic; Opus is reserved for quality-first work.
- [Gemini 3.7 Flash guidance](https://ai.google.dev/gemini-api/docs/latest-model)
  calls it a coding/agent workhorse and identifies it as the Antigravity agent's
  default. [Gemini long-context guidance](https://ai.google.dev/gemini-api/docs/long-context)
  documents multimodal and long-context strengths while warning that retrieval,
  latency and token cost still vary with context. Routing: AGY uses 3.7 Flash
  for balanced/economy work and 3.1 Pro High only for selected quality tasks.
- [xAI model guidance](https://docs.x.ai/developers/models) positions Grok 4.6
  for code and agentic tool use, and explicitly says current events require Web
  Search or X Search. Routing: Grok receives the strongest current-web/social
  prior only when its search surface remains enabled.
- [NVIDIA NIM docs](https://docs.nvidia.com/nim/large-language-models/latest/get-started/index.html)
  confirm hosted Llama 3.3 70B Instruct availability. Routing: the configured
  NIM model is a free-credit bulk/translation worker, not a frontier critic.
- [MiniMax M2.7 release](https://www.minimax.io/news/minimax-m27-en) emphasizes
  agent harnesses, software engineering and office work. Routing: M2.7 is an
  office/long-form alternative, but its metered API receives less economy bonus
  than already-paid subscriptions.

## Independent and community evidence

- [Terminal-Bench 2.0](https://www.tbench.ai/leaderboard/terminal-bench/2.0)
  uses executable terminal tasks and shows large differences between harnesses
  using the same model. Consequence: model family alone is insufficient; the
  local CLI surface and its recent reliability must affect routing.
- [Arena leaderboard dataset](https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset)
  publishes Agent Arena outcome/preference signals with dates, confidence bounds
  and methodology notes. Consequence: use it as a broad prior, never as proof
  that a particular code change or business answer is correct.

## Local evidence is runtime data

Do not commit a user's ledger, quota cache, outcomes, CLI versions or provider
authentication state as if they were universal facts. `route.py` reads the
current installation at run time:

- `data/ledger.jsonl` supplies recent success, error class and latency.
- `data/quota_cache.json` supplies only fresh, attributable samples. Local call
  counts are not official remaining quota.
- `data/outcomes.jsonl` supplies proof/human-backed quality observations.
- AGY's headless surface receives a tool-use penalty only through its curated
  profile; current local failures still come from the user's own ledger.

Unknown is not zero or unlimited. A provider that passes `--doctor` is merely
configured well enough for a smoke test; authentication and usable capacity are
confirmed only by an actual dispatch.

## Feedback loop

After a dispatched task has deterministic proof or explicit human acceptance,
record only metadata:

```powershell
python .\scripts\record_outcome.py --provider agy --model gemini-3.7-flash-high `
  --task design --verdict pass --label design-review-20260828 `
  --proof-label design-review-proof-20260828 `
  --evidence "validated artifact and replay command"
```

For an explicitly accepted human judgment, use `--human-accepted` instead of a
proof label. Do not record a model's own confidence as a pass. Do not store
prompt bodies, customer-identifying data or secrets in outcome evidence.

Current limitation: the proof link uses a unique ledger label and is not yet
cryptographically bound to an artifact hash, commit, build or deploy id. Treat
recorded outcomes as routing priors, not as the final gate for a high-risk
release. Re-check the real artifact and external state before declaring done.
