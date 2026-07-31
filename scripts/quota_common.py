#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota_common.py — ai-orchestra 額度顯示的共用型別與規則(標準庫,零第三方依賴)

這個模組定義四件事,quota_probe.py / usage_report.py / test_quota_display.py 都必須遵守:

  1. 快取 schema(Entry):每筆數值一定帶 source / sample_at / status / error_kind
  2. 新鮮度分級(唯一來源):
        < 10 分鐘   FRESH    即時(綠)
        10~60 分鐘  RECENT   稍舊(黃)
        1~6 小時    STALE    STALE(橘)
        > 6 小時    EXPIRED  EXPIRED(紅)— 明確標示不得作為決策依據
  3. 秘密過濾:任何要寫檔或印出的字串都先過 redact()
  4. ledger 失敗原因分類(dispatch.py 寫入與 usage_report.py 統計共用同一套規則)

核心設計原則(不可違反):
  誠實優先於完整 — 取不到就標「未知」,不要填 0,也不要拿舊值冒充即時值。

自我檢查:
  python quota_common.py
"""
from __future__ import annotations

import io
import re
import sys
import unicodedata
from datetime import datetime, timezone

SCHEMA_VERSION = 3


# ══════════════════════════════════════════════════════════════════
# 終端排版:中文是全形,len() 會算錯欄寬
# ══════════════════════════════════════════════════════════════════
def dwidth(text) -> int:
    """字串在等寬終端機的顯示寬度(CJK 全形算 2)。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def one_line(text) -> str:
    """壓成單行 — log 訊息常含換行,會把表格撞爛。"""
    return re.sub(r"\s+", " ", str(text)).strip()


def pad(text, width, align="<") -> str:
    """依顯示寬度補空白;超寬則截斷(不切壞全形字)。"""
    text = one_line(text)
    if dwidth(text) > width:
        # 截斷時保留一格當欄位間隔,否則兩欄會黏在一起
        out, w, limit = "", 0, max(1, width - 2)
        for c in text:
            cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
            if w + cw > limit:
                break
            out, w = out + c, w + cw
        text = out + "…"
    gap = " " * max(0, width - dwidth(text))
    return gap + text if align == ">" else text + gap


def force_utf8_stdout():
    """Windows 主控台預設不是 UTF-8,中文會炸。

    只包一次:重複包會讓前一層 wrapper 被 GC 時關掉底層 buffer
    (症狀:ValueError: I/O operation on closed file)。
    """
    if getattr(sys.stdout, "_ai_orchestra_utf8", False):
        return
    if not hasattr(sys.stdout, "buffer"):
        return
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8":
        try:
            sys.stdout._ai_orchestra_utf8 = True
        except AttributeError:
            pass
        return
    wrapper = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    wrapper._ai_orchestra_utf8 = True
    sys.stdout = wrapper


# ══════════════════════════════════════════════════════════════════
# 新鮮度分級 — 這是唯一的分級來源,顯示層不得自己定義門檻
# ══════════════════════════════════════════════════════════════════
FRESH_SECS = 10 * 60          # < 10 分鐘  = 即時
RECENT_SECS = 60 * 60         # 10~60 分鐘 = 稍舊
STALE_SECS = 6 * 60 * 60      # 1~6 小時   = STALE;超過 = EXPIRED

FRESH, RECENT, STALE, EXPIRED, NO_TIME = "FRESH", "RECENT", "STALE", "EXPIRED", "NO_TIME"

# 分級 → 顯示顏色類別(終端與 HTML 共用同一套語意)
FRESHNESS_CLASS = {
    FRESH: "green",
    RECENT: "yellow",
    STALE: "orange",
    EXPIRED: "red",
    NO_TIME: "grey",
}

# ══════════════════════════════════════════════════════════════════
# 合法值域
# ══════════════════════════════════════════════════════════════════
STATUSES = ("live", "stale", "expired", "counted", "derived", "unknown", "error")

# 來源分類 — 讀者一眼要看得出這個數字有多可信
#   app-local-file   桌面 App 自己寫在本機的檔案(非公開格式)
#   unofficial-api   非官方端點(供應商沒有承諾過,隨時可能失效)
#   local-count      本地 ledger 計數(不是官方剩餘量)
#   derived          從離散事件推導(只有二元態,無中間值)
#   manual           人工回報(備援)
SOURCES = ("app-local-file", "unofficial-api", "local-count", "derived", "manual")

ERROR_KINDS = ("auth", "exhausted", "blocked", "endpoint_changed", "none")

