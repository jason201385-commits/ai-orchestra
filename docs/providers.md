# 新增供應商

你要在 `config/providers.toml` 裡宣告你手上有的 AI 方案。複製隨附的
範本再編輯它：

```bash
cp providers.example.toml config/providers.toml
```

> **注意：** 隨附範本除了 `claude`/`codex` 以外，每個供應商都預設帶
> `enabled = false`。當你填入自己要用的供應商時，設成
> `enabled = true`（或直接刪掉那行——它預設為 true），否則派工會
> 回報 `provider '<name>' is disabled`。下方片段為求簡潔省略了 `enabled`，
> 也就是假設它已啟用。

供應商的**型別**恰好只有兩種。這兩種合起來，基本上涵蓋了
每一種付費 AI 方案，以及每一種本機模型伺服器。

---

## `type = "cli"` — 一個你已登入的命令列工具

```toml
[providers.codex]
type    = "cli"
kind    = "codex"        # claude | codex | grok | gemini | generic
command = "codex"        # the executable name (or full path)
role    = "independent reviewer"   # free-text; used by verify.py's defaults
enabled = true           # omit or true to enable; false to keep but skip
default_timeout = 300    # seconds
```

`kind` 選擇要用的轉接器：

| `kind` | 適用於 | 說明 |
|---|---|---|
| `claude` | [Claude Code](https://claude.com/claude-code) | 專門打造：串流 JSON、唯讀的 `review` profile、plan 模式、`--effort`、`--max-budget-usd`、跨行程序列化。推薦的總指揮。 |
| `codex` | OpenAI Codex CLI | 解析 `codex exec --json`，擷取 token 用量。 |
| `grok` | xAI Grok CLI | 解析 `grok -p --output-format json`，失敗時退回純文字。 |
| `gemini` | Google Gemini CLI | 偵測「ineligible tier / auth」這類失敗，並告訴你怎麼修。 |
| `generic` | **任何其他 CLI** | 由你自行提供參數樣板——見下方。 |

### `generic` CLI 轉接器

接上*任何*命令列工具。用 `{prompt}`（以及選用的 `{model}`）當作
佔位符：

```toml
[providers.mytool]
type    = "cli"
kind    = "generic"
command = "mytool"
args    = ["chat", "--model", "{model}", "--message", "{prompt}"]
model   = "some-model"
```

如果你**省略** `{prompt}` 佔位符，提示會採取以下其中一種方式：
- 當 `stdin = true` 時，管線送進工具的 **stdin**，或
- 否則附加成**最後一個參數**。

```toml
[providers.stdintool]
type    = "cli"
kind    = "generic"
command = "stdintool"
args    = ["--quiet"]
stdin   = true          # prompt goes to stdin
```

---

## `type = "openai"` — 任何 OpenAI 相容的 HTTP API

單一一個轉接器，`POST {base_url}/chat/completions`，就能適用於
極大範圍的服務：

```toml
[providers.deepseek]
type        = "openai"
base_url    = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"   # the ENV VAR NAME holding your key
model       = "deepseek-chat"
max_tokens  = 4096                 # optional (default 4096)
role        = "cheap batch"
```

API key 是從你在 `api_key_env` 裡指名的環境變數讀取。
**絕對不要把 key 本身放進檔案裡。**

**Token 上限參數。** 轉接器預設送出 `max_tokens`。OpenAI 在
`api.openai.com` 上的 GPT-5 等級／推理模型改為要求
`max_completion_tokens`，並會用 HTTP 400 拒絕 `max_tokens`。對這些模型，設定：

```toml
max_tokens       = 4096
max_tokens_param = "max_completion_tokens"
```

設 `max_tokens = 0` 可完全省略上限。大多數其他 OpenAI 相容
端點（DeepSeek、Groq、OpenRouter、本機伺服器）都接受一般的 `max_tokens`。

**帶 key 的端點會強制使用 HTTPS。** 如果 `api_key_env` 解析出一個真正的
key，轉接器會拒絕非 `https://` 的 `base_url`（localhost 例外），這樣一個
打錯成 `http://` 的 URL 就不會以明文洩漏你的 key。

執行前先設定它：

```bash
# macOS / Linux
export DEEPSEEK_API_KEY=sk-...

# Windows PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."
```

### 已驗證的 base URL

以下是常見端點（請對照各供應商的現行文件確認確切的
model id——模型名稱經常變動）：

| 供應商 | `base_url` | 典型的 `api_key_env` |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| Anthropic（OpenAI 相容） | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |
| DeepSeek | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| xAI（Grok API） | `https://api.x.ai/v1` | `XAI_API_KEY` |
| Mistral | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |

### 本機模型伺服器（不帶 key）

對不需 key 的本機端點設定 `api_key_env = ""`：

```toml
[providers.ollama]
type        = "openai"
base_url    = "http://localhost:11434/v1"
api_key_env = ""
model       = "llama3.3"
```

同樣的方式也適用於 LM Studio（`http://localhost:1234/v1`）、vLLM、
llama.cpp 的伺服器等等。

### 自訂標頭

有些閘道需要額外的標頭（例如 OpenRouter 的排名標頭）。用
`extra_headers` 加上它們：

```toml
[providers.openrouter]
type          = "openai"
base_url      = "https://openrouter.ai/api/v1"
api_key_env   = "OPENROUTER_API_KEY"
model         = "anthropic/claude-sonnet-5"
extra_headers = { "HTTP-Referer" = "https://your-app.example", "X-Title" = "ai-orchestra" }
```

---

## 共用欄位（兩種型別皆適用）

| 欄位 | 意義 |
|---|---|
| `role` | 自由文字。`verify.py` 會從 role 含有 `review` 的供應商挑選預設的對手（審查方）。同時也只是給人看的註記。 |
| `enabled` | `false` 會保留該區塊，但讓該供應商在你翻回來之前無法使用。 |
| `default_timeout` | 秒數；可用 `--timeout` 逐次呼叫覆寫。 |
| `model` | 預設模型；可用 `--model` 逐次呼叫覆寫。 |

## 驗證你的設定

```bash
python scripts/dispatch.py --list     # shows every provider, type, enabled state
python scripts/config.py              # same, with resolved home/data paths
```

接著來一次真正的呼叫：

```bash
echo "hello" | python scripts/dispatch.py <name>
```
