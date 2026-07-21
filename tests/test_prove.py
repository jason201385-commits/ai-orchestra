#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for prove.py check primitives (no network: uses python -c and temp
files). check_url is exercised only for its scheme guard."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import prove  # noqa: E402

PY = sys.executable


class CheckCmdTests(unittest.TestCase):
    def test_exit_code_match(self):
        ok, _ = prove.check_cmd([PY, "-c", "import sys; sys.exit(0)"], expect_rc=0)
        self.assertTrue(ok)
        ok, detail = prove.check_cmd([PY, "-c", "import sys; sys.exit(3)"], expect_rc=0)
        self.assertFalse(ok)
        self.assertIn("exit 3", detail)
        ok, _ = prove.check_cmd([PY, "-c", "import sys; sys.exit(3)"], expect_rc=3)
        self.assertTrue(ok)

    def test_contains_output(self):
        ok, _ = prove.check_cmd([PY, "-c", "print('hello world')"],
                                contains_output="hello")
        self.assertTrue(ok)
        ok, _ = prove.check_cmd([PY, "-c", "print('hello world')"],
                                contains_output="xyz")
        self.assertFalse(ok)

    def test_command_not_found(self):
        ok, detail = prove.check_cmd(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(ok)


class CheckFilesTests(unittest.TestCase):
    def test_exist_and_nonempty(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "good.txt"
            good.write_text("content", encoding="utf-8")
            empty = Path(d) / "empty.txt"
            empty.write_text("", encoding="utf-8")
            missing = Path(d) / "missing.txt"

            self.assertTrue(prove.check_files([str(good)])[0])
            self.assertFalse(prove.check_files([str(empty)])[0])
            self.assertFalse(prove.check_files([str(missing)])[0])
            self.assertFalse(prove.check_files([str(good), str(missing)])[0])

    def test_file_contains(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.py"
            f.write_text("def foo():\n    return 1\n", encoding="utf-8")
            self.assertTrue(prove.check_file_contains(str(f), "def foo")[0])
            self.assertFalse(prove.check_file_contains(str(f), "def bar")[0])
            self.assertFalse(prove.check_file_contains(str(Path(d) / "no.py"), "x")[0])


class CheckUrlSchemeTests(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        ok, detail = prove.check_url("file:///etc/passwd")
        self.assertFalse(ok)
        self.assertIn("http/https", detail)
        ok, detail = prove.check_url("ftp://example.com/x")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
