#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the 2026-07-26 codex outage.

Two independent bugs made codex look healthy while the model received nothing:

  1. call_codex put the prompt in argv. On Windows `codex` is an npm .cmd
     shim, so CreateProcess routes the command line through cmd.exe, which
     stops at the first newline — the model saw only line 1 and the shell
     still exited 0.
  2. dispatch scored `rc == 0 and text` as success, so a reply that literally
     said "please paste the content again" was metered as ok=True.

These tests are pure (no network, no provider CLI): they pin the argv/stdin
contract of the codex adapter, the shim guard, and the empty-input detector.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch  # noqa: E402


class CodexTransportTests(unittest.TestCase):
    """The prompt must travel on stdin, never in argv."""

    def _capture(self, prompt, model=""):
        seen = {}

        def fake_run_cmd(cmd, timeout, input_text=None, cwd=None):
            seen["cmd"] = list(cmd)
            seen["input_text"] = input_text
            return 0, (
                '{"type":"item.completed","item":'
                '{"type":"agent_message","text":"ok"}}'
            ), "", 0.1

        original = dispatch.run_cmd
        dispatch.run_cmd = fake_run_cmd
        try:
            result = dispatch.call_codex(
                {"command": "codex"}, prompt, model, 300
            )
        finally:
            dispatch.run_cmd = original
        return seen, result

    def test_prompt_goes_to_stdin_not_argv(self):
        prompt = "第一行 指示\n第二行 內容 📌\n第三行 結束"
        seen, result = self._capture(prompt)
        self.assertEqual(seen["input_text"], prompt)
        self.assertNotIn(prompt, seen["cmd"])
        for arg in seen["cmd"]:
            self.assertNotIn("\n", arg, f"multi-line argv element: {arg!r}")
        self.assertEqual(result[1], "ok")

    def test_dash_positional_is_last(self):
        seen, _ = self._capture("多行\n提示詞內容")
        self.assertEqual(seen["cmd"][-1], "-")
        self.assertIn("--json", seen["cmd"])
        self.assertIn("exec", seen["cmd"])

    def test_model_flag_keeps_dash_last(self):
        seen, _ = self._capture("多行\n提示詞內容", model="gpt-5.5-codex")
        self.assertEqual(seen["cmd"][-1], "-")
        self.assertEqual(seen["cmd"][-3:-1], ["-m", "gpt-5.5-codex"])

    def test_empty_stdin_is_never_sent(self):
        """The old adapter passed input_text="" — that was the whole bug."""
        seen, _ = self._capture("一行提示")
        self.assertNotEqual(seen["input_text"], "")
        self.assertIsNotNone(seen["input_text"])


class GeminiTransportTests(unittest.TestCase):
    """gemini is an npm .cmd shim too — same rule, prompt on stdin.

    gemini-cli 0.42.0 merges the two inputs as
    `input = input ? stdinData + "\\n\\n" + input : stdinData`
    (bundle/gemini-QSTQ2DBG.js:16084-16090), so an empty -p leaves the stdin
    text as the prompt verbatim while still selecting headless mode.
    """

    def _capture(self, prompt, model=""):
        seen = {}

        def fake_run_cmd(cmd, timeout, input_text=None, cwd=None):
            seen["cmd"] = list(cmd)
            seen["input_text"] = input_text
            return 0, "回答", "", 0.1

        original = dispatch.run_cmd
        dispatch.run_cmd = fake_run_cmd
        try:
            dispatch.call_gemini({"command": "gemini"}, prompt, model, 300)
        finally:
            dispatch.run_cmd = original
        return seen

    def test_prompt_goes_to_stdin_not_argv(self):
        prompt = "第一行 指示\n第二行 內容 📌\n第三行 結束"
        seen = self._capture(prompt)
        self.assertEqual(seen["input_text"], prompt)
        self.assertNotIn(prompt, seen["cmd"])
        for arg in seen["cmd"]:
            self.assertNotIn("\n", arg, f"multi-line argv element: {arg!r}")

    def test_empty_prompt_flag_selects_headless_without_appending(self):
        seen = self._capture("多行\n提示詞")
        self.assertIn("-p", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("-p") + 1], "")

    def test_model_flag_still_passed(self):
        seen = self._capture("多行\n提示詞", model="gemini-3-pro")
        self.assertEqual(seen["cmd"][-2:], ["-m", "gemini-3-pro"])
        self.assertIn("--skip-trust", seen["cmd"])


