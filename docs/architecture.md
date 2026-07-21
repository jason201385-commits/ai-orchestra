# 架構

`ai-orchestra` 刻意保持小巧：一組僅使用 Python 標準函式庫的腳本，圍繞一個設定檔與一個本地記帳檔。沒有伺服器、沒有常駐程式、沒有資料庫，也沒有任何需要 `pip install` 的東西。

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

## 元件

- **`config.py`** — 解析家目錄（`$AI_ORCHESTRA_HOME` 或 repo 根目錄）、`config/` 與 `data/` 目錄，並將 `providers.toml` 載入成一個正規化的 `{name: spec}` 對映。`tomllib` 在 3.11+ 上屬於標準函式庫。

- **`dispatch.py`** — 呼叫供應商的唯一進入點。它會：
  - 根據供應商的 `type`/`kind` 挑選一個轉接器；
  - 從 **stdin** 讀取提示（因此提示永遠不會出現在行程的引數清單中）；
  - 對於 `claude` 轉接器，串流 `stream-json`、印出心跳、強制執行真正的截止期限、在逾時時終止整棵行程樹（Windows Job Object / POSIX process group），並以諮詢式檔案鎖（advisory file lock）串行化並行的 Claude 呼叫；
  - 對於 `openai`，執行單次 `chat/completions` POST；
  - 精確寫入一行記帳，並遮蔽機密。

- **`verify.py`** — 反幻覺交叉查核。它為每個對手（審查方）分別生成一個 `dispatch.py`（因此對手也會被記帳），餵給每一個要求提出證據的對抗式提示，並綜合出一個判定，明確拒絕把「一致但無來源」的認同視為已驗證。

- **`quota_common.py`** — 記帳層共用的誠實原語：新鮮度分級（FRESH / RECENT / STALE / EXPIRED）、機密遮蔽，以及一套穩定的失敗分類規則集。

- **`usage_report.py` / `quota_probe.py` / `statusline_quota.py`** — 記帳與儀表板層。以記帳檔為基礎的計數是通用的；即時探測則是選用且盡力而為（見 [quota.md](quota.md)）。

## 不變量

1. **機密永不留存。** 金鑰來自環境變數，用於簽署一次請求後即丟棄。它們永遠不會被寫進設定、記帳檔、log，或（在可避免的情況下）行程引數。所有記帳／報告輸出都會經過 `redact()`。
2. **提示走 stdin。** 調度器從 stdin 取得提示，因此提示文字不會出現在作業系統的行程清單中。（有些第三方 CLI 仍要求把提示當成引數傳入——所以別把機密放進提示裡。）
3. **誠實優先於完整。** 未知是一等值。過時的資料會被標記，而且永遠不會被當成即時資料呈現。儀表板永遠不會憑空捏造一個零。
4. **軟性失敗，大聲回報。** 單一供應商失敗永遠不會拖垮整趟執行；失敗會被分類並附上修復提示地浮現出來。
5. **沒有隱藏狀態。** 一切都存在 `data/` 與 `config/` 底下的純檔案裡。刪除 `data/` 即可重置；其他都不會被動到。

## 可攜性

- 純標準函式庫，Python 3.11+。
- `dispatch.py` 與 `verify.py` 為跨平台。
- 選用的即時額度探測偏向 Windows／桌面應用程式，在其他環境會退化成記帳檔計數。
- Windows PowerShell 使用者透過 `dispatch.ps1` 管線傳入提示，讓非 ASCII 文字以 UTF-8 存活過管線；調度器也會拒絕明顯的亂碼（mojibake），讓損壞的提示快速失敗，而不是無聲地抵達模型。

## 擴充

- **新增 API 供應商：** 只要加一個 `type = "openai"` 區塊——不用寫程式。
- **介面特殊的新 CLI：** 使用 `kind = "generic"` 搭配一個引數模板，或在 `dispatch.py` 的 `CLI_ADAPTERS` 中新增一個專用轉接器（簽名為 `(spec, prompt, model, timeout) -> (rc, text, err, dur, usage, rate)`）。
