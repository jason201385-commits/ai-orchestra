# Contributing

Thanks for helping. `ai-orchestra` stays small and dependency-free on purpose —
contributions should preserve that.

## Ground rules

- **Standard library only.** No third-party runtime dependencies. If a feature
  needs a package, it probably belongs in a separate optional script, clearly
  marked.
- **Secrets never persist.** Keys come from environment variables. Never write a
  key (or a fragment of one) into config, logs, the ledger, or a committed file.
  All output that could carry a secret must pass through `quota_common.redact()`.
- **Honest > complete.** Don't paper over an unknown with a plausible default. If
  a value can't be verified, surface "unknown" and say how to fix it.
- **Cross-platform.** `dispatch.py`, `verify.py`, and `config.py` must run on
  Windows, macOS, and Linux. Platform-specific features (e.g. the live-quota
  probes) must degrade gracefully elsewhere.

## Good first issues

- **Localize the dashboard.** `usage_report.py` / `quota_probe.py` labels are
  currently Chinese/bilingual — a clean i18n pass (or English default) is very
  welcome.
- **More verified provider recipes** for `providers.example.toml` and
  [docs/providers.md](docs/providers.md).
- **A live-quota probe for another provider**, following the honesty rules in
  `quota_common.py` (label the source, grade freshness, never guess).

## Adding a provider adapter

Most providers need **no code** — a `type = "openai"` block covers every
OpenAI-compatible API. Only add code for a genuinely different CLI:

1. Write `call_yourcli(spec, prompt, model, timeout)` returning
   `(rc, text, err, duration, usage, rate)`.
2. Register it in `CLI_ADAPTERS` in `scripts/dispatch.py`.
3. Read the command name from `spec["command"]` (don't hardcode).
4. Add an example block to `providers.example.toml` and a row to the provider
   docs.

## Running the checks

```bash
python scripts/config.py                 # config resolves, providers parse
python scripts/quota_common.py           # freshness + redaction self-check
python scripts/dispatch.py --list        # providers enumerate
python tests/test_quota_display.py       # dashboard/display invariants
python tests/test_dispatch_stream.py     # streaming-timeout state machine
```

Please run these before opening a PR, and describe how you tested any provider
adapter (they hit real services, so we can't run them in CI).

## Commit style

Small, focused commits with a clear message. Explain *why*, not just *what*.
