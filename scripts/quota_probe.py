#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota_probe.py — 多供應商即時額度探測器(誠實版)

用法:
  python quota_probe.py --all              # 探測所有供應商,寫入 data/quota_cache.json
  python quota_probe.py --only codex claude
  python quota_probe.py --json             # 只印 JSON(給程式用)
  python quota_probe.py --set claude 5h=22 week_all=61 week_fable=100
                                           # 人工回報(備援;自動來源較新時自動值優先)
  python quota_probe.py --show-manual
  python quota_probe.py --clear-manual claude

各家取數方式(2026-07-19 實測):
  claude  %APPDATA%/Claude/plan-usage-history.json — 桌面 App 每 5 分鐘自寫
          u.fh = 5-hour used%、u.sd = Weekly all models used%(非公開格式)
          週-個別模型:本機無連續百分比,只能從 claude-code-sessions 的撞牆事件推導
                       (二元:已用盡 / 未知,沒有中間值)
  codex   非官方端點 chatgpt.com/backend-api/wham/usage — 唯一有真實剩餘量的一家
  grok    本機 log 反推(只有負面訊號:402 = 已耗盡);無法自動偵測恢復
  agy     已確認無查詢管道 — 只能本地計數
  gemini  未設定(無 API key、CLI 認證已失效)
  nim     header 確認無配額資訊 — 只能做零 token 認證存活探測 + 本地計數

