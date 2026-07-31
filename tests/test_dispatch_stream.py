import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch  # noqa: E402

# Minimal provider spec for the Claude adapter (config-driven signature).
CLAUDE_SPEC = {"name": "claude", "type": "cli", "kind": "claude", "command": "claude"}


def process_is_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def immediate_provider_lock(*_args, **_kwargs):
    from contextlib import contextmanager

    @contextmanager
    def unlocked():
        yield 0.0

    return unlocked()


class StreamingJsonCommandTests(unittest.TestCase):
    def test_prompt_normalization_strips_transport_bom_and_preserves_unicode(self):
        self.assertEqual(
            dispatch.normalize_stdin_prompt("\ufeff只讀 測試 中文"),
            "只讀 測試 中文",
        )
        self.assertFalse(
            dispatch.looks_like_question_mark_mojibake("只讀 測試 中文?")
        )
        self.assertFalse(dispatch.looks_like_question_mark_mojibake(
            "Is this correct? What about that?"
        ))
        self.assertFalse(dispatch.looks_like_question_mark_mojibake("Really??"))
        self.assertTrue(dispatch.looks_like_question_mark_mojibake("?? ?? ??"))
        garbled = "?? C:\\writing-agent\\AGENTS.md ... ??????"
        self.assertTrue(dispatch.looks_like_question_mark_mojibake(garbled))
        self.assertFalse(dispatch.should_reject_suspicious_prompt(
            "Why??????", utf8_transport_verified=True
        ))

    def test_question_mark_mojibake_fails_before_provider_call(self):
        # --allow-claude-in-session is deliberate: dispatching `claude` from
        # inside a Claude Code session is refused by an earlier guard, and this
        # suite is normally run from inside one (the project ships as a Claude
        # Code skill). Without the opt-out that guard preempts the check under
        # test, and the failure reads like a mojibake regression that isn't one.
        garbled = "?? C:\\writing-agent\\AGENTS.md ... ??????"
        process = subprocess.run(
            [sys.executable, dispatch.__file__, "claude", "--timeout", "5",
             "--allow-claude-in-session"],
            input=garbled,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(process.returncode, 2)
        self.assertIn("question-mark mojibake", process.stderr)

    def test_non_finite_budget_fails_before_provider_call(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                process = subprocess.run(
                    [
                        sys.executable, dispatch.__file__, "claude",
                        f"--max-budget-usd={value}",
                    ],
                    input="safe ASCII prompt",
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(process.returncode, 2)
                self.assertIn("finite and greater than zero", process.stderr)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "Windows PowerShell wrapper test",
    )
    def test_powershell_wrapper_round_trips_utf8_prompt_via_stdin(self):
        prompt = "只讀 測試 中文 Why??????"
        wrapper = Path(dispatch.__file__).with_name("dispatch.ps1")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_dispatch = tmp_path / "fake_dispatch.py"
            fake_dispatch.write_text(
                "import json, os, sys\n"
                "print(json.dumps({"
                "'prompt': sys.stdin.read(), "
                "'args': sys.argv[1:], "
                "'verified': os.environ.get('AI_ORCHESTRA_UTF8_STDIN_VERIFIED')"
                "}))\n",
                encoding="utf-8",
            )
            encoded = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
            runner = tmp_path / "runner.ps1"
            runner.write_text(
                "$prompt = [Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{encoded}'))\n"
                f"$prompt | & '{wrapper}' claude "
                f"-DispatchPath '{fake_dispatch}' -Timeout 5 -MaxBudgetUsd 0.25\n"
                "exit $LASTEXITCODE\n",
                encoding="utf-8-sig",
            )

            process = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(runner),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )

        self.assertEqual(process.returncode, 0, process.stderr)
        received = json.loads(process.stdout.strip())
        normalized = dispatch.normalize_stdin_prompt(received["prompt"])
        self.assertEqual(normalized.rstrip("\r\n"), prompt)
        self.assertEqual(received["verified"], "1")
        budget_index = received["args"].index("--max-budget-usd")
        self.assertEqual(received["args"][budget_index + 1], "0.25")

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "Windows PowerShell wrapper test",
    )
    def test_powershell_wrapper_rejects_non_finite_budget(self):
        wrapper = Path(dispatch.__file__).with_name("dispatch.ps1")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_dispatch = tmp_path / "must_not_run.py"
            fake_dispatch.write_text(
                "raise SystemExit('fake dispatcher must not run')\n",
                encoding="utf-8",
            )
            runner = tmp_path / "runner.ps1"
            runner.write_text(
                f"$wrapper = '{wrapper}'\n"
                f"$fake = '{fake_dispatch}'\n"
                "foreach ($value in @([double]::NaN, [double]::PositiveInfinity)) {\n"
                "  $rejected = $false\n"
                "  try { 'safe' | & $wrapper claude -DispatchPath $fake -MaxBudgetUsd $value }\n"
                "  catch { $rejected = $true }\n"
                "  if (-not $rejected) { exit 9 }\n"
                "}\n"
                "exit 0\n",
                encoding="utf-8-sig",
            )
            process = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(runner),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )

        self.assertEqual(process.returncode, 0, process.stderr)

    def test_large_bidirectional_pipes_do_not_deadlock(self):
        child_code = """
import json
import sys

print(json.dumps({"type": "system", "subtype": "init", "model": "fake"}), flush=True)
print("x" * 200_000, flush=True)
data = sys.stdin.read()
print(json.dumps({"type": "result", "result": str(len(data))}), flush=True)
"""
        rc, out, err, duration = dispatch.run_streaming_json_cmd(
            [sys.executable, "-c", child_code],
            timeout=10,
            provider="fake",
            input_text="y" * 1_000_000,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["result"], "1000000")
        self.assertIn("non_json_output", err)
        self.assertLess(duration, 10)

    def test_timeout_kills_descendant_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready_path = Path(tmp) / "descendant.pid"
            # The child publishes its PID atomically: write to a temp file, then
            # os.replace onto the handshake path. `write_text` alone creates the
            # file *before* the content lands, so a parent polling exists() can
            # read "" and blow up on int("") — this test used to fail that way
            # roughly one run in four.
            child_code = f"""
import json
import os
from pathlib import Path
import subprocess
import sys

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
ready = Path({str(ready_path)!r})
staging = ready.with_suffix(".tmp")
staging.write_text(str(descendant.pid), encoding="ascii")
os.replace(staging, ready)
print(json.dumps({{"type": "system", "subtype": "init", "model": "fake"}}), flush=True)
"""
            result = {}

            def run_timeout_case():
                result["value"] = dispatch.run_streaming_json_cmd(
                    [sys.executable, "-c", child_code],
                    timeout=3,
                    provider="fake",
                )

            def published_pid():
                """None until a complete PID is readable.

                Readiness is "the content parses", not "the path exists".
                OSError matters as much as ValueError here: on Windows a read
                that races os.replace raises PermissionError, so treating only
                ValueError as "not ready yet" would just trade one flake for
                another.
                """
                try:
                    return int(ready_path.read_text(encoding="ascii"))
                except (OSError, ValueError):
                    return None

            runner = threading.Thread(target=run_timeout_case)
            runner.start()
            ready_deadline = time.monotonic() + 2.5
            descendant_pid = None
            while descendant_pid is None and time.monotonic() < ready_deadline:
                descendant_pid = published_pid()
                if descendant_pid is None:
                    time.sleep(0.02)

            ready_seen = descendant_pid is not None
            descendant_was_running = (
                process_is_running(descendant_pid) if descendant_pid else False
            )
            runner.join(timeout=6)

            self.assertFalse(runner.is_alive(), "timeout runner did not finish")
            self.assertTrue(ready_seen, "descendant never signaled ready")
            self.assertTrue(
                descendant_was_running,
                "descendant was not alive at ready/PID handshake",
            )
            rc, out, err, duration = result["value"]
            gone_deadline = time.monotonic() + 2
            while (
                process_is_running(descendant_pid)
                and time.monotonic() < gone_deadline
            ):
                time.sleep(0.05)

        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("timeout after 3s;"), err)
        self.assertIn("events=system:1", err)
        self.assertLess(duration, 5)
        self.assertFalse(
            process_is_running(descendant_pid),
            f"descendant PID {descendant_pid} survived timeout cleanup",
        )

    def test_result_returns_quickly_after_emit_and_kills_hanging_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "result-child.json"
            child_code = f"""
import json
import os
from pathlib import Path
import time

emitted_at = time.time()
Path({str(state_path)!r}).write_text(json.dumps({{
    "pid": os.getpid(), "emitted_at": emitted_at
}}), encoding="ascii")
print(json.dumps({{"type": "system", "subtype": "init", "model": "fake"}}), flush=True)
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "result-before-hanging-exit",
    "usage": {{"input_tokens": 1, "output_tokens": 1}},
}}), flush=True)
time.sleep(30)
"""
            rc, out, err, duration = dispatch.run_streaming_json_cmd(
                [sys.executable, "-c", child_code],
                timeout=10,
                provider="fake",
            )
            returned_at = time.time()
            self.assertTrue(
                state_path.exists(), "result child did not publish state"
            )
            child_state = json.loads(state_path.read_text(encoding="ascii"))
            child_pid = child_state["pid"]
            cleanup_after_emit = returned_at - child_state["emitted_at"]
            gone_deadline = time.monotonic() + 1
            while process_is_running(child_pid) and time.monotonic() < gone_deadline:
                time.sleep(0.02)

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["result"], "result-before-hanging-exit")
        self.assertEqual(err, "")
        self.assertLess(duration, 10)
        self.assertLess(
            cleanup_after_emit, 1.5,
            f"result cleanup took {cleanup_after_emit:.3f}s after emit",
        )
        self.assertFalse(
            process_is_running(child_pid),
            f"result child PID {child_pid} survived cleanup",
        )

    def test_deadline_drains_result_arriving_during_bounded_grace(self):
        child_code = """
import json
import time

time.sleep(0.15)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "late-queued-result",
}), flush=True)
time.sleep(30)
"""
        with mock.patch.object(
            dispatch, "DEADLINE_DRAIN_GRACE_SECONDS", 1.0
        ):
            rc, out, err, duration = dispatch.run_streaming_json_cmd(
                [sys.executable, "-c", child_code],
                timeout=0.05,
                provider="fake",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["result"], "late-queued-result")
        self.assertEqual(err, "")
        self.assertLess(duration, 2)

    def test_streaming_timeout_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "timeout must be positive"):
            dispatch.run_streaming_json_cmd(
                [sys.executable, "-c", "print('unused')"],
                timeout=0,
                provider="fake",
            )

    def test_timeout_reports_activity_types_without_event_content(self):
        secret_marker = "DO_NOT_EXPOSE_EVENT_CONTENT"
        child_code = f"""
import json
import time

print(json.dumps({{"type": "system", "subtype": "init", "model": "fake"}}), flush=True)
print(json.dumps({{
    "type": "assistant",
    "message": {{"content": [{{
        "type": "tool_use", "name": "Read",
        "input": {{"file_path": "{secret_marker}"}},
    }}]}},
}}), flush=True)
print(json.dumps({{
    "type": "user",
    "message": {{"content": "{secret_marker}"}},
}}), flush=True)
time.sleep(5)
"""
        rc, out, err, _duration = dispatch.run_streaming_json_cmd(
            [sys.executable, "-c", child_code],
            timeout=0.5,
            provider="fake",
        )

        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertIn("events=assistant:1,system:1,user:1", err)
        self.assertIn("tools=Read:1", err)
        self.assertNotIn(secret_marker, err)

    def test_timeout_returns_only_complete_text_not_thinking(self):
        thinking_secret = "PRIVATE_THINKING_MUST_NOT_ESCAPE"
        child_code = f"""
import json
import time

print(json.dumps({{"type": "system", "subtype": "init", "model": "fake"}}), flush=True)
print(json.dumps({{
    "type": "assistant",
    "message": {{"content": [
        {{"type": "thinking", "thinking": "{thinking_secret}"}},
        {{"type": "text", "text": "usable incomplete answer"}}
    ]}},
}}), flush=True)
time.sleep(5)
"""
        rc, out, err, _duration = dispatch.run_streaming_json_cmd(
            [sys.executable, "-c", child_code],
            timeout=0.5,
            provider="fake",
        )

        self.assertEqual(rc, -1)
        self.assertEqual(json.loads(out)["result"], "usable incomplete answer")
        self.assertIn("partial_assistant_text=yes", err)
        self.assertIn("assistant_text_chars=24", err)
        self.assertNotIn(thinking_secret, out)
        self.assertNotIn(thinking_secret, err)

    def test_tool_use_message_is_not_returned_as_incomplete_answer(self):
        child_code = """
import json
import time

print(json.dumps({"type": "system", "subtype": "init", "model": "fake"}), flush=True)
print(json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": "I will inspect the file"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}
    ]},
}), flush=True)
time.sleep(5)
"""
        rc, out, err, _duration = dispatch.run_streaming_json_cmd(
            [sys.executable, "-c", child_code],
            timeout=0.5,
            provider="fake",
        )

        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertNotIn("partial_assistant_text=yes", err)

    def test_later_tool_use_invalidates_older_text_fallback(self):
        child_code = """
import json
import time

print(json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": "stale answer must be invalidated"}
    ]},
}), flush=True)
print(json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": "I will inspect more"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}
    ]},
}), flush=True)
time.sleep(5)
"""
        rc, out, err, _duration = dispatch.run_streaming_json_cmd(
            [sys.executable, "-c", child_code],
            timeout=0.5,
            provider="fake",
        )

        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertIn("assistant_text_chars=0", err)
        self.assertNotIn("partial_assistant_text=yes", err)

    def test_stream_tool_delta_invalidates_older_text_fallback(self):
        child_code = """
import json
import time

print(json.dumps({
    "type": "assistant",
    "message": {"content": [
        {"type": "text", "text": "stale pre-tool text"}
    ]},
}), flush=True)
print(json.dumps({
    "type": "stream_event",
    "event": {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "name": "Read", "id": "tool-1"}
    },
}), flush=True)
time.sleep(5)
"""
        rc, out, err, _duration = dispatch.run_streaming_json_cmd(
            [sys.executable, "-c", child_code],
            timeout=0.5,
            provider="fake",
        )

        self.assertEqual(rc, -1)
        self.assertEqual(out, "")
        self.assertIn("assistant_text_chars=0", err)
        self.assertIn("tools=Read:1", err)
        self.assertNotIn("partial_assistant_text=yes", err)

    def test_provider_lock_serializes_two_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "claude.lock"
            child_code = f"""
import sys
sys.path.insert(0, {str(Path(dispatch.__file__).parent)!r})
import dispatch
with dispatch.provider_serial_lock("claude", 5, {str(lock_path)!r}):
    print("child-acquired", flush=True)
"""
            with dispatch.provider_serial_lock("claude", 5, lock_path):
                child = subprocess.Popen(
                    [sys.executable, "-c", child_code],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.4)
                self.assertIsNone(child.poll())

            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "child-acquired")
            self.assertIn("another request is active; queued", stderr)

    def test_provider_lock_times_out_while_another_process_holds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "claude.lock"
            child_code = f"""
import sys
sys.path.insert(0, {str(Path(dispatch.__file__).parent)!r})
import dispatch
try:
    with dispatch.provider_serial_lock("claude", 0.2, {str(lock_path)!r}):
        print("unexpected-acquire")
except dispatch.ProviderQueueTimeout as exc:
    print(str(exc))
"""
            with dispatch.provider_serial_lock("claude", 5, lock_path):
                child = subprocess.run(
                    [sys.executable, "-c", child_code],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("queue wait exceeded 0s", child.stdout)
            self.assertIn("another request is active; queued", child.stderr)

    def test_provider_lock_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "claude.lock"
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with dispatch.provider_serial_lock("claude", 1, lock_path):
                    raise RuntimeError("boom")

            with dispatch.provider_serial_lock("claude", 1, lock_path):
                pass

    def test_claude_retries_once_only_after_no_result_timeout(self):
        success = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "retry-ok",
            "usage": {"input_tokens": 1},
        })
        with mock.patch.object(
            dispatch, "provider_serial_lock", immediate_provider_lock
        ), mock.patch.object(
            dispatch,
            "run_streaming_json_cmd",
            side_effect=[
                (-1, "", "timeout after 2s", 2.0),
                (0, success, "", 0.1),
            ],
        ) as runner:
            rc, text, err, _duration, usage, _rate = dispatch.call_claude(CLAUDE_SPEC, 
                "test", "fable", 2, retry_no_result=True
            )

        self.assertEqual(runner.call_count, 2)
        self.assertEqual(rc, 0)
        self.assertEqual(text, "retry-ok")
        self.assertEqual(err, "")
        self.assertEqual(usage["dispatch"]["attempts"], 2)
        self.assertTrue(usage["dispatch"]["retried"])
        # The timed-out first attempt may still have incurred unreported usage.
        self.assertFalse(usage["dispatch"]["usage_complete"])

    def test_claude_does_not_retry_timeout_by_default(self):
        with mock.patch.object(
            dispatch, "provider_serial_lock", immediate_provider_lock
        ), mock.patch.object(
            dispatch,
            "run_streaming_json_cmd",
            return_value=(-1, "", "timeout after 2s", 2.0),
        ) as runner:
            rc, text, err, _duration, usage, _rate = dispatch.call_claude(CLAUDE_SPEC, 
                "test", "fable", 2
            )

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(rc, -1)
        self.assertEqual(text, "")
        self.assertEqual(err, "timeout after 2s")
        self.assertEqual(usage["dispatch"]["attempts"], 1)
        self.assertFalse(usage["dispatch"]["retried"])
        self.assertFalse(usage["dispatch"]["usage_complete"])

    def test_queue_time_is_deducted_from_first_attempt_timeout(self):
        from contextlib import contextmanager

        @contextmanager
        def fake_lock(*_args, **_kwargs):
            yield 3.0

        success = json.dumps({"type": "result", "result": "ok"})
        with mock.patch.object(dispatch, "provider_serial_lock", fake_lock), mock.patch.object(
            dispatch,
            "run_streaming_json_cmd",
            return_value=(0, success, "", 0.1),
        ) as runner:
            _rc, _text, _err, _duration, usage, _rate = dispatch.call_claude(CLAUDE_SPEC, 
                "test", "fable", 10
            )

        self.assertEqual(runner.call_args.args[1], 7.0)
        self.assertEqual(usage["dispatch"]["queued_seconds"], 3.0)

    def test_queue_cannot_silently_extend_total_timeout(self):
        from contextlib import contextmanager

        @contextmanager
        def exhausted_lock(*_args, **_kwargs):
            yield 10.0

        with mock.patch.object(
            dispatch, "provider_serial_lock", exhausted_lock
        ), mock.patch.object(dispatch, "run_streaming_json_cmd") as runner:
            rc, text, err, _duration, usage, _rate = dispatch.call_claude(CLAUDE_SPEC, 
                "test", "fable", 10
            )

        runner.assert_not_called()
        self.assertEqual(rc, -4)
        self.assertEqual(text, "")
        self.assertEqual(err, "claude queue wait exceeded 10s")
        self.assertEqual(usage["dispatch"]["attempts"], 0)
        self.assertEqual(usage["dispatch"]["queued_seconds"], 10.0)

    def test_claude_does_not_retry_non_timeout_error(self):
        with mock.patch.object(
            dispatch, "provider_serial_lock", immediate_provider_lock
        ), mock.patch.object(
            dispatch,
            "run_streaming_json_cmd",
            return_value=(1, "", "provider_error_event", 0.1),
        ) as runner:
            rc, text, err, _duration, _usage, _rate = dispatch.call_claude(CLAUDE_SPEC, 
                "test", "fable", 2
            )

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(rc, 1)
        self.assertEqual(text, "")
        self.assertEqual(err, "provider_error_event")

    def test_text_profile_disables_tools_and_accepts_cost_caps(self):
        success = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "text-ok",
            "usage": {"input_tokens": 1},
        })
        with mock.patch.object(
            dispatch, "provider_serial_lock", immediate_provider_lock
        ), mock.patch.object(
            dispatch,
            "run_streaming_json_cmd",
            return_value=(0, success, "", 0.1),
        ) as runner:
            dispatch.call_claude(CLAUDE_SPEC, 
                "health check", "fable", 30,
                profile="text", effort="low", max_budget_usd=0.25,
            )

        cmd = runner.call_args.args[0]
        input_text = runner.call_args.kwargs["input_text"]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertNotIn("--max-turns", cmd)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.25")
        self.assertNotIn("AGENTS.md", input_text)


if __name__ == "__main__":
    unittest.main()