# 各錯誤類型的標準修法(顯示層直接引用,不要各自造句)
ERROR_FIX = {
    "auth": "認證已失效 — 重跑一次該 CLI 的互動式登入讓它 refresh token",
    "exhausted": "額度已用盡 — 等重置視窗,或把任務改派其他供應商",
    "blocked": "網路/沙盒阻擋或檔案讀不到 — 檢查連線與路徑後重跑 quota_probe.py --all",
    "endpoint_changed": "來源格式已變(404 / schema 變動)— 該探測失效,需重新偵查",
    "none": "",
}

SOURCE_ZH = {
    "app-local-file": "桌面 App 本機檔",
    "unofficial-api": "非官方端點",
    "local-count": "本地計數",
    "derived": "事件推導",
    "manual": "人工回報",
}


# ══════════════════════════════════════════════════════════════════
# 秘密過濾(Hard Constraint #1:token 絕不入檔、不入 log、不入報表)
# ══════════════════════════════════════════════════════════════════
# 這些欄位名一出現就整個丟掉,不管值長什麼樣
SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "token", "tokens", "api_key", "apikey",
    "authorization", "auth_header", "secret", "password", "client_secret", "cookie",
    "key_prefix", "rt_prefix", "session_key", "bearer", "credentials",
}

# 值層級的樣態過濾(即使欄位名無辜,值長得像 token 也砍掉)
# Prefixes may use '-' (sk-, xai-) or '_' (gsk_ Groq, ghp_/gho_/ghu_/ghs_ GitHub).
_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|xai|nvapi|gsk|ghp|gho|ghu|ghs|pk|rt)[-_][A-Za-z0-9_\-]{8,}", re.I),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.?[A-Za-z0-9_\-]*"),  # JWT
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I),
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{10,}"),  # Google OAuth
]


