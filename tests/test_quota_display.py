#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_quota_display.py — 用假資料驗證額度顯示層的四條紅線(標準庫 unittest,零依賴)

驗的是「會不會騙人」,不是「跑不跑得動」:

  1. 新鮮度分級   <10分=FRESH / 10~60分=RECENT / 1~6時=STALE / >6時=EXPIRED
                  且過期值不得再被當成 live,顯示文字一定含實際分鐘數
  2. 未知值處理   缺資料顯示「未知」,絕不顯示 0;Claude 缺軸會被補成「未知」列
  3. HTML 跳脫    note / label 裡的 <script> 等字元不得原樣進 HTML
  4. 秘密不外洩   終端輸出與 HTML 都不得出現任何 token 樣態字串

用法:
  python test_quota_display.py            # 跑全部
  python test_quota_display.py -v
"""
from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from quota_common import (  # noqa: E402
    EXPIRED, FRESH, NO_TIME, RECENT, STALE, effective_status, freshness,
    freshness_text, human_age, is_unavailable, lamp, make_entry, redact,
    reset_text, value_text,
)
import usage_report as R  # noqa: E402

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc).astimezone()


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


# 這些字串一旦出現在任何輸出,就是 Hard Constraint #1 破口
TOKEN_SAMPLES = {
    "access": "sk-live-AAAABBBBCCCCDDDD1234",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "xai": "xai-9f8e7d6c5b4a3210zyxw",
    "nvapi": "nvapi-abcdefgh12345678ijkl",
    "bearer": "Bearer AbCdEfGh12345678IjKlMnOp",
    "google_oauth": "ya29.a0AfH6SMBx1234567890abcdef",
    # underscore-prefixed keys (Groq / GitHub) and Google API keys must also redact
    "groq": "gsk_ABCDEFGHIJKLMNOP1234567890abcd",
    "github_pat": "ghp_ABCDEFGHIJKLMNOP1234567890abcd",
    "google_api": "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
}
# token 樣態的通用偵測(比對字面之外再加一層,防止變形洩漏)
# 必須與 quota_common._SECRET_PATTERNS 同步(含底線前綴與 AIza)
TOKEN_SHAPES = [
    re.compile(r"\b(?:sk|xai|nvapi|gsk|ghp|gho|ghu|ghs|pk|rt)[-_][A-Za-z0-9_\-]{8,}", re.I),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I),
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{10,}"),
]


def assert_no_tokens(case, text, where):
    for name, sample in TOKEN_SAMPLES.items():
        case.assertNotIn(sample, text, f"{where} 洩漏了 {name} token 字面值")
    for pat in TOKEN_SHAPES:
        m = pat.search(text)
        case.assertIsNone(m, f"{where} 出現 token 樣態字串:{m.group(0) if m else ''}")


# ══════════════════════════════════════════════════════════════════
class TestFreshnessTiers(unittest.TestCase):
    """1. 新鮮度分級 — 門檻錯了會讓 6 天前的數字被當成即時值。"""

    def test_tier_boundaries(self):
        cases = [
            (ago(seconds=30), FRESH),
            (ago(minutes=9, seconds=59), FRESH),
            (ago(minutes=10, seconds=1), RECENT),
            (ago(minutes=59), RECENT),
            (ago(minutes=60, seconds=1), STALE),
            (ago(hours=5, minutes=59), STALE),
            (ago(hours=6, seconds=1), EXPIRED),
            (ago(days=6), EXPIRED),
            (None, NO_TIME),
        ]
        for ts, want in cases:
            with self.subTest(ts=ts):
                self.assertEqual(freshness(ts, NOW), want)

    def test_effective_status_demotes_old_live_values(self):
        """關鍵:一筆 status=live 的值放到 6 小時後,對外不得再是 live。"""
        for age, want in ((timedelta(minutes=5), "live"),
                          (timedelta(minutes=45), "live"),
                          (timedelta(hours=3), "stale"),
                          (timedelta(days=6), "expired")):
            e = make_entry("claude", "5h", status="live", used_percent=93,
                           sample_at=(NOW - age).isoformat(), source="app-local-file")
            with self.subTest(age=age):
                self.assertEqual(effective_status(e, NOW), want)

    def test_six_day_old_value_is_flagged_not_trusted(self):
        """重現 2026-07-13 事故:6 天前的 93% 被當即時值印出。"""
        e = make_entry("claude", "5h", status="live", used_percent=93,
                       sample_at=ago(days=6), source="app-local-file")
        self.assertEqual(effective_status(e, NOW), "expired")
        self.assertEqual(lamp(e, NOW), "[E]")
        txt = freshness_text(e, NOW)
        self.assertIn("EXPIRED", txt)
        self.assertIn("不得當決策依據", txt)

    def test_freshness_text_always_shows_real_elapsed_time(self):
        """只給顏色不給分鐘數是不可接受的。"""
        for ts, needle in ((ago(minutes=4), "4 分鐘前"),
                           (ago(minutes=42), "42 分鐘前"),
                           (ago(hours=3), "3.0 小時前")):
            e = make_entry("codex", "week", status="live", used_percent=50, sample_at=ts)
            with self.subTest(ts=ts):
                self.assertIn(needle, freshness_text(e, NOW))
        self.assertEqual(human_age(None, NOW), "時間未知")

    def test_sample_at_not_fetched_at_drives_freshness(self):
        """剛剛讀檔 ≠ 數值是新的 — 桌面 App 關掉時檔案還在,值卻是舊的。"""
        e = make_entry("claude", "week_all", status="live", used_percent=61,
                       sample_at=ago(hours=4), fetched_at=ago(seconds=2),
                       source="app-local-file")
        self.assertEqual(effective_status(e, NOW), "stale")


class TestUnknownHandling(unittest.TestCase):
    """2. 未知值處理 — 缺資料填 0 會直接導致錯誤決策。"""

    def test_missing_value_reads_unknown_never_zero(self):
        e = make_entry("claude", "week_model", status="unknown", source="derived")
        self.assertEqual(value_text(e), "未知")
        self.assertNotIn("0", value_text(e))
        self.assertIsNone(e["value"])
        self.assertEqual(e["value_kind"], "none")

    def test_zero_percent_is_not_confused_with_unknown(self):
        """真的 0% 要顯示 0%,不能被當成未知 — 反向也不能混淆。"""
        e = make_entry("codex", "model:x", status="live", used_percent=0,
                       sample_at=ago(minutes=1))
        self.assertIn("已用 0%", value_text(e))
        self.assertEqual(e["value_kind"], "percent")

    def test_missing_reset_reads_unknown_and_keeps_raw_hint(self):
        e = make_entry("claude", "week_model:f", status="derived", reset_at=None)
        self.assertEqual(reset_text(e, NOW), "未知")
        e2 = make_entry("claude", "week_model:f", status="derived", reset_at=None,
                        reset_hint="resets 2:50am (Etc/GMT-8)")
        self.assertIn("2:50am", reset_text(e2, NOW))

    def test_claude_three_axes_always_present(self):
        """Claude 必須顯示三軸;探測只回一軸時,其餘補『未知』而非消失。"""
        entries = [make_entry("claude", "5h", status="live", used_percent=22,
                              sample_at=ago(minutes=2), source="app-local-file")]
        out = R.ensure_claude_axes(entries)
        metrics = {e["metric"] for e in out}
        self.assertEqual(metrics, {"5h", "week_all", "week_model"})
        for e in out:
            if e["metric"] != "5h":
                self.assertIsNone(e["used_percent"])
                self.assertEqual(value_text(e), "未知")
                self.assertTrue(e["fix"], "未知的軸必須附修法")

    def test_week_model_variant_satisfies_axis(self):
        entries = [
            make_entry("claude", "5h", status="live", used_percent=22, sample_at=ago(minutes=2)),
            make_entry("claude", "week_all", status="live", used_percent=61, sample_at=ago(minutes=2)),
            make_entry("claude", "week_model:claude-fable-5", status="derived", used_percent=100),
        ]
        out = R.ensure_claude_axes(entries)
        self.assertEqual(len(out), 3, "已有 week_model:<model> 就不該再補一列空的")

    def test_unavailable_providers_are_flagged(self):
        grok = make_entry("grok", "balance", status="error", used_percent=100,
                          error_kind="exhausted", sample_at=ago(minutes=5))
        codex = make_entry("codex", "week", status="live", used_percent=100,
                           error_kind="exhausted", sample_at=ago(minutes=1))
        okay = make_entry("nim", "auth", status="live", text="認證有效", sample_at=ago(minutes=1))
        self.assertTrue(is_unavailable(grok))
        self.assertTrue(is_unavailable(codex))
        self.assertFalse(is_unavailable(okay))
        self.assertEqual(lamp(grok, NOW), "[!]")
        self.assertEqual(lamp(codex, NOW), "[!]")
        self.assertTrue(grok["fix"], "不可用的供應商一定要附一行修法")


class TestHtmlEscaping(unittest.TestCase):
    """3. HTML 跳脫 — note 來自外部錯誤訊息,不跳脫就是 XSS + 版面爆炸。"""

    def _render(self, entries):
        return R.build_html(entries, ago(minutes=2), {}, {}, 7, NOW)

    def test_dangerous_chars_are_escaped(self):
        e = make_entry(
            "claude", "5h", label="<img src=x onerror=alert(1)>", status="live",
            used_percent=50, sample_at=ago(minutes=2), source="app-local-file",
            note="<script>alert('xss')</script> & \"quoted\"",
            fix="<b>修法</b>",
        )
        out = self._render([e])
        self.assertNotIn("<script>alert", out)
        self.assertNotIn("<img src=x", out)
        self.assertNotIn("<b>修法</b>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&amp;", out)

    def test_html_is_self_contained_no_external_resources(self):
        """檢查的是「會發出請求的構造」,不是字串裡有沒有網址。

        真實資料的 note 就含 grok 的 error URL(純文字,不會發請求),
        用 'https://' 當判準會誤判;要看的是 script/link/img/src/href/url()。
        """
        entries = [
            make_entry("codex", "week", status="live", used_percent=100,
                       error_kind="exhausted", sample_at=ago(minutes=1),
                       source="unofficial-api", unofficial=True),
            # 刻意在 note 裡塞網址與標籤,確認它們只會變成惰性文字
            make_entry("grok", "balance", status="error", used_percent=100,
                       error_kind="exhausted", sample_at=ago(minutes=2),
                       source="local-count",
                       note="402 Request URL: https://cli-chat-proxy.grok.com/v1/responses",
                       fix="<link rel=stylesheet href='http://evil.example/x.css'>"),
        ]
        out = self._render(entries)

        # 只看「真的標籤」— 被跳脫成 &lt;link 的內容是惰性文字,不該誤判
        allowed = {"html", "head", "meta", "title", "style", "body", "div",
                   "section", "h1", "h2", "b", "code", "span", "br"}
        tags = {m.group(1).lower() for m in re.finditer(r"<\s*/?\s*([a-zA-Z][\w-]*)", out)}
        self.assertTrue(tags <= allowed, f"出現非白名單標籤:{sorted(tags - allowed)}")

        # 屬性只在真實標籤內部檢查
        for tag in re.findall(r"<[a-zA-Z][^>]*>", out):
            for pat, name in ((r"\ssrc\s*=", "src 屬性"), (r"\shref\s*=", "href 屬性")):
                self.assertIsNone(re.search(pat, tag, re.I),
                                  f"標籤含會發出外部請求的屬性({name}):{tag[:80]}")
        # CSS 層級的外連
        for pat, name in ((r"url\s*\(", "CSS url()"), (r"@import", "CSS @import")):
            self.assertIsNone(re.search(pat, out, re.I),
                              f"CSS 不得外連:{name}")
        # 網址仍應以跳脫後的純文字保留(誠實顯示錯誤訊息)
        self.assertIn("cli-chat-proxy.grok.com", out)

    def test_expired_value_not_drawn_as_progress_bar(self):
        """過期值畫成進度條 = 視覺上冒充即時值。"""
        e = make_entry("claude", "5h", status="live", used_percent=93,
                       sample_at=ago(days=6), source="app-local-file")
        out = self._render([e])
        self.assertIn("數值已過期", out)
        self.assertNotIn('style="width:93%"', out)

    def test_fresh_value_is_drawn(self):
        e = make_entry("claude", "5h", status="live", used_percent=22,
                       sample_at=ago(minutes=2), source="app-local-file")
        self.assertIn('style="width:22%"', self._render([e]))

    def test_stale_banner_appears_when_probe_is_old(self):
        e = make_entry("claude", "5h", status="live", used_percent=22, sample_at=ago(days=6))
        out = R.build_html([e], ago(days=6), {}, {}, 7, NOW)
        self.assertIn("不可作為決策依據", out)


class TestNoSecretLeak(unittest.TestCase):
    """4. 秘密不外洩 — token 進了快取/報表就是永久性洩漏。"""

    def _dirty_entries(self):
        return [
            make_entry("codex", "week", label=f"窗口 {TOKEN_SAMPLES['access']}",
                       status="error", error_kind="auth",
                       sample_at=ago(minutes=1), source="unofficial-api",
                       note=f"HTTP 401 {TOKEN_SAMPLES['jwt']}",
                       fix=f"重新登入(舊 token {TOKEN_SAMPLES['bearer']})"),
            make_entry("grok", "balance", status="error", error_kind="exhausted",
                       sample_at=ago(minutes=3), source="local-count",
                       note=f"402 key={TOKEN_SAMPLES['xai']} rt={TOKEN_SAMPLES['google_oauth']}"),
            make_entry("nim", "auth", status="error", error_kind="auth",
                       sample_at=ago(minutes=1), source="unofficial-api",
                       note=f"401 {TOKEN_SAMPLES['nvapi']}"),
        ]

    def test_make_entry_redacts_at_construction(self):
        for e in self._dirty_entries():
            assert_no_tokens(self, repr(e), f"Entry({e['provider']})")

    def test_secret_keys_are_dropped_wholesale(self):
        raw = {"access_token": "whatever", "tokens": {"refresh_token": "x"},
               "key_prefix": "AVRM", "provider": "grok"}
        clean = redact(raw)
        self.assertEqual(clean, {"provider": "grok"})

    def test_terminal_output_has_no_tokens(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            R.print_report(self._dirty_entries(), ago(minutes=1), {}, {}, 7, NOW)
        assert_no_tokens(self, buf.getvalue(), "終端輸出")

    def test_html_output_has_no_tokens(self):
        out = R.build_html(self._dirty_entries(), ago(minutes=1), {}, {}, 7, NOW)
        assert_no_tokens(self, out, "HTML 輸出")

    def test_detector_actually_catches_a_leak(self):
        """反向驗證:如果偵測器抓不到已知 token,前面三個測試都是假的。"""
        with self.assertRaises(AssertionError):
            assert_no_tokens(self, f"leaked {TOKEN_SAMPLES['jwt']}", "自我驗證")
        with self.assertRaises(AssertionError):
            assert_no_tokens(self, f"leaked {TOKEN_SAMPLES['xai']}", "自我驗證")


class TestLedgerSummary(unittest.TestCase):
    """ledger 摘要:沒記錄錯誤類型時要誠實標『未記錄』,不能猜。"""

    def test_unrecorded_errors_are_labelled_honestly(self):
        recs = [
            {"ts": ago(hours=1), "provider": "grok", "ok": False, "err": None},
            {"ts": ago(hours=2), "provider": "grok", "ok": False,
             "err": "status 402 Payment Required"},
            {"ts": ago(hours=3), "provider": "grok", "ok": True},
        ]
        agg = R.aggregate(recs, NOW - timedelta(days=7))
        errs = dict(R.top_errs(agg["grok"]["errs"]))
        self.assertIn("未記錄(舊紀錄未存錯誤內容)", errs)
        self.assertIn("額度/餘額耗盡", errs)
        self.assertEqual(agg["grok"]["calls"], 3)
        self.assertEqual(agg["grok"]["ok"], 1)

    def test_err_kind_field_is_preferred_when_present(self):
        recs = [{"ts": ago(hours=1), "provider": "codex", "ok": False,
                 "err_kind": "timeout", "err": "something weird"}]
        agg = R.aggregate(recs, NOW - timedelta(days=7))
        self.assertEqual(dict(R.top_errs(agg["codex"]["errs"])), {"逾時": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
