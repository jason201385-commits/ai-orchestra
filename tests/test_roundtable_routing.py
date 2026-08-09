#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""roundtable.py 輪次→provider 路由回歸測試(2026-07-31 事故)。

事故:--preset debate --roles "codex=正方,grok=反方,agy=裁判" 三個 single 輪次
全部靜默派給第一家 codex(r01-codex / r02-codex / r03-codex),使用者以為在
跨家抗辯,實際是同家自問自答。

本檔完全 mock 掉 dispatch 路徑(call 與 preflight),不呼叫任何模型、不燒額度。
驗證:
  (a) debate 三個 single 輪次依 --roles 宣告順序派給三個不同 provider
  (b) roles 數量少於 single 輪次數 → 明確報錯中止,且一毛額度都沒燒
  (c) roundtable 的 parallel 輪次仍是每輪全員出動(行為不變)
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import roundtable  # noqa: E402


def fake_preflight(providers):
    """全員就緒,零 subprocess。"""
    return {p: (True, "(mocked ok)") for p in providers}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []  # [(provider, prompt, label), ...] 依實際呼叫序

        def fake_call(provider, prompt, label, timeout):
            self.calls.append((provider, prompt, label))
            return True, f"answer-from-{provider}", 0.1

        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(roundtable, "call", side_effect=fake_call),
            mock.patch.object(roundtable, "preflight", side_effect=fake_preflight),
            mock.patch.object(roundtable, "RUNS", self.runs_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def run_main(self, argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["roundtable.py"] + argv), \
                contextlib.redirect_stdout(out):
            rc = roundtable.main()
        return rc, out.getvalue()

    # (a) 事故本體:debate 三輪必須依宣告序派給三家不同 provider
    def test_debate_routes_rounds_to_roles_in_declared_order(self):
        rc, _ = self.run_main([
            "--preset", "debate", "--topic", "測試議題",
            "--roles", "codex=正方,grok=反方,agy=裁判"])
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in self.calls], ["codex", "grok", "agy"])
        # prompt 也要對到該輪角色(不是拿第一家的角色套到所有輪)
        self.assertIn("【正方】", self.calls[0][1])
        self.assertIn("【反方】", self.calls[1][1])
        self.assertIn("【裁判】", self.calls[2][1])
        # 產出檔名要反映真正執行的 provider(事故時是 r01/r02/r03 全 codex)
        run_dir = next(self.runs_dir.glob("*-debate"))
        files = sorted(f.name for f in run_dir.glob("r*.md"))
        self.assertEqual(files, ["r01-codex.md", "r02-grok.md", "r03-agy.md"])
        man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [r["results"][0]["provider"] for r in man["rounds"]],
            ["codex", "grok", "agy"])

    # (b) roles 不足 → 明確報錯中止,不靜默重用第一家,且不燒任何額度
    def test_debate_insufficient_roles_aborts_before_any_dispatch(self):
        rc, out = self.run_main([
            "--preset", "debate", "--topic", "測試議題",
            "--roles", "codex=正方,grok=反方"])  # 3 個 single 輪只給 2 家
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.calls, [], "roles 不足時不得呼叫任何 provider")
        self.assertIn("單人輪次", out)
        self.assertEqual(list(self.runs_dir.glob("*-debate")), [],
                         "報錯中止不應留下 run 目錄")

    # (c) roundtable 全 parallel:每輪全員出動,行為不可因本次修復而改變
    def test_roundtable_parallel_rounds_dispatch_all_roles_every_round(self):
        rc, _ = self.run_main([
            "--preset", "roundtable", "--topic", "測試議題",
            "--roles", "codex=技術,grok=市場,agy=設計"])
        self.assertEqual(rc, 0)
        wf = roundtable.load_workflows()
        n_rounds = len(wf["roundtable"]["rounds"])
        self.assertEqual(len(self.calls), n_rounds * 3)
        # 逐輪檢查:每輪(以 label 分組)都是三家各出一次
        by_label = {}
        for provider, _, label in self.calls:
            by_label.setdefault(label, []).append(provider)
        self.assertEqual(len(by_label), n_rounds)
        for label, provs in by_label.items():
            self.assertEqual(sorted(provs), ["agy", "codex", "grok"],
                             f"{label} 應該三家各出動一次")

    # (c') consult 也是全 parallel,順手釘住
    def test_consult_parallel_unaffected(self):
        rc, _ = self.run_main([
            "--preset", "consult", "--topic", "測試議題",
            "--roles", "grok=研究,nim=研究"])
        self.assertEqual(rc, 0)
        wf = roundtable.load_workflows()
        n_rounds = len(wf["consult"]["rounds"])
        self.assertEqual(len(self.calls), n_rounds * 2)

    # 混合型 preset(coding:single→parallel→single):single 輪吃掉宣告序,
    # parallel 輪仍全員
    def test_coding_mixed_modes_route_correctly(self):
        rc, _ = self.run_main([
            "--preset", "coding", "--topic", "測試議題",
            "--roles", "codex=規格,grok=審查,agy=驗收"])
        self.assertEqual(rc, 0)
        r1 = [c[0] for c in self.calls if c[2] == "coding-r1"]
        r2 = [c[0] for c in self.calls if c[2] == "coding-r2"]
        r3 = [c[0] for c in self.calls if c[2] == "coding-r3"]
        self.assertEqual(r1, ["codex"])                       # 第 1 個 single → 第 1 家
        self.assertEqual(sorted(r2), ["agy", "codex", "grok"])  # parallel 全員
        self.assertEqual(r3, ["grok"])                        # 第 2 個 single → 第 2 家


if __name__ == "__main__":
    unittest.main()
