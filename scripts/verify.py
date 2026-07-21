#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify.py — an adversarial cross-check to catch hallucinations before you ship.

This is the opposite of "ask five models and go with the majority". It encodes
what actually reduces hallucination (see docs/anti-hallucination.md):

  * Adversarial stance — each critic is told to REFUTE the claim, not agree.
  * Evidence-or-it-didn't-happen — a claim with no source / no runnable proof
    is reported UNSUPPORTED even if it "sounds right".
  * Consensus is not truth — if critics only agree because they share the same
    blind spot, this tool says so instead of rubber-stamping.

Usage:
    echo "the claim / answer to check" | python verify.py --critics codex,deepseek
    python verify.py --claim "X does Y" --critics codex --check-evidence
    python verify.py --claim "..." --critics codex --json       # machine-readable

Each critic runs through dispatch.py (in parallel), so every check is metered in
the ledger and honours your provider config. Critics default to enabled
providers marked `adversary = true` (or, for back-compat, whose role mentions
"review"), always excluding the coordinator for independence; override
with --critics.

With --check-evidence, each critic's named EVIDENCE_SPEC (a file/url, or with
--run-commands a shell command) is actually executed — that is the ONLY way to
earn exit 0. Model agreement alone never does.

Exit codes:
    0  VERIFIED — all critics support AND named evidence check(s) passed
       (only reachable with --check-evidence)
    2  refuted / unsupported, OR a named evidence check FAILED — DO NOT SHIP
    3  UNVERIFIED — all support but no evidence ran (consensus is not proof)
    4  verification did not run (every critic errored)
    1  inconclusive (partial errors, or mixed/uncertain verdicts)
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def _utf8_io():
    """Idempotently make stdout/stderr UTF-8. Called from main() only — never at
    import time, so importing this module doesn't mutate a caller's streams."""
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name)
        if getattr(_s, "_ai_orchestra_utf8", False) or not hasattr(_s, "buffer"):
            continue
        if (getattr(_s, "encoding", "") or "").lower().replace("-", "") == "utf8":
            try:
                _s._ai_orchestra_utf8 = True
            except AttributeError:
                pass
            continue
        _w = io.TextIOWrapper(_s.buffer, encoding="utf-8", errors="replace")
        _w._ai_orchestra_utf8 = True
        setattr(sys, _name, _w)


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from config import load_providers  # noqa: E402
from prove import check_cmd, check_file_contains, check_files, check_url  # noqa: E402

DISPATCH = SCRIPTS / "dispatch.py"

CRITIC_PROMPT = """\
You are an adversarial verifier. Your job is to REFUTE the claim below, not to \
agree with it. Assume it is wrong until evidence proves otherwise.

Rules:
- A claim with no citable source, no file:line, and no runnable proof is \
UNSUPPORTED — regardless of how plausible it sounds.
- Distinguish real-world correlation from "sounds coherent". Ask: what concrete \
observation would make this false?
- Be specific. Vague agreement is worthless.

Respond in EXACTLY this format and nothing else:

VERDICT: SUPPORTED | REFUTED | UNSUPPORTED | UNCERTAIN
CONFIDENCE: low | medium | high
EVIDENCE_NEEDED: <the single concrete proof that would settle this, in prose>
EVIDENCE_TYPE: cmd | file | url | none
EVIDENCE_SPEC: <a machine-runnable form of that proof, matching EVIDENCE_TYPE:
  cmd  -> a shell command that exits 0 if and only if the claim holds
  file -> a path, or "path:LINE", or 'path contains "text"'
  url  -> a URL, or 'url contains "text"'
  none -> leave blank if no local check can settle this>
FLAWS:
- <specific problem, or "none found">

--- CLAIM TO VERIFY ---
{claim}
{context}
"""


def default_critics(providers: dict) -> list:
    """Pick independent adversaries. Prefer providers explicitly marked
    `adversary = true`; fall back to the old role~="review" heuristic only if
    none are marked. The coordinator is never a default critic — an adversary
    that is the same model producing the claim isn't independent."""
    enabled = {n: s for n, s in providers.items() if s.get("enabled", True)}
    explicit = sorted(
        n for n, s in enabled.items()
        if s.get("adversary") and "coordinator" not in (s.get("role") or "").lower()
    )
    if explicit:
        return explicit
    return sorted(
        n for n, s in enabled.items()
        if "review" in (s.get("role") or "").lower()
        and "coordinator" not in (s.get("role") or "").lower()
    )


