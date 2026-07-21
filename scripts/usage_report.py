#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usage_report.py — 多 AI 額度顯示表(誠實版)

用法:
  python usage_report.py              # 終端表格
  python usage_report.py --html       # 另外產生 data/dashboard.html(純靜態、無外部資源)
  python usage_report.py --days 14    # 調整 ledger 統計視窗

顯示規則(誠實優先於完整):
  * 每一列都帶「來源」與「新鮮度」,新鮮度一定顯示實際幾分鐘前,不能只給顏色
  * <10 分鐘=即時(綠)/ 10~60 分鐘=稍舊(黃)/ 1~6 小時=STALE(橘)/ >6 小時=EXPIRED(紅)
  * EXPIRED 明確標示「不得作為決策依據」,數值不畫進度條、不冒充即時
  * 資料缺 = 顯示「未知」,絕不填 0
  * 不可用的供應商(grok 402、codex 配額 100%)標「不可用」並附一行修法
  * 非官方端點 / 非公開檔案格式一律標示

數值來自 data/quota_cache.json(跑 quota_probe.py --all 產生)。
ledger.jsonl 是唯一保證正確的事實來源 — 調用次數/成功率一律以它為準。
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quota_common import (  # noqa: E402
    ERR_KIND_ZH, EXPIRED, FRESH, FRESHNESS_CLASS, FRESHNESS_LEGEND, LAMP_LEGEND,
    NO_TIME, RECENT, SCHEMA_VERSION, STALE, classify_ledger_err, effective_status,
    force_utf8_stdout, freshness, freshness_text, human_age, is_unavailable, lamp,
    one_line, pad, redact, reset_text, source_text, value_text,
)

force_utf8_stdout()

from config import DATA_DIR  # noqa: E402

LEDGER = DATA_DIR / "ledger.jsonl"
CACHE = DATA_DIR / "quota_cache.json"
DASH = DATA_DIR / "dashboard.html"

PROVIDER_ORDER = ["claude", "codex", "grok", "gemini", "nim"]

# Claude 三軸的顯示順序 — 缺哪一軸就補一列「未知」,不讓它默默消失
CLAUDE_AXES = [
    ("5h", "5-hour limit"),
    ("week_all", "Weekly · all models"),
    ("week_model", "Weekly · 個別模型"),
]


# ══════════════════════════════════════════════════════════════════
# 資料載入
# ══════════════════════════════════════════════════════════════════
def load_cache():
    if not CACHE.exists():
        return None, []
    try:
        doc = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    if doc.get("schema") != SCHEMA_VERSION:
        # 舊 schema 一律不採信 — 欄位語意可能已變,拿來顯示就是在騙自己
        return doc.get("generated_at"), []
    return doc.get("generated_at"), doc.get("entries") or []


def ensure_claude_axes(entries):
    """Claude 一定要顯示三軸。缺的補「未知」,不是補 0。"""
    have = {e["metric"] for e in entries if e.get("provider") == "claude"}
    out = list(entries)
    for metric, label in CLAUDE_AXES:
        if metric in have:
            continue
        # week_model 會展開成 week_model:<model>,有任何一個就算有
        if metric == "week_model" and any(m.startswith("week_model:") for m in have):
            continue
        out.append({
            "provider": "claude", "metric": metric, "label": label, "status": "unknown",
            "value": None, "value_kind": "none", "used_percent": None, "count": None,
            "text": "", "reset_at": None, "reset_hint": "", "sample_at": None,
            "fetched_at": None, "source": "app-local-file", "source_detail": "",
            "unofficial": True, "error_kind": "blocked",
            "note": "本次探測未取得這一軸",
            "fix": "跑 python quota_probe.py --all;仍缺就用 --set claude 人工回報",
        })
    return out


