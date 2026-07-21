#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prove.py — a proof-of-work / replay gate.

Confirm a claim with a LOCAL check, not another model's opinion. This is the
"proof-of-work" and "verify by replay" levers from docs/anti-hallucination.md,
made executable: run a command, inspect a file, or fetch a URL, and exit 0 only
if the proof actually holds.

Usage:
    python prove.py --cmd "pytest -q tests/test_x.py" --expect-rc 0
    python prove.py --cmd "grep -rc TODO src" --contains-output "0"
    python prove.py --files src/a.py src/b.py            # exist & non-empty
    python prove.py --file-contains src/a.py "def foo"   # file contains text
    python prove.py --url https://example.com --contains-text "Example"

Combine several checks in one call — ALL must pass. Exit 0 = proven,
non-zero = not proven. Each run is metered to the ledger as provider "prove".

Also importable: check_cmd / check_files / check_file_contains / check_url each
return (ok: bool, detail: str), so verify.py can run a critic's named evidence.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from config import DATA_DIR, LEDGER  # noqa: E402
try:
    from quota_common import redact
except Exception:  # pragma: no cover
    def redact(x):
        return x


# ── check primitives (importable) ──────────────────────────────────────────

def check_cmd(cmd, expect_rc=0, contains_output=None, shell=None, timeout=120):
    """Run a command; pass if exit code == expect_rc and (optional) stdout
    contains the substring.

    A string `cmd` runs through the platform shell (correct quoting/paths on
    both Windows and POSIX) — only pass commands you trust. A list `cmd` runs
    with shell=False (safe argv), which is how verify.py runs an evidence check.
    """
    if shell is None:
        shell = isinstance(cmd, str)
    try:
        p = subprocess.run(
            cmd, shell=shell, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        return False, f"command not found: {e}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != expect_rc:
        tail = (p.stderr.decode("utf-8", "replace") or out)[-200:]
        return False, f"exit {p.returncode} (expected {expect_rc}); {tail.strip()}"
    if contains_output is not None and contains_output not in out:
        return False, f"stdout did not contain {contains_output!r}"
    return True, f"exit {expect_rc}" + (
        f", output contains {contains_output!r}" if contains_output else "")


def check_files(paths):
    """Pass if every path exists and is a non-empty regular file."""
    missing, empty = [], []
    for p in paths:
        fp = Path(p)
        if not fp.is_file():
            missing.append(p)
        elif fp.stat().st_size == 0:
            empty.append(p)
    if missing:
        return False, f"missing: {', '.join(missing)}"
    if empty:
        return False, f"empty: {', '.join(empty)}"
    return True, f"{len(paths)} file(s) exist & non-empty"


def check_file_contains(path, substring):
    fp = Path(path)
    if not fp.is_file():
        return False, f"missing: {path}"
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"unreadable: {e}"
    if substring in text:
        return True, f"{path} contains {substring!r}"
    return False, f"{path} does not contain {substring!r}"


def check_url(url, contains_text=None, timeout=20):
    """Pass if the URL returns a 2xx and (optional) the body contains the text.
    HTTPS/HTTP only — refuses file://, ftp://, etc."""
    import urllib.request
    import urllib.error
    from urllib.parse import urlparse
    if urlparse(url).scheme not in ("http", "https"):
        return False, "only http/https URLs are allowed"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read(200_000).decode("utf-8", "replace") if contains_text else ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, redact(str(e))[:160]
    if not (200 <= code < 300):
        return False, f"HTTP {code}"
    if contains_text is not None and contains_text not in body:
        return False, f"body did not contain {contains_text!r}"
    return True, f"HTTP {code}" + (
        f", body contains {contains_text!r}" if contains_text else "")


def log_proof(kind, ok, detail, label=""):
    """Meter the proof like any other work (provider = 'prove')."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rec = redact({
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "provider": "prove", "label": label, "kind": kind,
            "ok": bool(ok), "detail": str(detail)[:300],
        })
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # metering must never break the gate


def main():
    ap = argparse.ArgumentParser(description="Proof-of-work / replay gate.")
    ap.add_argument("--cmd", help="a command to run")
    ap.add_argument("--expect-rc", type=int, default=0, help="expected exit code")
    ap.add_argument("--contains-output", help="substring the command stdout must contain")
    ap.add_argument("--files", nargs="+", help="paths that must exist & be non-empty")
    ap.add_argument("--file-contains", nargs=2, metavar=("PATH", "SUBSTR"),
                    help="a file that must contain a substring")
    ap.add_argument("--url", help="a URL that must return 2xx")
    ap.add_argument("--contains-text", help="substring the URL body must contain")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checks = []
    if args.cmd:
        checks.append(("cmd", *check_cmd(
            args.cmd, args.expect_rc, args.contains_output, timeout=args.timeout)))
    if args.files:
        checks.append(("files", *check_files(args.files)))
    if args.file_contains:
        checks.append(("file-contains", *check_file_contains(*args.file_contains)))
    if args.url:
        checks.append(("url", *check_url(args.url, args.contains_text, args.timeout)))

    if not checks:
        ap.error("give at least one of --cmd / --files / --file-contains / --url")

    all_ok = all(ok for _, ok, _ in checks)
    for kind, ok, detail in checks:
        log_proof(kind, ok, detail, args.label)

    if args.json:
        print(json.dumps({
            "proven": all_ok,
            "checks": [{"kind": k, "ok": ok, "detail": d} for k, ok, d in checks],
        }, ensure_ascii=False, indent=2))
    else:
        for kind, ok, detail in checks:
            print(f"[{'PASS' if ok else 'FAIL'}] {kind}: {detail}")
        print(f"\n{'PROVEN' if all_ok else 'NOT PROVEN'} "
              f"({sum(ok for _, ok, _ in checks)}/{len(checks)} checks passed)")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
