#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record verified task outcomes so routing learns from local real workloads.

Only record a verdict backed by a human decision or replayable evidence. Model
self-assessment is not an outcome.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import CONFIG_DIR, DATA_DIR, LEDGER, load_providers  # noqa: E402
from quota_common import force_utf8_stdout, one_line, redact  # noqa: E402

OUTCOMES = DATA_DIR / "outcomes.jsonl"
ROUTING_PROFILES = CONFIG_DIR / "routing_profiles.json"
VERDICT_SCORE = {"pass": 5.0, "partial": 3.0, "fail": 0.0}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _valid_tasks(path: Path = ROUTING_PROFILES) -> set[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(doc.get("tasks", {}))


def _latest_proof_status(label: str, path: Path = LEDGER) -> bool | None:
    if not label or not path.exists():
        return None
    latest = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("provider") == "prove" and row.get("label") == label:
                latest = row.get("ok") is True
    return latest


def _has_successful_proof(label: str, path: Path = LEDGER) -> bool:
    """Compatibility helper: only the latest matching proof may authorize pass."""
    return _latest_proof_status(label, path) is True


def write_outcome(*, provider: str, task: str, verdict: str, model: str = "",
                  label: str = "", evidence: str = "", score: float | None = None,
                  proof_label: str = "", human_accepted: bool = False,
                  path: Path = OUTCOMES, ledger_path: Path = LEDGER,
                  profiles_path: Path = ROUTING_PROFILES) -> dict:
    if provider not in load_providers():
        raise ValueError(f"unknown provider: {provider}")
    if not SAFE_NAME.fullmatch(task):
        raise ValueError("task must be 1-80 characters: letters, digits, _ . : -")
    valid_tasks = _valid_tasks(profiles_path)
    if task not in valid_tasks:
        raise ValueError(f"unknown routing task: {task}")
    if verdict not in VERDICT_SCORE:
        raise ValueError(f"bad verdict: {verdict}")
    if proof_label and not SAFE_NAME.fullmatch(proof_label):
        raise ValueError("proof-label must be 1-80 characters: letters, digits, _ . : -")
    resolved_score = VERDICT_SCORE[verdict] if score is None else float(score)
    if not 0.0 <= resolved_score <= 5.0 or not resolved_score == resolved_score:
        raise ValueError("score must be between 0 and 5")
    proof_status = _latest_proof_status(proof_label, ledger_path)
    proof_ok = proof_status is True
    if verdict == "pass" and not (proof_ok or human_accepted):
        raise ValueError("pass requires --proof-label matching a successful prove ledger row or --human-accepted")
    verification_source = (
        f"prove:{proof_label}" if proof_ok else
        "human_acceptance" if human_accepted else
        f"prove:{proof_label}:failed" if proof_label and proof_status is False else
        f"prove:{proof_label}:missing" if proof_label else
        "failure_or_partial_observation"
    )
    row = redact({
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "provider": provider,
        "model": model or None,
        "task": task,
        "verdict": verdict,
        "score": resolved_score,
        "label": one_line(label)[:120],
        "evidence": one_line(evidence)[:500],
        "source": verification_source,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def main():
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="Record a verified ai-orchestra task outcome.")
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--task", required=True)
    ap.add_argument("--verdict", choices=sorted(VERDICT_SCORE), required=True)
    ap.add_argument("--score", type=float, default=None, help="optional 0-5 override")
    ap.add_argument("--label", default="")
    ap.add_argument("--evidence", default="", help="short file/command/URL proof reference; no secrets")
    ap.add_argument("--proof-label", default="", help="successful provider=prove ledger label")
    ap.add_argument("--human-accepted", action="store_true", help="use only after explicit human acceptance")
    args = ap.parse_args()
    try:
        row = write_outcome(
            provider=args.provider,
            model=args.model,
            task=args.task,
            verdict=args.verdict,
            score=args.score,
            label=args.label,
            evidence=args.evidence,
            proof_label=args.proof_label,
            human_accepted=args.human_accepted,
        )
    except ValueError as exc:
        ap.error(str(exc))
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
