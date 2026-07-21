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
    python verify.py --claim "X does Y" --context "from file foo.py" --critics codex
    python verify.py --claim "..." --critics codex --json      # machine-readable

Each critic runs through dispatch.py (in parallel), so every check is metered in
the ledger and honours your provider config. Critics default to enabled
providers marked `adversary = true` (or, for back-compat, whose role mentions
"review"), always excluding the coordinator for independence; override
with --critics.

Exit codes (there is deliberately no 0 — agreement alone never certifies):
    2  refuted / unsupported — DO NOT SHIP
    4  verification did not run (every critic errored)
    3  UNVERIFIED — all critics support it, but consensus is not proof
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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from config import load_providers  # noqa: E402

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
EVIDENCE_NEEDED: <the single concrete proof that would settle this>
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


def synthesize(verdicts: list) -> tuple:
    """Pure decision function: verdict tokens -> (exit_code, label, message).

    Deliberately never returns exit 0: this tool does not certify a claim on
    model agreement alone. A caller that wants a hard pass must confirm the
    EVIDENCE_NEEDED each critic named (or use a future evidence-check mode).
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
    if errors == total:  # includes the empty-panel case
        return 4, "DID_NOT_RUN", (
            "verification did NOT run — every critic errored. This is NOT a pass. "
            "Fix critic auth/availability and re-run.")
    if errors:
        return 1, "INCONCLUSIVE", (
            f"{errors} of {total} critic(s) errored, so the panel is incomplete. "
            "Re-run with working critics.")
    if supported == total:
        return 3, "UNVERIFIED", (
            "all critics support it, but consensus is not truth. This counts only "
            "if the support is grounded in the EVIDENCE_NEEDED each critic named. "
            "Go confirm that evidence yourself; do not treat agreement as a pass.")
    return 1, "INCONCLUSIVE", (
        "mixed/uncertain verdicts. Provide the evidence the critics asked for and "
        "re-run, or narrow the claim.")


def main():
    ap = argparse.ArgumentParser(
        description="Adversarially cross-check a claim across providers.",
    )
    ap.add_argument("--claim", default="", help="the claim (else read from stdin)")
    ap.add_argument("--context", default="", help="optional grounding context/source")
    ap.add_argument("--critics", default="",
                    help="comma-separated provider names (default: adversary=true)")
    ap.add_argument("--timeout", type=int, default=180)
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

    code, label, message = synthesize(verdicts)
    tally = {v.lower(): verdicts.count(v) for v in
             ("SUPPORTED", "REFUTED", "UNSUPPORTED", "UNCERTAIN", "ERROR")}

    if args.json:
        print(json.dumps({
            "result": label,
            "exit_code": code,
            "tally": tally,
            "critics": [{"critic": d["critic"], "verdict": d["verdict"],
                         "ok": d["ok"]} for d in details],
            "message": message,
        }, ensure_ascii=False, indent=2))
    else:
        print("─" * 60)
        print(f"Tally: {tally['supported']} supported · {tally['refuted']} refuted "
              f"· {tally['unsupported']} unsupported · {tally['uncertain']} "
              f"uncertain · {tally['error']} error")
        print(f"RESULT (exit {code}): {message}")
    sys.exit(code)


if __name__ == "__main__":
    main()
