# Usage & quota (honest metering)

There are two layers here. One works for **everyone**; the other is an
**optional, best-effort bonus**.

## 1. The ledger — universal, always correct

Every `dispatch.py` call appends one line to `data/ledger.jsonl`:

```json
{"ts":"2026-07-21T14:00:00+08:00","provider":"deepseek","type":"openai",
 "label":"triage","duration_s":2.1,"output_chars":812,"ok":true,"rc":0, ...}
```

It records **metadata only** — provider, duration, token counts (when the
provider returns them), success/failure and a failure *kind*. It never stores
your prompt text and never stores a key.

`usage_report.py` turns the ledger into a table (calls, success rate, tokens,
top failure reasons) and an offline HTML dashboard:

```bash
python scripts/usage_report.py            # terminal
python scripts/usage_report.py --html     # writes data/dashboard.html
python scripts/usage_report.py --days 14  # widen the window
```

The dashboard is a single static file with **no external resources** (no CDN,
no remote fonts/images). Re-run the command to refresh it.

## 2. Live remaining-quota probes — optional, best-effort

`quota_probe.py` tries to surface *real remaining usage* for a few providers.
This is inherently fragile: most vendors don't offer a remaining-quota API, so
the probes read local app files or undocumented endpoints. **They are honest
about it** — anything they can't verify is shown as "unknown", stale values are
labelled and refuse to masquerade as live, and unofficial sources are tagged.

```bash
python scripts/quota_probe.py --all       # probe, write data/quota_cache.json
python scripts/usage_report.py            # the report picks the cache up
```

Reality per provider (this is the honest boundary — it changes as vendors do):

| Provider | What a probe can get | What it cannot |
|---|---|---|
| Codex/ChatGPT | Live % from an **unofficial** internal endpoint (may break anytime); exact tokens per call | No documented API |
| Claude | Live 5h/7d % **if** you enable the statusline export (below) or the desktop app's local history file (Windows) | Sandboxed `claude -p` can't self-probe |
| Grok | Success/fail + a negative "402 = exhausted" signal from local logs | No balance API |
| Gemini | Local count vs the free-tier cap | No remaining-quota API |
| NVIDIA NIM | Local count vs the RPM cap | Response headers carry no quota info |

> The probes are **Windows- and desktop-app-specific** by nature (they read
> `%APPDATA%` files and vendor internals). On other platforms — or for any
> provider without a probe — the system falls back to ledger-based local
> counting, which is always available. The probe labels are currently
> Chinese/bilingual; PRs to fully localize the dashboard are welcome.

### Optional: live Claude quota via the statusline

If you use Claude Code, `statusline_quota.py` doubles as a statusline script and
exports your live 5h/7d percentages to `data/claude_quota.json`. Enable it in
`~/.claude/settings.json` (adjust the path to your clone):

```json
"statusLine": {
  "type": "command",
  "command": "python /path/to/ai-orchestra/scripts/statusline_quota.py"
}
```

### Manual reporting (fallback)

When no automatic source is available you can record a reading yourself; an
automatic source, if newer, always wins:

```bash
python scripts/quota_probe.py --set claude 5h=22 week_all=61
python scripts/quota_probe.py --show-manual
```

## The rule these obey

Honest beats complete. A quota you can't verify is shown as **unknown**, not as
a reassuring number. That is the same discipline the
[anti-hallucination protocol](anti-hallucination.md) applies to claims — don't
present the unverified as verified.