def redact(value):
    """遞迴過濾字串/dict/list 內的秘密。所有寫檔與輸出前都要過這一關。"""
    if isinstance(value, str):
        out = value
        for pat in _SECRET_PATTERNS:
            out = pat.sub("[REDACTED]", out)
        return out
    if isinstance(value, dict):
        return {
            k: redact(v)
            for k, v in value.items()
            if str(k).lower() not in SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


# ══════════════════════════════════════════════════════════════════
# 時間工具
# ══════════════════════════════════════════════════════════════════
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def epoch_ms_to_iso(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def age_seconds(sample_at_iso, now=None):
    """回傳資料年齡(秒);無法判定回 None。"""
    dt = parse_iso(sample_at_iso)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc).astimezone()
    return (now - dt).total_seconds()


def freshness(sample_at_iso, now=None) -> str:
    """FRESH / RECENT / STALE / EXPIRED / NO_TIME。唯一的分級來源。"""
    age = age_seconds(sample_at_iso, now)
    if age is None:
        return NO_TIME
    if age < FRESH_SECS:
        return FRESH
    if age < RECENT_SECS:
        return RECENT
    if age < STALE_SECS:
        return STALE
    return EXPIRED


def human_age(sample_at_iso, now=None) -> str:
    """一律給實際數字 — 只給顏色不給分鐘數是不可接受的。"""
    age = age_seconds(sample_at_iso, now)
    if age is None:
        return "時間未知"
    if age < 0:
        return "剛剛"
    if age < 90:
        return f"{int(age)} 秒前"
    if age < 5400:
        return f"{int(age // 60)} 分鐘前"
    if age < 172800:
        return f"{age / 3600:.1f} 小時前"
    return f"{age / 86400:.1f} 天前"


def human_reset(reset_at_epoch, now=None) -> str:
    """reset_at 是 epoch 秒。回傳『還有多久 + 幾點』。"""
    if not isinstance(reset_at_epoch, (int, float)) or reset_at_epoch <= 0:
        return "未知"
    dt = datetime.fromtimestamp(reset_at_epoch).astimezone()
    now = now or datetime.now(timezone.utc).astimezone()
    delta = (dt - now).total_seconds()
    if delta <= 0:
        return f"已過({dt:%m-%d %H:%M})"
    if delta < 3600:
        return f"{int(delta // 60)} 分後({dt:%H:%M})"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小時後({dt:%m-%d %H:%M})"
    return f"{delta / 86400:.1f} 天後({dt:%m-%d %H:%M})"


# ══════════════════════════════════════════════════════════════════
# Entry:快取裡的一筆數值
# ══════════════════════════════════════════════════════════════════
def make_entry(provider, metric, *, label="", status="unknown", used_percent=None,
               count=None, text="", reset_at=None, reset_hint="", sample_at=None,
               fetched_at=None, source="local-count", source_detail="",
               error_kind="none", note="", fix="", unofficial=False):
    """建立一筆合法 Entry。所有字串欄位都已過 redact。

    metric        這一列量的是什麼(claude 的 5h / week_all / week_model …)
    used_percent  已用百分比(0-100);未知請留 None,不要填 0
    count         本地計數(調用次數)— 與 used_percent 互斥使用
    sample_at     這個數值「在什麼時間點為真」— 新鮮度以此判定,不是 fetched_at
    fetched_at    我們什麼時候去讀的(僅供稽核,不參與新鮮度)
    """
    assert status in STATUSES, f"bad status: {status}"
    assert source in SOURCES, f"bad source: {source}"
    assert error_kind in ERROR_KINDS, f"bad error_kind: {error_kind}"
    if not fix and error_kind != "none":
        fix = ERROR_FIX.get(error_kind, "")

    if isinstance(used_percent, (int, float)):
        value, value_kind = used_percent, "percent"
    elif isinstance(count, (int, float)):
        value, value_kind = count, "count"
    elif text:
        value, value_kind = redact(one_line(text)), "text"
    else:
        value, value_kind = None, "none"

    fetched = fetched_at or now_iso()
    return {
        "provider": provider,
        "metric": metric,
        "label": redact(label or metric),
        "status": status,
        "value": value,
        "value_kind": value_kind,
        "used_percent": used_percent,
        "count": count,
        "text": redact(one_line(text)),
        "reset_at": reset_at,                   # epoch 秒(可為 None)
        "reset_hint": redact(one_line(reset_hint)),   # 解析不出絕對時間時保留的原字串
        "sample_at": sample_at or fetched,      # 新鮮度以此為準
        "fetched_at": fetched,
        "source": source,
        "source_detail": redact(source_detail),
        "unofficial": bool(unofficial),
        "error_kind": error_kind,
        "note": redact(one_line(note)),
        "fix": redact(one_line(fix)),
    }


def effective_status(entry, now=None) -> str:
    """把儲存的 status 與新鮮度合併成『現在該怎麼顯示』。

    這是誠實原則的實作點:一筆 live 值放到 6 小時後,對外就不再是 live。
    """
    status = entry.get("status", "unknown")
    if status in ("error", "unknown"):
        return status
    if status == "counted":
        return "counted"          # 本地計數本來就不宣稱即時
    if status == "derived":
        return "derived"          # 二元推導,沒有中間值可談
    fresh = freshness(entry.get("sample_at"), now)
    if fresh in (FRESH, RECENT):
        return "live"
    if fresh == STALE:
        return "stale"
    if fresh == EXPIRED:
        return "expired"          # 明確標示,不得當即時值
    return "unknown"


def is_unavailable(entry) -> bool:
    """這家/這一軸現在是不是根本不能用。"""
    pct = entry.get("used_percent")
    return (entry.get("error_kind") in ("exhausted", "auth")
            or (isinstance(pct, (int, float)) and pct >= 100))


def lamp(entry, now=None) -> str:
    """狀態燈(純文字,終端機安全)。"""
    eff = effective_status(entry, now)
    if is_unavailable(entry):
        return "[!]"
    if eff == "error":
        return "[X]"
    if eff == "expired":
        return "[E]"
    if eff == "unknown":
        return "[?]"
    if eff == "stale":
        return "[~]"
    if eff == "counted":
        return "[#]"
    if eff == "derived":
        return "[D]"
    pct = entry.get("used_percent")
    if isinstance(pct, (int, float)) and pct >= 80:
        return "[Y]"
    return "[O]"


LAMP_LEGEND = (
    "[O] 可用  [Y] 已用 >=80%  [!] 不可用/已用盡  [~] STALE(1~6 小時前)  "
    "[E] EXPIRED(>6 小時,不得當決策依據)  [?] 未知  [#] 本地計數  "
    "[D] 事件推導(只有二元態)  [X] 探測失敗"
)

FRESHNESS_LEGEND = (
    "新鮮度分級:<10 分鐘 = 即時(綠)・10~60 分鐘 = 稍舊(黃)・"
    "1~6 小時 = STALE(橘)・>6 小時 = EXPIRED(紅,不得作為決策依據)"
)


def value_text(entry) -> str:
    """把數值變成人看的字。未知就寫『未知』,絕不寫 0。"""
    pct = entry.get("used_percent")
    if isinstance(pct, (int, float)):
        return f"已用 {pct:g}%(剩 {max(0, 100 - pct):g}%)"
    cnt = entry.get("count")
    if isinstance(cnt, (int, float)):
        return f"{entry.get('text') or '本地計數'} {int(cnt)} 次"
    if entry.get("text"):
        return entry["text"]
    return "未知"


def source_text(entry, detail=True) -> str:
    """detail=False 給終端窄欄用;HTML 用完整版(含來源檔案/端點)。"""
    base = SOURCE_ZH.get(entry.get("source"), str(entry.get("source", "?")))
    if entry.get("unofficial") and entry.get("source") != "unofficial-api":
        base += "・非官方"
    src_detail = entry.get("source_detail")
    return f"{base}({src_detail})" if detail and src_detail else base


def freshness_text(entry, now=None) -> str:
    """一定包含實際幾分鐘前 — 不能只給顏色。"""
    fresh = freshness(entry.get("sample_at"), now)
    age = human_age(entry.get("sample_at"), now)
    prefix = "人工回報 " if entry.get("source") == "manual" else ""
    return prefix + {
        FRESH: f"即時({age})",
        RECENT: f"稍舊({age})",
        STALE: f"STALE({age})",
        EXPIRED: f"EXPIRED({age})・不得當決策依據",
        NO_TIME: "時間未知",
    }[fresh]


def reset_text(entry, now=None) -> str:
    """優先給絕對時間;解析不出來就誠實回傳原字串。"""
    at = human_reset(entry.get("reset_at"), now)
    if at != "未知":
        return at
    hint = entry.get("reset_hint")
    return f"原字串:{hint}" if hint else "未知"


# ══════════════════════════════════════════════════════════════════
# ledger 失敗原因分類
# ══════════════════════════════════════════════════════════════════
def classify_ledger_err(err, rc=None):
    """把 ledger 的 err 字串歸類。分不出來就回 'unclassified',不要硬猜。"""
    text = (err or "").strip()
    if not text or text.lower() in ("none", "null"):
        return "unrecorded"          # 舊紀錄沒存錯誤 — 誠實標「未記錄」
    low = text.lower()
    # 這兩種是「送進去的 prompt 根本沒到模型」— 要在通用規則之前判,
    # 否則會被下面的 "empty" / "not found" 規則吃掉,變成看不出根因。
    if "replied as if the prompt was empty" in low:
        return "empty_input_reply"
    if "silently truncate" in low:
        return "prompt_truncated"
    if "402" in low or "balance exhausted" in low or "payment required" in low:
        return "exhausted"
    if "429" in low or "rate limit" in low or "quota" in low or "usage limit" in low:
        return "rate_limited"
    if ("401" in low or "403" in low or "auth" in low or "credential" in low
            or "ineligibletier" in low):
        return "auth"
    if "timeout" in low or "timed out" in low or rc == -1:
        return "timeout"
    if ("no such file" in low or "not found" in low or "winerror 2" in low
            or "is not recognized" in low or rc == -2):
        return "exe_missing"
    if "404" in low or "410" in low:
        return "endpoint_changed"
    if "no output" in low or "no result" in low or "empty" in low:
        return "empty_output"
    if "connection" in low or "ssl" in low or "network" in low or "resolve" in low:
        return "network"
    return "unclassified"


ERR_KIND_ZH = {
    "exhausted": "額度/餘額耗盡",
    "rate_limited": "被限流",
    "auth": "認證失效",
    "timeout": "逾時",
    "exe_missing": "找不到執行檔",
    "endpoint_changed": "端點變更",
    "empty_output": "回傳空內容",
    "empty_input_reply": "模型說沒收到輸入(prompt 沒送到)",
    "prompt_truncated": "prompt 在 argv 被截斷(.cmd shim)",
    "network": "網路錯誤",
    "unclassified": "其他(未分類)",
    "unrecorded": "未記錄(舊紀錄未存錯誤內容)",
}


# ══════════════════════════════════════════════════════════════════
def _self_check():
    from datetime import timedelta
    now = datetime.now(timezone.utc).astimezone()
    cases = [
        (now - timedelta(minutes=3), FRESH),
        (now - timedelta(minutes=9), FRESH),
        (now - timedelta(minutes=11), RECENT),
        (now - timedelta(minutes=59), RECENT),
        (now - timedelta(minutes=61), STALE),
        (now - timedelta(hours=5), STALE),
        (now - timedelta(hours=7), EXPIRED),
        (now - timedelta(days=6), EXPIRED),
    ]
    ok = True
    for dt, want in cases:
        got = freshness(dt.isoformat())
        good = got == want
        ok &= good
        print(f"  {human_age(dt.isoformat()):<14} -> {got:<8} "
              f"{'OK' if good else 'FAIL want ' + want}")
    print("\n  redact 檢查:")
    dirty = {"access_token": "sk-abc123456789",
             "msg": "Bearer eyJhbGciOi.eyJzdWIi.sig 失敗",
             "key_prefix": "AVRM"}
    print(f"    {redact(dirty)}")
    print("\n  " + LAMP_LEGEND)
    print("  " + FRESHNESS_LEGEND)
    return ok


if __name__ == "__main__":
    force_utf8_stdout()
    print(f"quota_common.py 自我檢查(schema v{SCHEMA_VERSION})\n")
    sys.exit(0 if _self_check() else 1)