安全(Hard Constraint #1):token 只在本機讀取、只送往簽發方,
絕不印出、絕不寫入快取、絕不進報表;錯誤訊息一律過 redact()。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quota_common import (  # noqa: E402
    SCHEMA_VERSION, epoch_ms_to_iso, force_utf8_stdout, freshness, human_age,
    make_entry, now_iso, parse_iso, redact,
)

force_utf8_stdout()

from config import DATA_DIR as DATA  # noqa: E402

CACHE = DATA / "quota_cache.json"
MANUAL = DATA / "quota_manual.json"       # 人工回報獨立存放,probe 不會蓋掉
LEDGER = DATA / "ledger.jsonl"

APPDATA = Path(os.environ.get("APPDATA", "") or (Path.home() / "AppData" / "Roaming"))
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CLAUDE_HISTORY = APPDATA / "Claude" / "plan-usage-history.json"
CLAUDE_SESSIONS = APPDATA / "Claude" / "claude-code-sessions"
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
GROK_LOG = Path.home() / ".grok" / "logs" / "unified.jsonl"

ALL_PROVIDERS = ("claude", "codex", "grok", "gemini", "nim")

# 撞牆事件只在這個窗口內採信 — 更早的事件早就重置了,拿出來講是誤導
WALL_MAX_AGE_DAYS = 7


def _short_path(p) -> str:
    """路徑縮寫,避免報表出現一長串使用者目錄。"""
    s = str(p)
    appdata = str(APPDATA)
    if appdata and s.startswith(appdata):
        s = "%APPDATA%" + s[len(appdata):]
    return s.replace(str(Path.home()), "~")


def _classify_http(code) -> str:
    if code in (401, 403):
        return "auth"
    if code in (402, 429):
        return "exhausted"
    if code in (404, 410):
        return "endpoint_changed"
    return "blocked"


# ══════════════════════════════════════════════════════════════════
# CLAUDE — 三軸:5h / 週-全模型 / 週-個別模型
# ══════════════════════════════════════════════════════════════════
CLAUDE_AXES = (
    ("5h", "fh", "5-hour limit"),
    ("week_all", "sd", "Weekly · all models"),
)


def probe_claude():
    entries = []
    entries.extend(_claude_history())
    entries.extend(_claude_week_model())
    tier = _claude_tier()
    if tier:
        entries.append(make_entry(
            "claude", "plan", label="方案層級", status="unknown", text=tier,
            source="app-local-file", source_detail=_short_path(CLAUDE_CREDS),
            note="靜態標籤,不含任何用量數字",
        ))
    return entries


def _claude_history():
    """桌面 App 自寫的額度歷史 — 主來源,純讀檔、零副作用、零 token。"""
    src = _short_path(CLAUDE_HISTORY)

    def blank(kind, note, fix=""):
        return [make_entry(
            "claude", metric, label=label, status="unknown",
            source="app-local-file", source_detail=src, unofficial=True,
            error_kind=kind, note=note, fix=fix,
        ) for metric, _key, label in CLAUDE_AXES]

    if not CLAUDE_HISTORY.exists():
        return blank("blocked", "桌面 App 額度歷史檔不存在",
                     "開一次 Claude 桌面 App 讓它產生,或用 --set 人工回報")
    try:
        doc = json.loads(CLAUDE_HISTORY.read_text(encoding="utf-8"))
        samples = [s for s in (doc.get("samples") or [])
                   if isinstance(s, dict) and isinstance(s.get("t"), (int, float))
                   and isinstance(s.get("u"), dict)]
        if not samples:
            raise ValueError("samples 為空或 schema 非預期")
        # 多 org:各 org 各自取樣,一律取「全部樣本中最新的那一筆」
        latest = max(samples, key=lambda s: s["t"])
        orgs = {s.get("org") for s in samples}
        # t 是 epoch ms;這是「數值在什麼時候為真」— 新鮮度以它判定,
        # 不可用檔案 mtime,也不可因為檔案存在就當即時
        sample_at = epoch_ms_to_iso(latest["t"])
        ver = doc.get("version")
        multi = f";本機有 {len(orgs)} 個 org,顯示最新樣本所屬的那一個" if len(orgs) > 1 else ""

        out = []
        for metric, key, label in CLAUDE_AXES:
            pct = latest["u"].get(key)
            good = isinstance(pct, (int, float))
            out.append(make_entry(
                "claude", metric, label=label,
                status="live" if good else "unknown",
                used_percent=pct if good else None,
                sample_at=sample_at,
                source="app-local-file", source_detail=src, unofficial=True,
                error_kind="none" if good else "endpoint_changed",
                note=(f"桌面 App 每 5 分鐘取樣(欄位 u.{key},version={ver},非公開格式);"
                      f"App 關閉期間數值凍結,實測最大空窗 3.8 小時{multi}"),
                fix="" if good else "檔案格式已變 — 需重新偵查,期間請用 --set 人工回報",
            ))
        return out
    except Exception as e:
        # fail soft:格式改版不能讓整支探測器倒,也不能沉默地印舊值
        return blank("endpoint_changed",
                     f"解析失敗:{redact(str(e))[:140]}",
                     "桌面 App 可能改了檔案格式(version 欄位暗示會改版)— "
                     "需重新偵查,期間請用 --set 人工回報")


_RESET_RE = re.compile(r"resets?\s+(\d{1,2}):(\d{2})\s*([ap]m)?", re.I)


def _parse_reset_hint(text, after_dt):
    """把 'resets 2:50am (Etc/GMT-8)' 解析成 after_dt 之後的第一個絕對時間。

    解析不出來就回 None,由呼叫端保留原字串 — 不硬猜。
    時區採本機時區:實測字串裡的 Etc/GMT-8 即 UTC+8(POSIX 反號),與本機一致。
    """
    m = _RESET_RE.search(text or "")
    if not m or after_dt is None:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    cand = after_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= after_dt:
        cand += timedelta(days=1)
    return cand


def _claude_week_model():
    """週-個別模型:本機沒有連續百分比。

    唯一帶 model 維度的訊號是 claude-code-sessions 的撞牆事件,
    只能推導「某 model 在 errorAt 撞上限、預計某時重置」— 二元態,不是百分比。
    只採用最近 7 天內、且尚未過重置時間的事件。
    """
    src = _short_path(CLAUDE_SESSIONS)
    base_note = ("本機無逐模型百分比;此列由 claude-code-sessions 的撞牆事件推導,"
                 "只有『已用盡 / 未知』兩態,無中間值")

    if not CLAUDE_SESSIONS.exists():
        return [make_entry(
            "claude", "week_model", label="Weekly · 個別模型", status="unknown",
            source="derived", source_detail=src, unofficial=True, error_kind="blocked",
            note=base_note + ";session 目錄不存在",
            fix="精確數值只有桌面 App 畫面看得到 — 讀完用 --set claude week_fable=<%> 回報",
        )]

    now = datetime.now(timezone.utc).astimezone()
    cutoff = now - timedelta(days=WALL_MAX_AGE_DAYS)
    by_model = {}
    scanned = 0
    for f in glob.glob(str(CLAUDE_SESSIONS / "*" / "*" / "local_*.json")):
        scanned += 1
        try:
            j = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue          # 單檔壞掉不影響其他檔
        err, at = j.get("error"), j.get("errorAt")
        if not err or not isinstance(at, (int, float)):
            continue
        if "limit" not in str(err).lower():
            continue
        hit_dt = parse_iso(epoch_ms_to_iso(at))
        if hit_dt is None or hit_dt < cutoff:
            continue          # 太舊 — 早就重置了
        model = redact(str(j.get("model") or "unknown"))
        prev = by_model.get(model)
        if prev is None or hit_dt > prev["hit_dt"]:
            by_model[model] = {"hit_dt": hit_dt, "err": redact(str(err))[:160]}

    active = []
    for model, rec in by_model.items():
        reset_dt = _parse_reset_hint(rec["err"], rec["hit_dt"])
        if reset_dt is not None and reset_dt <= now:
            continue          # 已過重置時間 — 不再宣稱不可用
        active.append((model, rec, reset_dt))

    if not active:
        return [make_entry(
            "claude", "week_model", label="Weekly · 個別模型", status="unknown",
            source="derived", source_detail=src, unofficial=True,
            note=base_note + f";掃描 {scanned} 個 session 檔,近 {WALL_MAX_AGE_DAYS} 天無仍在窗口內的撞牆事件",
            fix="無事件不等於還有額度 — 精確數值請看桌面 App,再用 --set claude week_<model>=<%> 回報",
        )]

    out = []
    for model, rec, reset_dt in sorted(active, key=lambda x: x[1]["hit_dt"], reverse=True):
        out.append(make_entry(
            "claude", f"week_model:{model}", label=f"個別模型 · {model}",
            status="derived", used_percent=100,
            text=f"{model} 已撞上限",
            sample_at=rec["hit_dt"].isoformat(timespec="seconds"),
            reset_at=reset_dt.timestamp() if reset_dt else None,
            reset_hint="" if reset_dt else rec["err"],
            source="derived", source_detail=src, unofficial=True,
            error_kind="exhausted",
            note=base_note + f";撞牆於 {rec['hit_dt']:%m-%d %H:%M}(原訊息:{rec['err']})",
            fix=(f"{model} 已用盡 — 等重置,或改用其他 model / 供應商。"
                 f"確切剩餘百分比只有桌面 App 看得到"),
        ))
    return out


def _claude_tier():
    """只讀方案層級標籤,不碰任何 token 欄位。"""
    try:
        j = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
    except Exception:
        return None
    for node in (j, j.get("claudeAiOauth") or {}):
        if not isinstance(node, dict):
            continue
        tier, sub = node.get("rateLimitTier"), node.get("subscriptionType")
        if tier or sub:
            return redact(" / ".join(str(x) for x in (sub, tier) if x))
    return None


# ══════════════════════════════════════════════════════════════════
# CODEX — 唯一有真實剩餘量的一家(非官方端點)
# ══════════════════════════════════════════════════════════════════
CODEX_SRC = "chatgpt.com/backend-api/wham/usage"


def probe_codex():
    def fail(kind, note, fix=""):
        return [make_entry("codex", "week", label="Codex 主窗", status="error",
                           source="unofficial-api", source_detail=CODEX_SRC, unofficial=True,
                           error_kind=kind, note=note, fix=fix)]

    if not CODEX_AUTH.exists():
        return fail("auth", "~/.codex/auth.json 不存在(未登入 Codex)",
                    "跑一次互動式 codex 登入")
    try:
        auth = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
        tokens = auth.get("tokens", {})
        access, account = tokens.get("access_token", ""), tokens.get("account_id", "")
        if not access:
            return fail("auth", "auth.json 無 access_token", "跑一次互動式 codex 登入")
        req = urllib.request.Request(
            "https://" + CODEX_SRC,
            headers={
                "Authorization": f"Bearer {access}",
                "ChatGPT-Account-Id": account,
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return fail(_classify_http(e.code), f"HTTP {e.code}",
                    "token 過期 — 跑一次互動式 codex 讓它 refresh" if e.code in (401, 403) else "")
    except Exception as e:
        return fail("blocked", redact(str(e))[:160])
    finally:
        # 明確丟棄,不讓 token 進入後續流程
        access = account = auth = tokens = None
        del access, account, auth, tokens

    ts = now_iso()
    plan = redact(str(j.get("plan_type") or j.get("plan") or "?"))
    entries = []

    # 明確路徑解析(不 deep-scan)。rate_limit 是「帳號層級主配額」;
    # additional_rate_limits[] 的每個元素才是「某個具名模型的獨立配額」,
    # 它底下的 primary_window 屬於那個模型,不是帳號的第二個窗口。
    rl = j.get("rate_limit") or {}
    allowed, reached = rl.get("allowed"), rl.get("limit_reached")
    for metric, key, label in (
        ("week", "primary_window", "主配額"),
        ("secondary", "secondary_window", "次要窗口"),
    ):
        win = rl.get(key)
        if isinstance(win, dict):
            entries.append(_codex_window(metric, label, win, plan, ts, allowed, reached))

    for i, extra in enumerate(j.get("additional_rate_limits") or []):
        if not isinstance(extra, dict):
            continue
        name = redact(str(extra.get("limit_name") or extra.get("metered_feature") or f"extra{i}"))
        sub = extra.get("rate_limit") or {}
        win = sub.get("primary_window")
        if isinstance(win, dict):
            entries.append(_codex_window(
                f"model:{name}", f"個別模型 · {name}", win, plan, ts,
                sub.get("allowed"), sub.get("limit_reached"),
            ))

    credits = j.get("credits") or {}
    if credits:
        bal_raw = credits.get("balance")          # 注意:API 回的是字串型別
        has = credits.get("has_credits")
        try:
            bal = float(bal_raw)
        except (TypeError, ValueError):
            bal = None
        entries.append(make_entry(
            "codex", "credits", label="Credits 餘額", status="live",
            text=f"餘額 {bal:g}" if bal is not None else "餘額未知",
            sample_at=ts, source="unofficial-api", source_detail=CODEX_SRC, unofficial=True,
            error_kind="exhausted" if (has is False and not bal) else "none",
            note=f"balance={redact(str(bal_raw))} has_credits={has}",
            fix="" if has else "無 credits 可墊超額 — 只能等主配額窗口重置",
        ))

    if not entries:
        return fail("endpoint_changed", "回應中找不到任何 rate_limit 窗口",
                    "端點 schema 可能已變更,需重新偵查")
    return entries


def _codex_window(metric, label, win, plan, ts, allowed, limit_reached):
    pct = win.get("used_percent")
    secs = win.get("limit_window_seconds") or 0
    hours = secs / 3600 if secs else 0
    span = f"{round(hours)}h 窗" if hours < 24 else f"{round(hours / 24)}d 窗"
    # allowed / limit_reached 是權威布林,比百分比可靠 — 優先當「能不能用」的判準
    exhausted = (limit_reached is True or allowed is False
                 or (isinstance(pct, (int, float)) and pct >= 100))
    good = isinstance(pct, (int, float))
    return make_entry(
        "codex", metric, label=f"{label}({span})",
        status="live" if good else "unknown",
        used_percent=pct if good else None,
        reset_at=win.get("reset_at"), sample_at=ts,
        source="unofficial-api", source_detail=CODEX_SRC, unofficial=True,
        error_kind="exhausted" if exhausted else "none",
        note=f"plan={plan} allowed={allowed} limit_reached={limit_reached}",
        fix="配額已滿 — 等重置,或把任務改派 claude/agy/nim" if exhausted else "",
    )


# ══════════════════════════════════════════════════════════════════
# GROK — 只有負面訊號:log 裡的 402。無法自動偵測恢復。
# ══════════════════════════════════════════════════════════════════
def probe_grok():
    src = _short_path(GROK_LOG)
    count = _ledger_count("grok")

    def base(**kw):
        kw.setdefault("source", "local-count")
        kw.setdefault("source_detail", src)
        return make_entry("grok", "balance", label="Grok Build 餘額", **kw)

    if not GROK_LOG.exists():
        return [base(status="unknown", error_kind="blocked",
                     note=f"找不到 grok log(近 7 日本地計數 {count} 次)",
                     fix="確認 grok CLI 已安裝並跑過")]
    last = None
    try:
        with open(GROK_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "shell.turn." not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("msg", "")).startswith("shell.turn."):
                    last = rec
    except Exception as e:
        return [base(status="error", error_kind="blocked", note=redact(str(e))[:160])]

    if not last:
        return [base(status="unknown", note="log 中無任何調用紀錄")]

    # ⚠️ ctx 內含 key_prefix / rt_prefix — 絕不整包帶出,只取白名單欄位再 redact
    ctx = last.get("ctx") or {}
    code = ctx.get("status_code")
    msg = redact(str(ctx.get("message") or ""))[:140]
    sample_at = str(last.get("ts") or "") or None
    failed = str(last.get("msg", "")).endswith("inference_failed")

    if failed and code == 402:
        return [base(status="error", used_percent=100, sample_at=sample_at,
                     error_kind="exhausted",
                     note=f"最後一次調用回 402:{msg}(近 7 日本地計數 {count} 次)",
                     fix="餘額耗盡且無法自動偵測恢復 — 到 grok.com 儲值/續訂;"
                         "下次真實調用成功才會轉綠")]
    if failed:
        return [base(status="error", sample_at=sample_at,
                     error_kind=_classify_http(code) if isinstance(code, int) else "blocked",
                     note=f"最後一次調用失敗(status={code}):{msg}")]
    return [base(status="unknown", count=count, text="近 7 日本地計數", sample_at=sample_at,
                 note="最後一次調用成功;但餘額本身無查詢管道,只知道『當時可用』",
                 fix="無官方查詢 API — 剩餘量只能靠下次調用驗證")]


# ══════════════════════════════════════════════════════════════════
# AGY / GEMINI / NIM
# ══════════════════════════════════════════════════════════════════
def probe_gemini():
    if os.environ.get("GEMINI_API_KEY"):
        return [make_entry(
            "gemini", "usage", label="Gemini API", status="counted",
            count=_ledger_count("gemini"), text="近 7 日本地計數",
            source="local-count", source_detail="ledger.jsonl",
            note="有 API key,但 Google 未提供查詢剩餘配額的端點",
            fix="剩餘量只能到 aistudio.google.com/rate-limit 網頁看",
        )]
    return [make_entry(
        "gemini", "usage", label="Gemini API", status="unknown",
        source="local-count", source_detail="env / gemini CLI", error_kind="auth",
        note="NOT CONFIGURED — 無 GEMINI_API_KEY,且 CLI OAuth 已失效(2026-07-16 起)",
        fix="要用就設 GEMINI_API_KEY;在那之前不要把它排進分工表",
    )]


def probe_nim():
    """GET /v1/models 是零 token 的認證存活探測(六家裡最乾淨的一支)。"""
    key = os.environ.get("NVIDIA_API_KEY", "")
    count = _ledger_count("nim")
    usage_entry = make_entry(
        "nim", "usage", label="NIM 用量", status="counted", count=count,
        text="近 7 日本地計數",
        source="local-count", source_detail="ledger.jsonl",
        note="已實測:回應 header 完全無 x-ratelimit-* 欄位,剩餘量不可見(免費層 40 RPM)",
        fix="只能本地計數",
    )
    if not key:
        return [make_entry("nim", "auth", label="NVIDIA NIM 認證", status="unknown",
                           source="local-count", source_detail="env",
                           error_kind="auth", note="NVIDIA_API_KEY 未設定"), usage_entry]
    src = "integrate.api.nvidia.com/v1/models"
    try:
        req = urllib.request.Request("https://" + src,
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            n = len(json.loads(resp.read().decode("utf-8")).get("data") or [])
        auth_entry = make_entry(
            "nim", "auth", label="NVIDIA NIM 認證", status="live", sample_at=now_iso(),
            text="認證有效", source="unofficial-api", source_detail=src,
            note=f"可見 {n} 個模型;零 token 探測。注意:認證有效 ≠ 還有額度",
        )
    except urllib.error.HTTPError as e:
        auth_entry = make_entry(
            "nim", "auth", label="NVIDIA NIM 認證", status="error", sample_at=now_iso(),
            source="unofficial-api", source_detail=src,
            error_kind=_classify_http(e.code), note=f"HTTP {e.code}")
    except Exception as e:
        auth_entry = make_entry(
            "nim", "auth", label="NVIDIA NIM 認證", status="error", sample_at=now_iso(),
            source="unofficial-api", source_detail=src,
            error_kind="blocked", note=redact(str(e))[:160])
    finally:
        key = None
        del key
    return [auth_entry, usage_entry]


def _ledger_count(provider, days=7):
    """近 N 日該供應商的調用次數(本地 ledger = 唯一保證正確的事實來源)。"""
    if not LEDGER.exists():
        return 0
    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(days=days)
    n = 0
    try:
        with open(LEDGER, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("provider") != provider:
                    continue
                ts = parse_iso(r.get("ts"))
                if ts is not None and ts >= cutoff:
                    n += 1
    except Exception:
        return n
    return n


PROBES = {
    "claude": probe_claude, "codex": probe_codex, "grok": probe_grok,
    "gemini": probe_gemini, "nim": probe_nim,
}


# ══════════════════════════════════════════════════════════════════
# 人工回報通道(備援)
# ══════════════════════════════════════════════════════════════════
METRIC_ALIASES = {
    "5h": ("5h", "5-hour limit"),
    "five_hour": ("5h", "5-hour limit"),
    "week_all": ("week_all", "Weekly · all models"),
    "week": ("week", "主配額"),
}


def load_manual():
    if not MANUAL.exists():
        return {}
    try:
        return json.loads(MANUAL.read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_manual(provider, pairs):
    store = load_manual()
    ts = now_iso()
    written = []
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"格式錯誤:'{pair}' — 應為 軸=百分比,例如 5h=22")
        raw_metric, raw_val = pair.split("=", 1)
        raw_metric = raw_metric.strip()
        try:
            val = float(raw_val)
        except ValueError:
            raise SystemExit(f"'{raw_val}' 不是數字(百分比 0-100)")
        if not 0 <= val <= 100:
            raise SystemExit(f"{val} 超出範圍 — 百分比要在 0-100")
        metric, label = METRIC_ALIASES.get(raw_metric, (raw_metric, raw_metric))
        if raw_metric.startswith("week_") and raw_metric not in METRIC_ALIASES:
            model = raw_metric[len("week_"):]
            metric, label = f"week_model:{model}", f"個別模型 · {model}"
        store.setdefault(provider, {})[metric] = {
            "used_percent": val, "label": label, "sample_at": ts,
        }
        written.append(f"{label} = {val:g}%")
    MANUAL.parent.mkdir(parents=True, exist_ok=True)
    MANUAL.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def manual_entries(provider):
    out = []
    for metric, rec in (load_manual().get(provider) or {}).items():
        sample_at = rec.get("sample_at") or rec.get("as_of")
        out.append(make_entry(
            provider, metric, label=rec.get("label", metric), status="live",
            used_percent=rec.get("used_percent"), sample_at=sample_at,
            source="manual", source_detail="桌面 App 人工讀數",
            note="人工回報值(備援通道)",
            fix="數字會隨時間走鐘 — 超過 10 分鐘就該重新回報,或等自動來源補上",
        ))
    return out


def merge(probed, manual):
    """合併規則:自動來源存在且較新時,自動值優先;否則人工回報遞補。

    人工是備援,不是預設 — 這是「新鮮度必須即時」的直接後果。
    """
    by_key = {(e["provider"], e["metric"]): e for e in probed}
    for m in manual:
        key = (m["provider"], m["metric"])
        auto = by_key.get(key)
        auto_usable = (auto is not None
                       and auto.get("used_percent") is not None
                       and auto.get("source") != "manual")
        if auto_usable:
            a_ts, m_ts = parse_iso(auto.get("sample_at")), parse_iso(m.get("sample_at"))
            if a_ts is not None and m_ts is not None and a_ts >= m_ts:
                # 自動值較新 → 保留自動值,把人工值降級成註腳供交叉驗證
                auto["note"] = (auto["note"] + f";人工回報值 {m['used_percent']:g}% "
                                               f"({human_age(m.get('sample_at'))})").strip("；;")
                continue
            m["note"] += f"(自動探測值 {auto['used_percent']:g}%,{human_age(auto.get('sample_at'))})"
        by_key[key] = m
    return list(by_key.values())


# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="多供應商即時額度探測器 — 取不到就誠實標未知,絕不拿舊值冒充即時值",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例:\n"
               "  python quota_probe.py --all\n"
               "  python quota_probe.py --only codex claude\n"
               "  python quota_probe.py --set claude 5h=22 week_all=61 week_fable=100\n"
               "  python quota_probe.py --show-manual\n",
    )
    ap.add_argument("--all", action="store_true",
                    help="探測所有供應商(預設行為;單一供應商失敗不影響其他家)")
    ap.add_argument("--only", nargs="+", metavar="PROVIDER", choices=ALL_PROVIDERS,
                    help=f"只探測指定供應商({', '.join(ALL_PROVIDERS)})")
    ap.add_argument("--set", nargs="+", metavar="ARG",
                    help="人工回報(備援):--set claude 5h=22 week_all=61 week_fable=100")
    ap.add_argument("--show-manual", action="store_true", help="顯示目前的人工回報值")
    ap.add_argument("--clear-manual", metavar="PROVIDER", help="清掉某供應商的人工回報")
    ap.add_argument("--json", action="store_true", help="只輸出 JSON")
    args = ap.parse_args()

    if args.clear_manual:
        store = load_manual()
        store.pop(args.clear_manual, None)
        MANUAL.parent.mkdir(parents=True, exist_ok=True)
        MANUAL.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已清除 {args.clear_manual} 的人工回報")
        return

    if args.show_manual:
        store = load_manual()
        if not store:
            print("目前沒有任何人工回報值")
            return
        for prov, metrics in store.items():
            for metric, rec in metrics.items():
                ts = rec.get("sample_at") or rec.get("as_of")
                print(f"{prov:<8}{rec.get('label', metric):<26}{rec.get('used_percent'):g}%  "
                      f"({human_age(ts)},{freshness(ts)})")
        return

    if args.set:
        provider, pairs = args.set[0], args.set[1:]
        if not pairs:
            raise SystemExit("用法:--set <provider> 軸=值 [軸=值 ...]")
        for line in set_manual(provider, pairs):
            print(f"已記錄人工回報:{provider} {line}")
        print(f"\n寫入 {MANUAL}")
        print("注意:自動來源若存在且較新,報表會優先採用自動值(人工是備援)")
        print("下一步:python usage_report.py")
        return

    targets = args.only or ALL_PROVIDERS
    entries, failed = [], []
    t0 = datetime.now(timezone.utc)
    for name in targets:
        try:
            entries.extend(PROBES[name]())
        except Exception as e:
            # 單一供應商炸掉不中斷其他家 — 但也不能沉默
            failed.append(name)
            entries.append(make_entry(
                name, "probe", label=f"{name} 探測器", status="error",
                source="local-count", error_kind="blocked",
                note=f"探測器自身異常:{redact(str(e))[:160]}",
                fix="這是 quota_probe.py 的 bug,不是供應商問題 — 看 traceback 修",
            ))
    for name in targets:
        entries = merge(entries, manual_entries(name))

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    doc = {
        "schema": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "probed": list(targets),
        "probe_seconds": round(elapsed, 1),
        "entries": redact(entries),   # 最後一道秘密過濾
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return

    print(f"探測完成 {doc['generated_at']} ・ {len(doc['entries'])} 筆 ・ 耗時 {elapsed:.1f}s\n")
    for e in sorted(doc["entries"], key=lambda x: (x["provider"], x["metric"])):
        if isinstance(e.get("used_percent"), (int, float)):
            val = f"{e['used_percent']:g}%"
        elif isinstance(e.get("count"), (int, float)):
            val = f"{e['count']:g} 次"
        else:
            val = "未知"
        print(f"  {e['provider']:<8}{e['label'][:30]:<32}{val:>9}  "
              f"{e['status']:<9}{e['source']:<16}{e['error_kind']}")
    if failed:
        print(f"\n探測器異常的供應商:{', '.join(failed)}(其他家不受影響)")
    print(f"\n快取:{CACHE}\n報表:python usage_report.py")


if __name__ == "__main__":
    main()
