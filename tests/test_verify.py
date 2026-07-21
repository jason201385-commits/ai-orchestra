#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for verify.py — the anti-hallucination gate. Pure functions only
(no network / no subprocess): the verdict parser, the exit-code synthesis, and
default critic selection. These are the whole trust boundary, so they get the
most scrutiny."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import verify  # noqa: E402


class ParseVerdictTests(unittest.TestCase):
    def test_matrix(self):
        cases = [
            ("VERDICT: REFUTED", "REFUTED"),
            ("VERDICT: SUPPORTED", "SUPPORTED"),
            ("VERDICT: UNSUPPORTED", "UNSUPPORTED"),
            ("VERDICT: UNCERTAIN", "UNCERTAIN"),
            ("verdict: supported", "SUPPORTED"),          # case-insensitive
            ("**VERDICT:** REFUTED", "REFUTED"),          # markdown bold
            ("- VERDICT : SUPPORTED", "SUPPORTED"),       # bullet + spaces
            ("VERDICT:", "UNCERTAIN"),                    # empty value, no crash
            ("VERDICT: ", "UNCERTAIN"),
            ("no verdict line here", "UNCERTAIN"),
            ("intro\nVERDICT: REFUTED\ntrailing", "REFUTED"),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(verify.parse_verdict(text), want)

    def test_unsupported_not_shadowed_by_supported(self):
        # \b boundaries must stop SUPPORTED matching inside UNSUPPORTED.
        self.assertEqual(verify.parse_verdict("VERDICT: UNSUPPORTED"), "UNSUPPORTED")

    def test_multi_token_line_biases_negative(self):
        # Off-contract replies naming several tokens resolve to the most negative
        # one — the safe "DO NOT SHIP" direction for a gate.
        self.assertEqual(
            verify.parse_verdict("VERDICT: SUPPORTED (not UNSUPPORTED)"),
            "UNSUPPORTED",
        )
        self.assertEqual(
            verify.parse_verdict("VERDICT: SUPPORTED, definitely not REFUTED"),
            "REFUTED",
        )

    def test_empty_input(self):
        self.assertEqual(verify.parse_verdict(""), "UNCERTAIN")


class SynthesizeTests(unittest.TestCase):
    def code(self, verdicts):
        return verify.synthesize(verdicts)[0]

    def label(self, verdicts):
        return verify.synthesize(verdicts)[1]

    def test_refuted_or_unsupported_blocks(self):
        self.assertEqual(self.code(["REFUTED", "SUPPORTED"]), 2)
        self.assertEqual(self.code(["UNSUPPORTED", "SUPPORTED"]), 2)
        self.assertEqual(self.code(["REFUTED"]), 2)
        # refute wins even amid errors
        self.assertEqual(self.code(["ERROR", "REFUTED"]), 2)

    def test_all_errored_did_not_run(self):
        self.assertEqual(self.code(["ERROR", "ERROR"]), 4)
        self.assertEqual(self.label(["ERROR", "ERROR"]), "DID_NOT_RUN")
        # empty panel is also "did not run", never a pass
        self.assertEqual(self.code([]), 4)

    def test_partial_error_is_inconclusive(self):
        self.assertEqual(self.code(["ERROR", "SUPPORTED"]), 1)
        self.assertEqual(self.label(["ERROR", "SUPPORTED"]), "INCONCLUSIVE")

    def test_all_supported_is_unverified_never_zero(self):
        code, label, _ = verify.synthesize(["SUPPORTED", "SUPPORTED"])
        self.assertEqual(code, 3)
        self.assertEqual(label, "UNVERIFIED")
        self.assertEqual(self.code(["SUPPORTED"]), 3)

    def test_mixed_uncertain_is_inconclusive(self):
        self.assertEqual(self.code(["SUPPORTED", "UNCERTAIN"]), 1)
        self.assertEqual(self.code(["UNCERTAIN", "UNCERTAIN"]), 1)

    def test_never_returns_zero(self):
        import itertools
        toks = ["SUPPORTED", "REFUTED", "UNSUPPORTED", "UNCERTAIN", "ERROR"]
        for n in range(1, 4):
            for combo in itertools.product(toks, repeat=n):
                self.assertNotEqual(verify.synthesize(list(combo))[0], 0,
                                    f"exit 0 must never happen: {combo}")


class DefaultCriticsTests(unittest.TestCase):
    def test_prefers_explicit_adversary_flag(self):
        provs = {
            "claude": {"enabled": True, "adversary": True, "role": "coordinator"},
            "codex": {"enabled": True, "adversary": True, "role": "reviewer"},
            "deepseek": {"enabled": True, "adversary": False, "role": "batch"},
        }
        # explicit path picks adversaries, still excludes the coordinator
        self.assertEqual(verify.default_critics(provs), ["codex"])

    def test_falls_back_to_role_substring(self):
        provs = {
            "claude": {"enabled": True, "role": "coordinator, reviewer"},
            "codex": {"enabled": True, "role": "independent reviewer"},
            "deepseek": {"enabled": True, "role": "cheap batch"},
        }
        # no adversary flags anywhere → role~="review", minus coordinator
        self.assertEqual(verify.default_critics(provs), ["codex"])

    def test_excludes_disabled(self):
        provs = {
            "codex": {"enabled": False, "adversary": True, "role": "reviewer"},
            "grok": {"enabled": True, "adversary": True, "role": "second opinion"},
        }
        self.assertEqual(verify.default_critics(provs), ["grok"])

    def test_empty_when_none_qualify(self):
        provs = {"deepseek": {"enabled": True, "role": "batch"}}
        self.assertEqual(verify.default_critics(provs), [])


if __name__ == "__main__":
    unittest.main()
