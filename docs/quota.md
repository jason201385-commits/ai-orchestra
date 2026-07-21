# 用量與額度（誠實記帳）

這裡有兩層機制。一層對**所有人**都有效；另一層則是**選用的、盡力而為的加值功能**。

## 1. 記帳檔 — 通用、永遠正確

每一次 `dispatch.py` 呼叫都會在 `data/ledger.jsonl` 附加一行：

```json
{"ts":"2026-07-21T14:00:00+08:00","provider":"deepseek","type":"openai",
 "label":"triage","duration_s":2.1,"output_chars":812,"ok":true,"rc":0, ...}
```

它只記錄**中繼資料** — 供應商、耗時、token 數量（在供應商有回傳時）、成功／失敗以及失敗的*種類*。它從不儲存你的提示詞文字，也從不儲存任何金鑰。

`usage_report.py` 把記帳檔轉成一張表格（呼叫次數、成功率、token、主要失敗原因）以及一份離線 HTML 儀表板：

```bash
python scripts/usage_report.py            # terminal
python scripts/usage_report.py --html     # writes data/dashboard.html
python scripts/usage_report.py --days 14  # widen the window
```

這份儀表板是單一的靜態檔案，**沒有任何外部資源**（沒有 CDN，沒有遠端字型／圖片）。重新執行該指令即可刷新它。

## 2. 即時剩餘額度探測 — 選用、盡力而為

`quota_probe.py` 會嘗試呈現少數幾家供應商的*真實剩餘用量*。這本質上是脆弱的：多數供應商並不提供剩餘額度的 API，所以這些探測會去讀取本機的應用程式檔案或未公開的端點。**它們對此很誠實** — 任何無法驗證的東西都會顯示為「unknown」，過期的數值會被標記出來、拒絕偽裝成即時資料，非官方來源也會被標注。

```bash
python scripts/quota_probe.py --all       # probe, write data/quota_cache.json
python scripts/usage_report.py            # the report picks the cache up
```

各供應商的實況（這是誠實的邊界 — 會隨著供應商而變動）：

| Provider | What a probe can get | What it cannot |
|---|---|---|
| Codex/ChatGPT | 從一個**非官方**內部端點取得的即時百分比（隨時可能失效）；每次呼叫的精確 token 數 | 沒有官方文件化的 API |
| Claude | **如果**你啟用了 statusline 匯出（見下方）或桌面應用程式的本機歷史檔案（Windows），可取得即時 5h/7d 百分比 | 沙盒化的 `claude -p` 無法自我探測 |
| Grok | 成功／失敗，加上來自本機日誌的「402 = 已耗盡」負向訊號 | 沒有餘額 API |
| Gemini | 本機計數對比免費方案上限 | 沒有剩餘額度 API |
| NVIDIA NIM | 本機計數對比 RPM 上限 | 回應標頭不帶任何額度資訊 |

> 這些探測本質上是**針對 Windows 與桌面應用程式的**（它們會讀取 `%APPDATA%` 檔案與供應商內部資料）。在其他平台上 — 或對於任何沒有探測器的供應商 — 系統會退回到以記帳檔為基礎的本機計數，這種方式永遠可用。探測器的標籤目前是中文／雙語；歡迎提交 PR 把儀表板完整在地化。

### 選用：透過 statusline 取得即時 Claude 額度

如果你使用 Claude Code，`statusline_quota.py` 可兼作 statusline 腳本，並把你即時的 5h/7d 百分比匯出到 `data/claude_quota.json`。在 `~/.claude/settings.json` 中啟用它（把路徑調整成你的 clone 位置）：

```json
"statusLine": {
  "type": "command",
  "command": "python /path/to/ai-orchestra/scripts/statusline_quota.py"
}
```

### 手動回報（後備方案）

當沒有任何自動來源可用時，你可以自己記錄一筆讀數；若有更新的自動來源，它一律會勝出：

```bash
python scripts/quota_probe.py --set claude 5h=22 week_all=61
python scripts/quota_probe.py --show-manual
```

## 這些機制遵循的規則

誠實優先於完整。一個你無法驗證的額度會顯示為 **unknown**，而不是一個令人安心的數字。這正是[反幻覺協定](anti-hallucination.md)套用在主張上的同一套紀律 — 不要把未經驗證的東西呈現成已驗證的。