def run_critic(name: str, prompt: str, timeout: int) -> tuple:
    """Return (ok, text). Runs the critic through dispatch.py for metering."""
    try:
        p = subprocess.run(
            [sys.executable, str(DISPATCH), name, "--label", "verify",
             "--timeout", str(timeout)],
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return False, "(critic timed out)"
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0 and not out:
        return False, err[-300:] or "(no output)"
    return True, out


def parse_verdict(text: str) -> str:
    """Extract the verdict token robustly. Tolerates markdown (**VERDICT:**),
    an empty value, and the token appearing without exact spacing. Order matters:
    check UNSUPPORTED before SUPPORTED so the substring doesn't shadow it.
    If an off-contract reply names several tokens on the line, the most negative
    one wins (REFUTED > UNSUPPORTED > UNCERTAIN > SUPPORTED) — a deliberate bias
    toward "DO NOT SHIP", the safe direction for a hallucination gate."""
    valid = ("REFUTED", "UNSUPPORTED", "UNCERTAIN", "SUPPORTED")
    for line in text.splitlines():
        s = line.strip().upper()
        if re.match(r"[^A-Z]*VERDICT[^A-Z]*:", s):
            after = s.split(":", 1)[1] if ":" in s else ""
            for token in valid:
                if re.search(rf"\b{token}\b", after):
                    return token
    return "UNCERTAIN"


EVIDENCE_TYPES = ("cmd", "file", "url", "none")


def parse_evidence(text: str) -> tuple:
    """Extract (etype, espec) from a critic reply, or (None, None) if there is
    no machine-runnable evidence."""
    etype = espec = None
    for line in text.splitlines():
        s = line.strip()
        # tolerate markdown (**EVIDENCE_TYPE:**) — consume any non-alphanumeric
        # run (colon, asterisks, spaces) between the label and the value.
        mt = re.match(r"[^A-Za-z]*EVIDENCE_TYPE\b[^A-Za-z0-9]*(\w+)", s, re.I)
        if mt:
            t = mt.group(1).lower()
            etype = t if t in EVIDENCE_TYPES else None
        ms = re.match(r"[^A-Za-z]*EVIDENCE_SPEC\b\s*[:*]*\s*(.+)$", s, re.I)
        if ms:
            espec = ms.group(1).strip().strip("*`\"' \t").strip()
    if etype in (None, "none") or not espec:
        return None, None
    return etype, espec


def run_one_evidence(etype, espec, run_commands, timeout):
    """Run one critic's named evidence via prove.py primitives.
    Returns (ran: bool, ok: bool, detail: str). cmd-type evidence is skipped
    unless run_commands is True (it executes model-suggested shell commands)."""
    if etype == "cmd":
        if not run_commands:
            return False, True, "skipped (cmd evidence needs --run-commands)"
        print(f"  [running evidence cmd] {espec}", file=sys.stderr, flush=True)
        ok, detail = check_cmd(espec, timeout=timeout)
        return True, ok, detail
    if etype == "file":
        mm = re.match(r'^(.+?)\s+contains\s+["\']?(.+?)["\']?$', espec, re.I)
        if mm:
            ok, detail = check_file_contains(mm.group(1).strip(), mm.group(2))
            return True, ok, detail
        path = espec.rsplit(":", 1)[0] if re.search(r":\d+$", espec) else espec
        ok, detail = check_files([path.strip()])
        return True, ok, detail
    if etype == "url":
        mm = re.match(r'^(\S+)\s+contains\s+["\']?(.+?)["\']?$', espec, re.I)
        if mm:
            ok, detail = check_url(mm.group(1), mm.group(2), timeout=timeout)
        else:
            ok, detail = check_url(espec.split()[0], timeout=timeout)
        return True, ok, detail
    return False, True, "no checkable evidence"


def synthesize(verdicts: list, evidence_ran: bool = False,
               evidence_ok: bool = True) -> tuple:
    """Pure decision function: verdict tokens (+ evidence outcome) ->
    (exit_code, label, message).

    Exit 0 is reserved for a genuinely EARNED pass: all critics support it AND
    at least one named evidence check actually ran and passed. Model agreement
    alone never earns exit 0 — that is the whole point.
    """
    total = len(verdicts)
    errors = verdicts.count("ERROR")
    refuted = verdicts.count("REFUTED")
    unsupported = verdicts.count("UNSUPPORTED")
    supported = verdicts.count("SUPPORTED")

    if refuted or unsupported:
        return 2, "DO_NOT_SHIP", (
            "a critic refuted the claim or found no supporting evidence. Fix the "
            "flaws or attach the evidence each critic named, then re-verify.")
    if evidence_ran and not evidence_ok:
        return 2, "DO_NOT_SHIP", (
            "the named evidence check FAILED — the claim's own proof did not "
            "hold. Do not ship; the claim is not grounded.")
    if errors == total:  # includes the empty-panel case
        return 4, "DID_NOT_RUN", (
            "verification did NOT run — every critic errored. This is NOT a pass. "
            "Fix critic auth/availability and re-run.")
    if errors:
        return 1, "INCONCLUSIVE", (
            f"{errors} of {total} critic(s) errored, so the panel is incomplete. "
            "Re-run with working critics.")
    if supported == total:
        if evidence_ran and evidence_ok:
            return 0, "VERIFIED", (
                "all critics support it AND the named evidence check(s) passed. "
                "This is a grounded pass — proof held, not just agreement.")
        return 3, "UNVERIFIED", (
            "all critics support it, but consensus is not truth. Re-run with "
            "--check-evidence so the named proof is actually executed, or confirm "
            "that evidence yourself. Agreement alone is not a pass.")
    return 1, "INCONCLUSIVE", (
        "mixed/uncertain verdicts. Provide the evidence the critics asked for and "
        "re-run, or narrow the claim.")


def main():
    _utf8_io()
    ap = argparse.ArgumentParser(
        description="Adversarially cross-check a claim across providers.",
    )
    ap.add_argument("--claim", default="", help="the claim (else read from stdin)")
    ap.add_argument("--context", default="", help="optional grounding context/source")
    ap.add_argument("--critics", default="",
                    help="comma-separated provider names (default: adversary=true)")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--check-evidence", action="store_true",
                    help="run the file/url evidence each critic names — the only "
                         "way to earn exit 0 (a grounded pass)")
    ap.add_argument("--run-commands", action="store_true",
                    help="also run cmd-type evidence; executes model-suggested "
                         "shell commands, so use only when you trust the critics")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable JSON report instead of prose")
    args = ap.parse_args()

    claim = args.claim.strip() or sys.stdin.read().strip()
    if not claim:
        ap.error("provide a claim via --claim or stdin")

    providers = load_providers()
    if args.critics:
        critics = [c.strip() for c in args.critics.split(",") if c.strip()]
    else:
        critics = default_critics(providers)
    unknown = [c for c in critics if c not in providers]
    if unknown:
        ap.error(f"unknown critic(s): {', '.join(unknown)}. "
                 f"Configured: {', '.join(sorted(providers))}")
    if not critics:
        ap.error("no critics selected. Pass --critics name1,name2, mark a provider "
                 "`adversary = true`, or give one a role containing 'review'.")

    ctx = f"\n--- CONTEXT / SOURCE ---\n{args.context}" if args.context else ""
    prompt = CRITIC_PROMPT.format(claim=claim, context=ctx)

    if not args.json:
        print(f"Verifying across {len(critics)} critic(s) in parallel: "
              f"{', '.join(critics)}\n")

    # Run critics concurrently; dispatch.py's own lock serializes any that share
    # one CLI login (e.g. two claude critics), so parallelism is safe.
    def work(name):
        ok, text = run_critic(name, prompt, args.timeout)
        return name, ok, text

    with ThreadPoolExecutor(max_workers=min(8, len(critics))) as pool:
        results = list(pool.map(work, critics))

    details, verdicts = [], []
    for name, ok, text in results:
        verdict = parse_verdict(text) if ok else "ERROR"
        verdicts.append(verdict)
        details.append({"critic": name, "verdict": verdict, "ok": ok,
                        "output": text})
        if not args.json:
            print(f"══ {name} → {verdict} " + "═" * max(0, 40 - len(name)))
            print(text if ok else f"  (failed: {text})")
            print()

    # ── optional evidence loop: run the proof each critic named ──────────
    evidence_ran, evidence_ok, evidence_results = False, True, []
    if args.check_evidence:
        seen = set()
        for d in details:
            if not d["ok"]:
                continue
            etype, espec = parse_evidence(d["output"])
            if not etype or (etype, espec) in seen:
                continue
            seen.add((etype, espec))
            ran, ok, detail = run_one_evidence(
                etype, espec, args.run_commands, args.timeout)
            evidence_ran = evidence_ran or ran
            evidence_ok = evidence_ok and (ok if ran else True)
            evidence_results.append({"critic": d["critic"], "type": etype,
                                     "spec": espec, "ran": ran, "ok": ok,
                                     "detail": detail})

    code, label, message = synthesize(verdicts, evidence_ran, evidence_ok)
    tally = {v.lower(): verdicts.count(v) for v in
             ("SUPPORTED", "REFUTED", "UNSUPPORTED", "UNCERTAIN", "ERROR")}

    if args.json:
        print(json.dumps({
            "result": label,
            "exit_code": code,
            "tally": tally,
            "critics": [{"critic": d["critic"], "verdict": d["verdict"],
                         "ok": d["ok"]} for d in details],
            "evidence_checked": evidence_ran,
            "evidence": evidence_results,
            "message": message,
        }, ensure_ascii=False, indent=2))
    else:
        print("─" * 60)
        print(f"Tally: {tally['supported']} supported · {tally['refuted']} refuted "
              f"· {tally['unsupported']} unsupported · {tally['uncertain']} "
              f"uncertain · {tally['error']} error")
        if evidence_results:
            print("Evidence checks:")
            for e in evidence_results:
                mark = "PASS" if e["ran"] and e["ok"] else (
                    "FAIL" if e["ran"] else "SKIP")
                print(f"  [{mark}] ({e['type']}) {e['spec']} — {e['detail']}")
        print(f"RESULT (exit {code}): {message}")
    sys.exit(code)


if __name__ == "__main__":
    main()