def load_ledger():
    recs = []
    if not LEDGER.exists():
        return recs
    with open(LEDGER, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def tok_total(u):
    if not u:
        return 0
    for k in ("total_tokens", "tokens_total", "total"):
        if isinstance(u.get(k), (int, float)):
            return int(u[k])
    return sum(int(v) for k, v in u.items()
               if isinstance(v, (int, float))
               and any(x in k for x in ("input", "output", "prompt", "completion")))


def aggregate(recs, since):
    agg = defaultdict(lambda: {"calls": 0, "ok": 0, "tokens": 0, "errs": Counter()})
    for r in recs:
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if ts < since:
            continue
        a = agg[r.get("provider", "?")]
        a["calls"] += 1
        if r.get("ok"):
            a["ok"] += 1
        else:
            # 優先用 dispatch 寫入的 err_kind;舊紀錄則即時分類(可能是 unrecorded)
            a["errs"][r.get("err_kind") or classify_ledger_err(r.get("err"))] += 1
        a["tokens"] += tok_total(r.get("usage"))
    return agg


def top_errs(counter, n=3):
    if not counter:
        return []
    return [(ERR_KIND_ZH.get(k, k), v) for k, v in counter.most_common(n)]


def sort_key(e):
    """Claude 三軸依固定順序,其餘依 metric 字典序。"""
    order = {"5h": 0, "week_all": 1}
    m = e.get("metric", "")
    if m in order:
        return (order[m], m)
    if m.startswith("week_model"):
        return (2, m)
    return (5, m)


# ══════════════════════════════════════════════════════════════════
# 終端輸出
# ══════════════════════════════════════════════════════════════════
def print_report(entries, generated_at, day_agg, win_agg, days, now):
    print(f"═══ 多 AI 額度顯示表 ・ {now:%Y-%m-%d %H:%M} ═══")
    if generated_at:
        fr = freshness(generated_at, now)
        warn = "  ← 請重跑 python quota_probe.py --all" if fr in (STALE, EXPIRED, NO_TIME) else ""
        print(f"探測於 {human_age(generated_at, now)}({fr}){warn}")
    else:
        print("尚無探測資料(或快取 schema 過舊)— 先跑:python quota_probe.py --all")
    print()

    by_prov = defaultdict(list)
    for e in entries:
        by_prov[e["provider"]].append(e)

    # 欄寬用顯示寬度計算(中文全形算 2),否則表格會歪
    W = {"lamp": 4, "prov": 9, "label": 30, "val": 22, "reset": 22, "src": 19, "fresh": 28}
    print(pad("", W["lamp"]) + pad("供應商", W["prov"]) + pad("項目", W["label"])
          + pad("已用 / 計數", W["val"]) + pad("重置", W["reset"])
          + pad("來源", W["src"]) + "新鮮度")
    print("─" * (sum(W.values()) - 2))

    order = ([p for p in PROVIDER_ORDER if p in by_prov]
             + [p for p in sorted(by_prov) if p not in PROVIDER_ORDER])
    problems = []

    for prov in order:
        for e in sorted(by_prov[prov], key=sort_key):
            eff = effective_status(e, now)
            val = value_text(e)
            if eff == "expired" and e.get("used_percent") is not None:
                val = f"{val}(已過期)"
            print(pad(lamp(e, now), W["lamp"]) + pad(prov, W["prov"])
                  + pad(e["label"], W["label"]) + pad(val, W["val"])
                  + pad(reset_text(e, now), W["reset"])
                  + pad(source_text(e, detail=False), W["src"])
                  + freshness_text(e, now))
            detail, note = e.get("source_detail"), one_line(e.get("note") or "")
            tail = f"[{detail}] {note}".strip() if detail else note
            if tail:
                print(pad("", W["lamp"] + W["prov"]) + "└ " + tail)
            if e.get("fix") and (is_unavailable(e) or eff in ("error", "unknown", "expired")):
                problems.append((prov, e, eff))

    print("\n" + LAMP_LEGEND)
    print(FRESHNESS_LEGEND)

    if problems:
        print(f"\n── 不可用 / 需處理({len(problems)} 項)" + "─" * 58)
        for prov, e, eff in problems:
            if is_unavailable(e):
                tag = "不可用"
            elif eff == "expired":
                tag = "已過期"
            else:
                tag = "待確認"
            print(f"  [{tag}] {prov} · {e['label']}")
            print(f"         修法:{one_line(e['fix'])}")

    # ── ledger 事實基準 ──────────────────────────────────────────
    print("\n── 本地 ledger 事實基準(唯一保證正確的來源)" + "─" * 42)
    LW = {"prov": 9, "dc": 7, "dr": 11, "wc": 9, "wr": 13, "tok": 14}
    print(pad("供應商", LW["prov"]) + pad("今日", LW["dc"], ">") + pad("今日成功率", LW["dr"], ">")
          + pad(f"{days}日", LW["wc"], ">") + pad(f"{days}日成功率", LW["wr"], ">")
          + pad("tokens", LW["tok"], ">") + "  失敗原因 Top3")
    print("─" * (sum(LW.values()) + 30))
    provs = sorted(set(list(day_agg) + list(win_agg)))
    if not provs:
        print("  (統計視窗內沒有任何調用紀錄)")
    for p in provs:
        d, w = day_agg.get(p, {}), win_agg.get(p, {})
        dc, wc = d.get("calls", 0), w.get("calls", 0)
        drate = f"{round(d.get('ok', 0) / dc * 100)}%" if dc else "—"
        wrate = f"{round(w.get('ok', 0) / wc * 100)}%" if wc else "—"
        errs = "、".join(f"{k}×{v}" for k, v in top_errs(w.get("errs"))) or "無失敗"
        print(pad(p, LW["prov"]) + pad(dc, LW["dc"], ">") + pad(drate, LW["dr"], ">")
              + pad(wc, LW["wc"], ">") + pad(wrate, LW["wr"], ">")
              + pad(f"{w.get('tokens', 0):,}", LW["tok"], ">") + "  " + errs)
    print(f"\n記帳檔:{LEDGER}")


# ══════════════════════════════════════════════════════════════════
# HTML 儀表板(純靜態、無 CDN、無外部字型/圖片)
# ══════════════════════════════════════════════════════════════════
def css_class(entry, now):
    """卡片狀態燈的顏色類別。不可用永遠是紅的,不管新鮮度。"""
    if is_unavailable(entry):
        return "bad"
    eff = effective_status(entry, now)
    if eff == "error":
        return "bad"
    if eff == "expired":
        return "exp"
    if eff in ("unknown", "derived"):
        return "unk"
    if eff == "stale":
        return "stale"
    if eff == "counted":
        return "count"
    pct = entry.get("used_percent")
    if isinstance(pct, (int, float)) and pct >= 80:
        return "warn"
    return "ok"


def build_html(entries, generated_at, day_agg, win_agg, days, now):
    esc = html.escape
    by_prov = defaultdict(list)
    for e in entries:
        by_prov[e["provider"]].append(e)
    order = ([p for p in PROVIDER_ORDER if p in by_prov]
             + [p for p in sorted(by_prov) if p not in PROVIDER_ORDER])

    cards = []
    for prov in order:
        rows, prov_bad = [], False
        for e in sorted(by_prov[prov], key=sort_key):
            eff = effective_status(e, now)
            fr = freshness(e.get("sample_at"), now)
            pct = e.get("used_percent")
            cls = css_class(e, now)
            prov_bad |= is_unavailable(e)

            # 進度條只在「值可信」時畫;過期值不畫,避免視覺上冒充即時
            if isinstance(pct, (int, float)) and eff in ("live", "stale", "derived"):
                bar = (f'<div class="bar"><div class="fill {cls}" '
                       f'style="width:{min(100, max(0, pct)):g}%"></div></div>')
            elif isinstance(pct, (int, float)):
                bar = '<div class="expired-note">數值已過期,不作為即時值</div>'
            else:
                bar = ""

            val = esc(value_text(e))
            tags = []
            if is_unavailable(e):
                tags.append('<span class="tag bad">不可用</span>')
            if e.get("unofficial") or e.get("source") == "unofficial-api":
                tags.append('<span class="tag un">非官方來源</span>')
            if e.get("source") == "manual":
                tags.append('<span class="tag man">人工回報</span>')
            if e.get("source") == "derived":
                tags.append('<span class="tag der">事件推導・二元</span>')
            if fr == STALE:
                tags.append('<span class="tag stale">STALE</span>')
            if fr == EXPIRED:
                tags.append('<span class="tag exp">EXPIRED</span>')

            reset = reset_text(e, now)
            rows.append(f"""
      <div class="axis {cls}">
        <div class="axis-head"><span class="dot {cls}"></span>
          <span class="axis-name">{esc(e.get('label', ''))}</span>{''.join(tags)}</div>
        <div class="axis-val">{val}</div>
        {bar}
        <div class="meta">來源:{esc(source_text(e))}</div>
        <div class="meta fresh-{FRESHNESS_CLASS.get(fr, 'grey')}">新鮮度:{esc(freshness_text(e, now))}</div>
        <div class="meta">重置:{esc(reset)}</div>
        {f'<div class="note">{esc(e["note"])}</div>' if e.get("note") else ''}
        {f'<div class="fix">修法:{esc(e["fix"])}</div>' if e.get("fix") else ''}
      </div>""")

        d, w = day_agg.get(prov, {}), win_agg.get(prov, {})
        dc, wc = d.get("calls", 0), w.get("calls", 0)
        wrate = f"{round(w.get('ok', 0) / wc * 100)}%" if wc else "—"
        errs = "、".join(f"{esc(k)}×{v}" for k, v in top_errs(w.get("errs"))) or "無失敗"
        cards.append(f"""
    <section class="card{' card-bad' if prov_bad else ''}">
      <h2>{esc(prov)}{'<span class="hdr-bad">不可用</span>' if prov_bad else ''}</h2>
      {''.join(rows)}
      <div class="ledger">
        <div class="ledger-title">本地 ledger(事實基準)</div>
        <div>今日 <b>{dc}</b> 次 ・ 近 {days} 日 <b>{wc}</b> 次 ・ 成功率 <b>{wrate}</b>
          ・ {w.get('tokens', 0):,} tokens</div>
        <div class="meta">失敗原因 Top3:{errs}</div>
      </div>
    </section>""")

    gen_fresh = freshness(generated_at, now) if generated_at else NO_TIME
    banner = ""
    if gen_fresh in (STALE, EXPIRED, NO_TIME):
        aged = esc(human_age(generated_at, now)) if generated_at else "從未探測"
        banner = (f'<div class="banner">⚠ 探測資料為 {aged}({gen_fresh})'
                  f'— 表中數值不可作為決策依據,請重跑 '
                  f'<code>python quota_probe.py --all</code></div>')

    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-orchestra 多 AI 額度顯示表</title><style>
*{{box-sizing:border-box}}
body{{background:#0b0b0d;color:#e8e4dc;margin:0;padding:32px 24px;
 font-family:"Noto Sans TC","Microsoft JhengHei",system-ui,-apple-system,sans-serif;line-height:1.55}}
.wrap{{max-width:1200px;margin:auto}}
h1{{color:#c5a059;font-size:1.3rem;letter-spacing:.18em;margin:0 0 6px}}
.sub{{color:#8f8a80;font-size:.82rem;margin-bottom:20px}}
.banner{{background:#3a1d1d;border:1px solid #7e3b3b;color:#f0c9c9;padding:12px 16px;
 border-radius:6px;font-size:.85rem;margin-bottom:20px}}
.banner code{{background:#00000055;padding:2px 6px;border-radius:3px}}
.legend{{color:#8f8a80;font-size:.76rem;margin-bottom:22px;border-left:2px solid #2a2a2e;
 padding-left:12px;line-height:1.8}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}
.card{{background:#121215;border:1px solid #26262b;border-radius:8px;padding:18px 20px}}
.card-bad{{border-color:#5e2b2b}}
.card h2{{color:#c5a059;font-size:1.05rem;margin:0 0 14px;letter-spacing:.08em;
 text-transform:uppercase;display:flex;align-items:center;gap:8px}}
.hdr-bad{{font-size:.6rem;background:#3a1d1d;color:#f08a86;border:1px solid #7e3b3b;
 border-radius:9px;padding:1px 7px;letter-spacing:.08em;text-transform:none}}
.axis{{border-top:1px solid #212126;padding:11px 0}}
.axis:first-of-type{{border-top:none;padding-top:0}}
.axis-head{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.axis-name{{font-size:.85rem;color:#cfcabf}}
.axis-val{{font-size:1.02rem;font-weight:600;margin:3px 0 2px}}
.dot{{width:8px;height:8px;border-radius:50%;flex:none}}
.dot.ok{{background:#4ea87a}} .dot.warn{{background:#d6a13c}} .dot.bad{{background:#d9534f}}
.dot.unk{{background:#5a5a62}} .dot.count{{background:#5b8fb9}}
.dot.stale{{background:#d98a3c}} .dot.exp{{background:#a03535}}
.axis.ok .axis-val{{color:#7fd3a6}} .axis.warn .axis-val{{color:#e5be6a}}
.axis.bad .axis-val{{color:#f08a86}} .axis.unk .axis-val{{color:#8f8a80}}
.axis.count .axis-val{{color:#8ec2e8}} .axis.stale .axis-val{{color:#e09a55}}
.axis.exp .axis-val{{color:#9c6a6a}}
.bar{{height:5px;background:#232329;border-radius:3px;overflow:hidden;margin:6px 0 4px}}
.fill{{height:5px}} .fill.ok{{background:#4ea87a}} .fill.warn{{background:#d6a13c}}
.fill.bad{{background:#d9534f}} .fill.unk{{background:#5a5a62}} .fill.count{{background:#5b8fb9}}
.fill.stale{{background:#d98a3c}} .fill.exp{{background:#a03535}}
.expired-note{{font-size:.72rem;color:#f08a86;margin:5px 0}}
.meta{{font-size:.72rem;color:#8f8a80}}
.fresh-green{{color:#7fd3a6}} .fresh-yellow{{color:#e5be6a}}
.fresh-orange{{color:#e09a55}} .fresh-red{{color:#f08a86;font-weight:600}}
.note{{font-size:.72rem;color:#7d7870;margin-top:4px;border-left:2px solid #2a2a2e;padding-left:8px}}
.fix{{font-size:.73rem;color:#d6a13c;margin-top:4px}}
.tag{{font-size:.62rem;padding:1px 6px;border-radius:9px;letter-spacing:.05em}}
.tag.un{{background:#3a2f18;color:#d6a13c;border:1px solid #5c4a24}}
.tag.man{{background:#1c3040;color:#8ec2e8;border:1px solid #2d4b63}}
.tag.der{{background:#2b2338;color:#b79ce0;border:1px solid #453764}}
.tag.stale{{background:#3a3218;color:#e5be6a;border:1px solid #5c5024}}
.tag.exp{{background:#3a1d1d;color:#f08a86;border:1px solid #7e3b3b}}
.tag.bad{{background:#3a1d1d;color:#f08a86;border:1px solid #7e3b3b}}
.ledger{{margin-top:12px;border-top:1px solid #26262b;padding-top:10px;font-size:.78rem;color:#a8a39a}}
.ledger-title{{color:#c5a059;font-size:.7rem;letter-spacing:.1em;margin-bottom:4px}}
.foot{{color:#6d6961;font-size:.72rem;margin-top:26px;border-top:1px solid #212126;padding-top:14px}}
</style></head><body><div class="wrap">
<h1>多 AI 額度顯示表</h1>
<div class="sub">產生於 {now:%Y-%m-%d %H:%M:%S} ・ 探測於 {esc(human_age(generated_at, now)) if generated_at else '未探測'}
 ・ 靜態頁面,重新整理不會更新數據(要更新請重跑 quota_probe.py --all 與 usage_report.py --html)</div>
{banner}
<div class="legend">{esc(FRESHNESS_LEGEND)}<br>
{esc(LAMP_LEGEND)}<br>
標 <span class="tag un">非官方來源</span> 者為非官方端點或非公開檔案格式(桌面 App 本機檔),
可能隨供應商更新而失效。標 <span class="tag der">事件推導・二元</span> 者只有「已用盡 / 未知」兩態,
沒有中間百分比。資料缺一律顯示「未知」,不以 0 代替。</div>
<div class="grid">{''.join(cards)}</div>
<div class="foot">數值來源:data/quota_cache.json(quota_probe.py --all 產生)・
 調用次數與成功率來源:data/ledger.jsonl(唯一保證正確的事實來源)・
 本頁不含任何外部資源(無 CDN、無外連字型與圖片)</div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(
        description="多 AI 額度顯示表 — 每個數值都帶來源與新鮮度,取不到就標未知",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="範例:\n"
               "  python usage_report.py\n"
               "  python usage_report.py --html\n"
               "  python usage_report.py --days 14\n",
    )
    ap.add_argument("--html", action="store_true", help="另外產生 data/dashboard.html")
    ap.add_argument("--days", type=int, default=7, help="ledger 統計視窗天數(預設 7)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).astimezone()
    generated_at, entries = load_cache()
    entries = ensure_claude_axes(redact(entries))   # 顯示前最後一道秘密過濾

    recs = load_ledger()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_agg = aggregate(recs, today)
    win_agg = aggregate(recs, now - timedelta(days=args.days))

    print_report(entries, generated_at, day_agg, win_agg, args.days, now)

    if args.html:
        DASH.parent.mkdir(parents=True, exist_ok=True)
        DASH.write_text(build_html(entries, generated_at, day_agg, win_agg, args.days, now),
                        encoding="utf-8")
        print(f"儀表板:{DASH}")


if __name__ == "__main__":
    main()
