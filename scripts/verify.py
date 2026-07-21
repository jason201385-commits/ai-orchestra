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

Each critic runs through dispatch.py, so every check is metered in the ledger
and honours your provider config. Critics default to enabled providers whose
role mentions "review" (excluding the coordinator, for independence); override
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
import re
import subprocess
import sys
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
    """Enabled providers whose role invites review — but never the coordinator.
    An adversary that is the same model producing the claim isn't independent."""
    picks = [
        n for n, s in providers.items()
        if s.get("enabled", True)
        and "review" in (s.get("role") or "").lower()
        and "coordinator" not in (s.get("role") or "").lower()
    ]
    return picks


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
    check UNSUPPORTED before SUPPORTED so the substring doesn't shadow it."""
    valid = ("REFUTED", "UNSUPPORTED", "UNCERTAIN", "SUPPORTED")
    for line in text.splitlines():
        s = line.strip().upper()
        if re.match(r"[^A-Z]*VERDICT[^A-Z]*:", s):
            after = s.split(":", 1)[1] if ":" in s else ""
            for token in valid:
                if re.search(rf"\b{token}\b", after):
                    return token
    return "UNCERTAIN"


def main():
    ap = argparse.ArgumentParser(
        description="Adversarially cross-check a claim across providers.",
    )
    ap.add_argument("--claim", default="", help="the claim (else read from stdin)")
    ap.add_argument("--context", default="", help="optional grounding context/source")
    ap.add_argument("--critics", default="",
                    help="comma-separated provider names (default: role~=review)")
    ap.add_argument("--timeout", type=int, default=180)
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
        ap.error("no critics selected. Pass --critics name1,name2, or give a "
                 "provider a role containing 'review' in providers.toml.")

    ctx = f"\n--- CONTEXT / SOURCE ---\n{args.context}" if args.context else ""
    prompt = CRITIC_PROMPT.format(claim=claim, context=ctx)

    print(f"Verifying across {len(critics)} critic(s): {', '.join(critics)}\n")
    verdicts = []
    for name in critics:
        ok, text = run_critic(name, prompt, args.timeout)
        verdict = parse_verdict(text) if ok else "ERROR"
        verdicts.append(verdict)
        print(f"══ {name} → {verdict} " + "═" * max(0, 40 - len(name)))
        print(text if ok else f"  (failed: {text})")
        print()

    # ── synthesis: agreement is NOT proof ────────────────────────────────
    # Exit codes let scripts act on the outcome:
    #   2 = refuted / unsupported            (DO NOT SHIP)
    #   4 = verification did not run         (every critic errored)
    #   3 = UNVERIFIED — consensus only      (all supported, but that's not proof)
    #   1 = inconclusive                     (partial errors, or mixed/uncertain)
    # There is deliberately NO exit 0: this tool never certifies a claim on model
    # agreement alone — confirming the named evidence is a human step.
    total = len(verdicts)
    errors = verdicts.count("ERROR")
    refuted = verdicts.count("REFUTED")
    unsupported = verdicts.count("UNSUPPORTED")
    supported = verdicts.count("SUPPORTED")
    uncertain = verdicts.count("UNCERTAIN")
    print("─" * 60)
    print(f"Tally: {supported} supported · {refuted} refuted · "
          f"{unsupported} unsupported · {uncertain} uncertain · {errors} error")

    if refuted or unsupported:
        print("RESULT (exit 2): DO NOT SHIP AS-IS — a critic refuted the claim or "
              "found no supporting evidence. Fix the flaws or attach the evidence "
              "each critic named, then re-verify.")
        sys.exit(2)
    if errors == total:
        print("RESULT (exit 4): verification did NOT run — every critic errored. "
              "This is NOT a pass. Fix critic auth/availability and re-run.")
        sys.exit(4)
    if errors:
        print(f"RESULT (exit 1): inconclusive — {errors} of {total} critic(s) "
              "errored, so the panel is incomplete. Re-run with working critics.")
        sys.exit(1)
    if supported == total:
        print("RESULT (exit 3): UNVERIFIED — all critics support it, but consensus "
              "is not truth. This counts only if the support is grounded in the "
              "EVIDENCE_NEEDED each critic named. Go confirm that evidence "
              "yourself; do not treat agreement as a pass.")
        sys.exit(3)
    print("RESULT (exit 1): inconclusive — mixed/uncertain verdicts. Provide the "
          "evidence the critics asked for and re-run, or narrow the claim.")
    sys.exit(1)


if __name__ == "__main__":
    main()
