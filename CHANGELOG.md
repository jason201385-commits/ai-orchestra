# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — 2026-07-21

First open-source release. Generalized from a personal Claude Code skill into a
tool anyone can point at their own AI subscriptions.

### Added
- **Config-driven providers** (`config/providers.toml`). Declare any number of
  AI plans with two types: `cli` (claude / codex / grok / gemini / generic) and
  `openai` (any OpenAI-compatible HTTP API — OpenAI, DeepSeek, Groq, OpenRouter,
  Mistral, xAI, NVIDIA NIM, local Ollama / LM Studio, …). No code to add a
  provider.
- **`verify.py`** — an adversarial cross-check that demands evidence and refuses
  to treat mere consensus as verification. Implements the community-sourced
  anti-hallucination protocol.
- **`docs/anti-hallucination.md`** — the protocol, distilled from a public
  discussion, with credits.
- Full docs: providers, quota/metering, architecture; bilingual README (EN + 繁中).
- MIT license, `.gitignore`, contributing guide.

### Changed
- `dispatch.py` refactored from a hardcoded 6-provider list into a config-driven
  dispatcher, while preserving the streaming/timeout/process-tree/serialization
  machinery for the Claude adapter.
- Quota scripts now resolve their home via `config.py` (honouring
  `$AI_ORCHESTRA_HOME`) instead of a fixed path.
- Live-quota probing is now framed as an optional, best-effort feature that
  degrades to universal ledger-based counting.

### Removed
- Personal data and setup-specific hardcoding (the original author's usage
  ledger, machine paths, and a bespoke desktop-app provider).
