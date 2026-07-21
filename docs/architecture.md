# Architecture

`ai-orchestra` is deliberately small: a set of standard-library Python scripts
around one config file and one local ledger. There is no server, no daemon, no
database, and nothing to `pip install`.

```
             config/providers.toml          (you declare your plans)
                      │
                      ▼
   stdin ──► dispatch.py ──► adapter ──► provider (CLI or HTTP API) ──► stdout
                      │
                      └──► data/ledger.jsonl  (one metered line per call)
                                   │
                      quota_probe.py (optional, best-effort)
                                   │
                                   ▼
                          usage_report.py ──► terminal / data/dashboard.html

   verify.py ──► spawns dispatch.py per critic ──► adversarial verdict
```

## Components

- **`config.py`** — resolves the home directory (`$AI_ORCHESTRA_HOME` or the
  repo root), the `config/` and `data/` dirs, and loads `providers.toml` into a
  normalized `{name: spec}` map. `tomllib` is standard library on 3.11+.

- **`dispatch.py`** — the one entry point for calling a provider. It:
  - picks an adapter from the provider's `type`/`kind`;
  - reads the prompt from **stdin** (so it never lands in the process arg list);
  - for the `claude` adapter, streams `stream-json`, prints heartbeats, enforces
    a real deadline, kills the whole process tree on timeout (Windows Job Object
    / POSIX process group), and serializes concurrent Claude calls with an
    advisory file lock;
  - for `openai`, does a single `chat/completions` POST;
  - writes exactly one ledger line, with secrets redacted.

- **`verify.py`** — the anti-hallucination cross-check. It spawns `dispatch.py`
  per critic (so critics are metered too), feeds each an adversarial
  evidence-demanding prompt, and synthesizes a verdict that explicitly refuses
  to treat unanimous-but-unsourced agreement as verification.

- **`quota_common.py`** — the honesty primitives shared by the metering layer:
  freshness grading (FRESH / RECENT / STALE / EXPIRED), secret redaction, and a
  stable failure-classification ruleset.

- **`usage_report.py` / `quota_probe.py` / `statusline_quota.py`** — the
  metering & dashboard layer. Ledger-based counting is universal; live probes
  are optional and best-effort (see [quota.md](quota.md)).

## Invariants

1. **Secrets never persist.** Keys come from environment variables, are used to
   sign a request, and are discarded. They are never written to config, ledger,
   log, or (where avoidable) a process argument. All ledger/report output passes
   through `redact()`.
2. **stdin for prompts.** The dispatcher takes the prompt on stdin so prompt
   text doesn't show up in the OS process list. (Some third-party CLIs still
   require the prompt as an argument — so don't put secrets in prompts.)
3. **Honest > complete.** Unknown is a first-class value. Stale data is labelled
   and never shown as live. The dashboard never invents a zero.
4. **Fail soft, report loud.** One provider failing never takes down the run; the
   failure is classified and surfaced with a fix hint.
5. **No hidden state.** Everything lives in plain files under `data/` and
   `config/`. Delete `data/` to reset; nothing else is touched.

## Portability

- Pure standard library, Python 3.11+.
- `dispatch.py` and `verify.py` are cross-platform.
- The optional live-quota probes are Windows/desktop-app oriented and degrade to
  ledger counting elsewhere.
- Windows PowerShell users pipe prompts through `dispatch.ps1` so non-ASCII text
  survives the pipe as UTF-8; the dispatcher also rejects obvious mojibake so a
  corrupted prompt fails fast instead of silently reaching the model.

## Extending

- **New API provider:** just add a `type = "openai"` block — no code.
- **New CLI with an unusual interface:** use `kind = "generic"` with an arg
  template, or add a purpose-built adapter to `CLI_ADAPTERS` in `dispatch.py`
  (signature `(spec, prompt, model, timeout) -> (rc, text, err, dur, usage, rate)`).
