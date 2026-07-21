#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — where ai-orchestra keeps its home, and how it loads your providers.

Design goals:
  * Zero third-party dependencies (tomllib ships with Python 3.11+).
  * A single, obvious place to declare *your* AI subscriptions (config/providers.toml).
  * Sane fallbacks so a fresh clone still runs the built-in CLI providers.

Home directory resolution (first match wins):
  1. $AI_ORCHESTRA_HOME                      — explicit override
  2. the repository root (parent of scripts/) — the default for a git clone

  config lives in   <home>/config/providers.toml
  runtime data in   <home>/data/            (ledger, quota cache — gitignored)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - very old Python
    tomllib = None


def home_dir() -> Path:
    env = os.environ.get("AI_ORCHESTRA_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


HOME = home_dir()
CONFIG_DIR = HOME / "config"
DATA_DIR = HOME / "data"
USER_PROVIDERS = CONFIG_DIR / "providers.toml"
EXAMPLE_PROVIDERS = HOME / "providers.example.toml"
LEDGER = DATA_DIR / "ledger.jsonl"


# ── provider schema ────────────────────────────────────────────────
# A provider is either:
#   type = "cli"     — a local command-line tool you already log into
#   type = "openai"  — any OpenAI-compatible /chat/completions HTTP endpoint
#
# CLI providers carry a `kind` so we can use the right adapter:
#   claude | codex | grok | gemini   → purpose-built adapters (best UX)
#   generic                          → run `command` with args, prompt via
#                                      argument placeholder {prompt} or stdin
#
# openai providers cover almost every paid API on the market with one adapter:
#   OpenAI, Anthropic (OpenAI-compat endpoint), DeepSeek, Groq, Mistral,
#   xAI/Grok API, Together, OpenRouter, Fireworks, Perplexity, NVIDIA NIM,
#   and local servers (Ollama, LM Studio, vLLM, llama.cpp).

BUILTIN_CLI_KINDS = {"claude", "codex", "grok", "gemini", "generic"}


def _normalize(name: str, raw: dict) -> dict:
    ptype = str(raw.get("type", "cli")).lower()
    try:
        dt = int(raw.get("default_timeout", 300))
    except (TypeError, ValueError):
        dt = 300
    spec = {
        "name": name,
        "type": ptype,
        "role": str(raw.get("role", "")),
        "enabled": bool(raw.get("enabled", True)),
        # A non-positive timeout would crash the dispatcher — fall back to 300.
        "default_timeout": dt if dt > 0 else 300,
        "model": raw.get("model") or None,
        # Explicit opt-in as a verify.py adversary/critic (preferred over the
        # legacy role~="review" heuristic).
        "adversary": bool(raw.get("adversary", False)),
    }
    if ptype == "cli":
        kind = str(raw.get("kind", "generic")).lower()
        if kind not in BUILTIN_CLI_KINDS:
            kind = "generic"
        spec.update({
            "kind": kind,
            "command": raw.get("command") or name,
            # generic CLI: extra args; {prompt} is replaced with the prompt,
            # or if no {prompt} placeholder exists and stdin=true, piped in.
            "args": list(raw.get("args", [])),
            "stdin": bool(raw.get("stdin", False)),
        })
    elif ptype == "openai":
        try:
            max_tokens = int(raw.get("max_tokens", 4096))
        except (TypeError, ValueError):
            max_tokens = 4096
        spec.update({
            "kind": "openai",
            "base_url": str(raw.get("base_url", "")).rstrip("/"),
            "api_key_env": str(raw.get("api_key_env", "")),
            "max_tokens": max_tokens,
            # parameter name for the token cap; OpenAI's newer models want
            # "max_completion_tokens". Set max_tokens = 0 to omit the cap.
            "max_tokens_param": str(raw.get("max_tokens_param", "max_tokens")),
            # some endpoints (e.g. local) accept an empty/omitted key
            "extra_headers": dict(raw.get("extra_headers", {})),
        })
    else:
        spec["kind"] = "unknown"
    return spec


def load_providers(path: Path | None = None) -> dict:
    """Return {name: spec}. Prefers config/providers.toml, falls back to the
    shipped providers.example.toml so a fresh clone is still usable."""
    if tomllib is None:
        print("[config] Python 3.11+ required for tomllib", file=sys.stderr)
        return {}
    chosen = path
    if chosen is None:
        chosen = USER_PROVIDERS if USER_PROVIDERS.exists() else EXAMPLE_PROVIDERS
    if not chosen.exists():
        return {}
    try:
        doc = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"[config] failed to read {chosen}: {e}", file=sys.stderr)
        return {}
    table = doc.get("providers", {})
    out = {}
    for name, raw in table.items():
        if isinstance(raw, dict):
            out[name] = _normalize(name, raw)
    return out


def provider_names(only_enabled: bool = True) -> list:
    provs = load_providers()
    return sorted(
        n for n, s in provs.items() if (s.get("enabled", True) or not only_enabled)
    )


def config_source() -> Path:
    return USER_PROVIDERS if USER_PROVIDERS.exists() else EXAMPLE_PROVIDERS


if __name__ == "__main__":
    provs = load_providers()
    print(f"home:   {HOME}")
    print(f"config: {config_source()}")
    print(f"data:   {DATA_DIR}")
    print(f"\n{len(provs)} provider(s) configured:")
    for name, spec in sorted(provs.items()):
        flag = "" if spec.get("enabled", True) else "  (disabled)"
        detail = spec.get("kind")
        if spec["type"] == "openai":
            detail = f"openai → {spec.get('base_url')}"
        print(f"  {name:<14}{spec['type']:<8}{detail}{flag}")
        if spec.get("role"):
            print(f"  {'':<14}role: {spec['role']}")