class ShimTruncationGuardTests(unittest.TestCase):
    """No adapter may hand a multi-line argument to a .cmd/.bat shim."""

    def test_flags_newline_argument_to_cmd_shim(self):
        risk = dispatch.argv_newline_truncation_risk(
            [r"C:\npm\codex.CMD", "exec", "line one\nline two"]
        )
        self.assertTrue(risk)
        self.assertIn("stdin", risk)

    def test_flags_carriage_return_too(self):
        self.assertTrue(dispatch.argv_newline_truncation_risk(
            [r"C:\npm\tool.bat", "a\r\nb"]
        ))

    def test_single_line_argv_is_allowed(self):
        self.assertEqual("", dispatch.argv_newline_truncation_risk(
            [r"C:\npm\codex.CMD", "exec", "100% 完成 & 通過 | 管線 ^ < >"]
        ))

    def test_real_exe_is_not_restricted(self):
        self.assertEqual("", dispatch.argv_newline_truncation_risk(
            [r"C:\Users\me\.grok\bin\grok.exe", "-p", "line one\nline two"]
        ))

    def test_run_cmd_refuses_instead_of_truncating(self):
        rc, out, err, _dur = dispatch.run_cmd(
            [r"C:\npm\codex.CMD", "exec", "line one\nline two"], 30
        )
        if dispatch.os.name != "nt":
            self.skipTest("shim truncation is a Windows-only failure mode")
        self.assertEqual(rc, -3)
        self.assertEqual(out, "")
        self.assertIn("truncate", err)


class EmptyInputReplyTests(unittest.TestCase):
    """A reply that says "I got nothing" must not be metered as a success."""

    LONG_PROMPT = "內" * 2000

    def test_detects_the_replies_that_started_this(self):
        for reply in (
            "請貼上完整內容,我才能幫你分析。",
            "這則訊息中沒有出現你說的文章,請重新貼上。",
            "我這邊沒有看到任何內容,可以再傳一次嗎?",
            "Your message appears to be empty — please paste the content again.",
            "I didn't receive any text. Could you resend it?",
        ):
            with self.subTest(reply=reply):
                reason = dispatch.detect_empty_input_reply(
                    self.LONG_PROMPT, reply
                )
                self.assertTrue(reason, f"missed: {reply}")
                self.assertIn("replied as if the prompt was empty", reason)

    def test_real_answer_is_not_flagged(self):
        answer = (
            "這份 dispatch.py 有兩個問題。第一,codex 的 prompt 走 argv,"
            "在 Windows 上會被 cmd.exe 截斷;第二,成功判定只看 rc 跟長度。"
            "建議改走 stdin,並加上回覆內容的檢查。" * 4
        )
        self.assertEqual(
            "", dispatch.detect_empty_input_reply(self.LONG_PROMPT, answer)
        )

    def test_short_prompt_may_legitimately_draw_a_request_for_content(self):
        self.assertEqual(
            "",
            dispatch.detect_empty_input_reply("幫我看這個", "請貼上完整內容。"),
        )

    def test_long_reply_that_merely_mentions_pasting_is_not_flagged(self):
        reply = "步驟三:請貼上你的設定檔。" + "接著執行測試並確認輸出。" * 40
        self.assertGreater(len(reply), dispatch.SUSPECT_MAX_REPLY_CHARS)
        self.assertEqual(
            "", dispatch.detect_empty_input_reply(self.LONG_PROMPT, reply)
        )

    def test_empty_reply_is_left_to_the_existing_empty_check(self):
        self.assertEqual(
            "", dispatch.detect_empty_input_reply(self.LONG_PROMPT, "   ")
        )


class ErrKindTests(unittest.TestCase):
    """Both failure modes need their own dashboard bucket."""

    def test_buckets(self):
        import quota_common
        reason = dispatch.detect_empty_input_reply("內" * 2000, "請貼上完整內容。")
        self.assertEqual(
            quota_common.classify_ledger_err(reason, 0), "empty_input_reply"
        )
        _rc, _o, err, _d = dispatch.run_cmd([r"C:\npm\x.cmd", "a\nb"], 5)
        if dispatch.os.name == "nt":
            self.assertEqual(
                quota_common.classify_ledger_err(err, -3), "prompt_truncated"
            )
        for kind in ("empty_input_reply", "prompt_truncated"):
            self.assertIn(kind, quota_common.ERR_KIND_ZH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
