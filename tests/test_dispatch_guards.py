#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two dispatch-time guards.


  1. Quota gate: the >80% main-window rule lived only in SKILL.md prose, so
     parallel sessions each passed their own pre-flight check and together
     blew codex past the gate (78% → 84% within an hour on 2026-08-01).
     The check now runs inside dispatch at dispatch time.
  2. Session gate: `claude` via dispatch inside a Claude Code session is a
     known 401/contention anti-pattern (ledger: 60.7% success rate), yet it
     kept being invoked. dispatch now refuses when CLAUDECODE=1 unless
     --allow-claude-in-session is passed.

Quota tests are pure (cache path injected). Session tests run dispatch.py as
a subprocess with empty stdin, so a skipped gate dies at "prompt is required"
— no provider is ever invoked.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dispatch  # noqa: E402

DISPATCH = Path(__file__).resolve().parent.parent / "scripts" / "dispatch.py"


def _cache(entries, generated_at=None):
    return {
        "schema": 3,
        "generated_at": generated_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": entries,
    }


def _entry(provider, metric, used_percent, label=""):
    return {
        "provider": provider,
        "metric": metric,
        "label": label or metric,
        "used_percent": used_percent,
        "status": "live",
    }


class QuotaGateTests(unittest.TestCase):
    def _write(self, payload):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        self.addCleanup(os.unlink, f.name)
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f)
        f.close()
        return f.name

    def test_main_window_above_gate_blocks(self):
        p = self._write(_cache([_entry("codex", "week", 84, "主配額(7d 窗)")]))
        reason = dispatch.quota_gate_block_reason("codex", cache_path=p)
        self.assertIsNotNone(reason)
        self.assertIn("84%", reason)
        self.assertIn("--ignore-quota", reason)

    def test_main_window_below_gate_passes(self):
        p = self._write(_cache([_entry("claude", "5h", 51)]))
        self.assertIsNone(dispatch.quota_gate_block_reason("claude", cache_path=p))

    def test_model_level_metric_never_blocks(self):
        # One model at 100% must not block the provider: a model-level
        # metric is not the account's main window.
        p = self._write(
            _cache([_entry("claude", "week_model:claude-opus-5", 100)])
        )
        self.assertIsNone(dispatch.quota_gate_block_reason("claude", cache_path=p))

    def test_other_provider_entry_never_blocks(self):
        p = self._write(_cache([_entry("codex", "week", 99)]))
        self.assertIsNone(dispatch.quota_gate_block_reason("grok", cache_path=p))

    def test_stale_cache_passes(self):
        old = (datetime.now().astimezone() - timedelta(hours=7)).isoformat(
            timespec="seconds"
        )
        p = self._write(
            _cache([_entry("codex", "week", 99)], generated_at=old)
        )
        self.assertIsNone(dispatch.quota_gate_block_reason("codex", cache_path=p))

    def test_missing_cache_passes(self):
        self.assertIsNone(
            dispatch.quota_gate_block_reason(
                "codex", cache_path="Z:/no/such/file.json"
            )
        )

    def test_corrupt_cache_passes(self):
        p = self._write("{not json")
        self.assertIsNone(dispatch.quota_gate_block_reason("codex", cache_path=p))

    def test_null_percent_passes(self):
        p = self._write(_cache([_entry("grok", "week", None)]))
        self.assertIsNone(dispatch.quota_gate_block_reason("grok", cache_path=p))


class SessionGateTests(unittest.TestCase):
    """Empty stdin means a passed gate dies at 'prompt is required' —
    the provider CLI is never reached."""

    def _run(self, argv, claudecode):
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        if claudecode is not None:
            env["CLAUDECODE"] = claudecode
        r = subprocess.run(
            [sys.executable, str(DISPATCH), *argv],
            input="", capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=60,
        )
        return r.returncode, r.stderr

    def test_claude_inside_session_blocked(self):
        rc, err = self._run(["claude"], claudecode="1")
        self.assertEqual(rc, 2)
        self.assertIn("allow-claude-in-session", err)

    def test_escape_hatch_skips_session_gate(self):
        rc, err = self._run(
            ["claude", "--allow-claude-in-session", "--ignore-quota"],
            claudecode="1",
        )
        self.assertEqual(rc, 2)
        self.assertIn("prompt is required", err)
        self.assertNotIn("allow-claude-in-session", err.split("error:")[-1])

    def test_no_claudecode_env_not_blocked(self):
        rc, err = self._run(["claude", "--ignore-quota"], claudecode=None)
        self.assertEqual(rc, 2)
        self.assertIn("prompt is required", err)

    def test_non_claude_provider_unaffected_by_session(self):
        # codex, because it ships enabled in providers.example.toml — which is
        # what a fresh clone falls back to when config/providers.toml (which is
        # gitignored) does not exist. Naming a provider that ships disabled
        # makes this fail for everyone except whoever wrote it.
        rc, err = self._run(["codex", "--ignore-quota"], claudecode="1")
        self.assertEqual(rc, 2)
        self.assertIn("prompt is required", err)


if __name__ == "__main__":
    unittest.main()
