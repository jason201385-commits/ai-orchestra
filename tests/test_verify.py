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


class EvidenceSynthesisTests(unittest.TestCase):
    def test_evidence_earns_exit_0(self):
        code, label, _ = verify.synthesize(
            ["SUPPORTED", "SUPPORTED"], evidence_ran=True, evidence_ok=True)
        self.assertEqual(code, 0)
        self.assertEqual(label, "VERIFIED")

    def test_failed_evidence_blocks(self):
        code, label, _ = verify.synthesize(
            ["SUPPORTED"], evidence_ran=True, evidence_ok=False)
        self.assertEqual(code, 2)
        self.assertEqual(label, "DO_NOT_SHIP")

    def test_no_evidence_stays_unverified(self):
        # default (no evidence run) — consensus never earns 0
        self.assertEqual(verify.synthesize(["SUPPORTED", "SUPPORTED"])[0], 3)

    def test_refute_beats_passing_evidence(self):
        # a refutal is decisive even if some evidence passed
        self.assertEqual(
            verify.synthesize(["REFUTED", "SUPPORTED"],
                              evidence_ran=True, evidence_ok=True)[0], 2)


class ParseEvidenceTests(unittest.TestCase):
    def test_typed_evidence(self):
        self.assertEqual(
            verify.parse_evidence("EVIDENCE_TYPE: file\nEVIDENCE_SPEC: src/foo.py"),
            ("file", "src/foo.py"))
        self.assertEqual(
            verify.parse_evidence("**EVIDENCE_TYPE:** cmd\n**EVIDENCE_SPEC:** `pytest -q`"),
            ("cmd", "pytest -q"))

    def test_none_and_missing(self):
        self.assertEqual(
            verify.parse_evidence("EVIDENCE_TYPE: none\nEVIDENCE_SPEC:"), (None, None))
        self.assertEqual(verify.parse_evidence("no evidence lines"), (None, None))


class RunEvidenceTests(unittest.TestCase):
    def test_cmd_skipped_without_optin(self):
        ran, ok, detail = verify.run_one_evidence("cmd", "pytest -q", False, 10)
        self.assertFalse(ran)
        self.assertIn("run-commands", detail)

    def test_file_evidence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.py"
            f.write_text("def foo():\n    pass\n", encoding="utf-8")
            self.assertEqual(verify.run_one_evidence("file", str(f), False, 10)[:2],
                             (True, True))
            self.assertEqual(
                verify.run_one_evidence("file", f'{f} contains "def foo"', False, 10)[:2],
                (True, True))
            self.assertEqual(
                verify.run_one_evidence("file", str(Path(d) / "missing.py"), False, 10)[:2],
                (True, False))


class FromAnswerTests(unittest.TestCase):
    def test_extract_claims_json(self):
        self.assertEqual(verify.extract_claims_json('["a","b","c"]'),
                         ["a", "b", "c"])
        self.assertEqual(verify.extract_claims_json('prose\n["x", "y"]\nmore'),
                         ["x", "y"])
        self.assertEqual(verify.extract_claims_json("no json here"), [])
        self.assertEqual(verify.extract_claims_json('["a", 3, "b", ""]'),
                         ["a", "b"])  # non-strings & blanks dropped
        self.assertEqual(verify.extract_claims_json('["a","b","c"]', max_claims=2),
                         ["a", "b"])

    def test_aggregate(self):
        self.assertEqual(verify.aggregate_from_answer([0, 0, 0]), 0)
        self.assertEqual(verify.aggregate_from_answer([2, 0, 3]), 2)   # any fail
        self.assertEqual(verify.aggregate_from_answer([3, 3]), 3)
        self.assertEqual(verify.aggregate_from_answer([4, 0]), 4)
        self.assertEqual(verify.aggregate_from_answer([0, 3]), 1)      # mixed
        self.assertEqual(verify.aggregate_from_answer([]), 4)


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
