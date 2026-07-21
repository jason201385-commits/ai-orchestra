# Adding providers

You declare the AI plans you have in `config/providers.toml`. Copy the shipped
template and edit it:

```bash
cp providers.example.toml config/providers.toml
```

> **Heads up:** the shipped example ships every provider except `claude`/`codex`
> with `enabled = false`. When you fill in a provider you use, set
> `enabled = true` (or delete the line — it defaults to true) or dispatch will
> report `provider '<name>' is disabled`. The snippets below omit `enabled` for
> brevity, i.e. they assume enabled.

There are exactly two provider **types**. Between them they cover essentially
every paid AI plan and every local model server.

---

## `type = "cli"` — a command-line tool you're logged into

```toml
[providers.codex]
type    = "cli"
kind    = "codex"        # claude | codex | grok | gemini | generic
command = "codex"        # the executable name (or full path)
role    = "independent reviewer"   # free-text; used by verify.py's defaults
enabled = true           # omit or true to enable; false to keep but skip
default_timeout = 300    # seconds
```

`kind` selects the adapter:

| `kind` | For | Notes |
|---|---|---|
| `claude` | [Claude Code](https://claude.com/claude-code) | Purpose-built: streaming JSON, read-only `review` profile, plan mode, `--effort`, `--max-budget-usd`, cross-process serialization. The recommended coordinator. |
| `codex` | OpenAI Codex CLI | Parses `codex exec --json`, extracts token usage. |
| `grok` | xAI Grok CLI | Parses `grok -p --output-format json`, falls back to plain text. |
| `gemini` | Google Gemini CLI | Detects the "ineligible tier / auth" failure and tells you how to fix it. |
| `generic` | **any other CLI** | You provide the arg template — see below. |

### The `generic` CLI adapter

Wire up *any* command-line tool. Use `{prompt}` (and optionally `{model}`) as
placeholders:

```toml
[providers.mytool]
type    = "cli"
kind    = "generic"
command = "mytool"
args    = ["chat", "--model", "{model}", "--message", "{prompt}"]
model   = "some-model"
```

If you **omit** a `{prompt}` placeholder, the prompt is either:
- piped to the tool's **stdin** when `stdin = true`, or
- appended as the **final argument** otherwise.

```toml
[providers.stdintool]
type    = "cli"
kind    = "generic"
command = "stdintool"
args    = ["--quiet"]
stdin   = true          # prompt goes to stdin
```

---

## `type = "openai"` — any OpenAI-compatible HTTP API

One adapter, `POST {base_url}/chat/completions`, works for a huge range of
services:

```toml
[providers.deepseek]
type        = "openai"
base_url    = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"   # the ENV VAR NAME holding your key
model       = "deepseek-chat"
max_tokens  = 4096                 # optional (default 4096)
role        = "cheap batch"
```

The API key is read from the environment variable you name in `api_key_env`.
**Never put the key itself in the file.**

**Token limit parameter.** By default the adapter sends `max_tokens`. OpenAI's
GPT-5-class / reasoning models on `api.openai.com` instead require
`max_completion_tokens` and reject `max_tokens` with HTTP 400. For those, set:

```toml
max_tokens       = 4096
max_tokens_param = "max_completion_tokens"
```

Set `max_tokens = 0` to omit the cap entirely. Most other OpenAI-compatible
endpoints (DeepSeek, Groq, OpenRouter, local servers) accept plain `max_tokens`.

**HTTPS is enforced for keyed endpoints.** If `api_key_env` resolves to a real
key, the adapter refuses a non-`https://` `base_url` (localhost is exempt) so a
mistyped `http://` URL can't leak your key in cleartext.

Set it before running:

```bash
# macOS / Linux
export DEEPSEEK_API_KEY=sk-...

# Windows PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."
```

### Verified base URLs

These are common endpoints (confirm the exact model id against each provider's
current docs — model names change often):

| Provider | `base_url` | typical `api_key_env` |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| Anthropic (OpenAI-compat) | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |
| DeepSeek | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| xAI (Grok API) | `https://api.x.ai/v1` | `XAI_API_KEY` |
| Mistral | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |

### Local model servers (no key)

Set `api_key_env = ""` for keyless local endpoints:

```toml
[providers.ollama]
type        = "openai"
base_url    = "http://localhost:11434/v1"
api_key_env = ""
model       = "llama3.3"
```

Works the same way for LM Studio (`http://localhost:1234/v1`), vLLM,
llama.cpp's server, etc.

### Custom headers

Some gateways want extra headers (e.g. OpenRouter's ranking headers). Add them
with `extra_headers`:

```toml
[providers.openrouter]
type          = "openai"
base_url      = "https://openrouter.ai/api/v1"
api_key_env   = "OPENROUTER_API_KEY"
model         = "anthropic/claude-sonnet-5"
extra_headers = { "HTTP-Referer" = "https://your-app.example", "X-Title" = "ai-orchestra" }
```

---

## Common fields (both types)

| Field | Meaning |
|---|---|
| `role` | Free text. `verify.py` picks default critics from providers whose role contains `review`. Also just a human note. |
| `enabled` | `false` keeps the block but makes the provider unusable until you flip it. |
| `default_timeout` | Seconds; overridable per call with `--timeout`. |
| `model` | Default model; overridable per call with `--model`. |

## Verifying your config

```bash
python scripts/dispatch.py --list     # shows every provider, type, enabled state
python scripts/config.py              # same, with resolved home/data paths
```

Then a real call:

```bash
echo "hello" | python scripts/dispatch.py <name>
```
