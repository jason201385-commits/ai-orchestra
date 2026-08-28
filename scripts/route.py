#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence-aware, subscription-aware routing for ai-orchestra.

This tool recommends an executor; it never dispatches by itself. The Codex or
Claude primary session remains the permission boundary and final integrator.

Examples:
  python route.py --task implementation --coordinator codex
  python route.py --task adversarial_review --coordinator codex --role critic
  python route.py --task bulk_text --coordinator codex --mode economy --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import (  # noqa: E402
    CONFIG_DIR, DATA_DIR, LEDGER, check_provider_readiness,
    infer_provider_family, load_providers,
)
from quota_common import effective_status, force_utf8_stdout, parse_iso, redact  # noqa: E402

PROFILES_PATH = CONFIG_DIR / "routing_profiles.json"
QUOTA_CACHE = DATA_DIR / "quota_cache.json"
OUTCOMES = DATA_DIR / "outcomes.jsonl"

COORDINATOR_FAMILY = {"codex": "openai", "claude": "anthropic"}
FAMILY_PROFILE = {
    "anthropic": "claude",
    "openai": "codex",
    "google": "gemini",
    "xai": "grok",
    "meta": "nim",
    "minimax": "minimax",
}
BILLING_SCORE = {
    "subscription": 100.0,
    "local": 100.0,
    "free_credit": 88.0,
    "unknown": 25.0,
    "metered_api": 35.0,
}
MODE_WEIGHTS = {
    # capability, local quality, health, quota, already-paid utility
    "quality": {"capability": 0.60, "outcome": 0.15, "health": 0.12, "quota": 0.05, "billing": 0.08},
    "balanced": {"capability": 0.48, "outcome": 0.15, "health": 0.14, "quota": 0.09, "billing": 0.14},
    "economy": {"capability": 0.36, "outcome": 0.10, "health": 0.14, "quota": 0.15, "billing": 0.25},
}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_profiles(path: Path = PROFILES_PATH) -> dict:
    doc = _read_json(path, {})
    if doc.get("schema") != 1 or not isinstance(doc.get("tasks"), dict) or not isinstance(doc.get("profiles"), dict):
        raise ValueError(f"invalid routing profile schema: {path}")
    return doc


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _within_days(ts, days: int) -> bool:
    dt = parse_iso(ts)
    if dt is None:
        return False
    now = datetime.now(timezone.utc).astimezone()
    return dt >= now - timedelta(days=days)


def ledger_stats(days: int = 30, path: Path = LEDGER, task_name: str | None = None) -> dict:
    grouped = defaultdict(list)
    task_grouped = defaultdict(list)
    for row in _iter_jsonl(path) or ():
        provider = str(row.get("provider") or "")
        if provider and _within_days(row.get("ts"), days):
            grouped[provider].append(row)
            if task_name and row.get("task") == task_name:
                task_grouped[provider].append(row)

    result = {}
    for provider, rows in grouped.items():
        scoped_rows = task_grouped.get(provider)
        scope = "task" if scoped_rows else "provider_fallback"
        if scoped_rows:
            rows = scoped_rows
        attempts = len(rows)
        successes = sum(bool(row.get("ok")) for row in rows)
        durations = [
            float(row["duration_s"])
            for row in rows
            if isinstance(row.get("duration_s"), (int, float)) and row["duration_s"] >= 0
        ]
        # A small prior prevents one lucky call from outranking a provider with
        # substantial evidence. The prior mean is 2/3, intentionally neutral.
        reliability = (successes + 2.0) / (attempts + 3.0)
        median_latency = statistics.median(durations) if durations else None
        latency_score = 60.0 if median_latency is None else max(20.0, 100.0 - min(160.0, median_latency) / 2.0)
        health = reliability * 80.0 + latency_score * 0.20
        errors = Counter(str(row.get("err_kind") or "unrecorded") for row in rows if not row.get("ok"))
        result[provider] = {
            "days": days,
            "attempts": attempts,
            "successes": successes,
            "success_rate": round(successes / attempts, 4) if attempts else None,
            "median_latency_s": round(median_latency, 1) if median_latency is not None else None,
            "health_score": round(health, 1),
            "errors": dict(errors),
            "scope": scope,
        }
    return result


def outcome_stats(days: int = 90, path: Path = OUTCOMES) -> dict:
    grouped = defaultdict(list)
    for row in _iter_jsonl(path) or ():
        provider = str(row.get("provider") or "")
        task = str(row.get("task") or "")
        score = row.get("score")
        if provider and task and isinstance(score, (int, float)) and _within_days(row.get("ts"), days):
            grouped[(provider, task)].append(float(score))
    result = {}
    for key, scores in grouped.items():
        # Three neutral pseudo-observations keep tiny samples directional.
        normalized = (3 * 70.0 + sum(max(0.0, min(5.0, s)) * 20.0 for s in scores)) / (3 + len(scores))
        result[key] = {"count": len(scores), "quality_score": round(normalized, 1)}
    return result


def quota_states(path: Path = QUOTA_CACHE) -> dict:
    doc = _read_json(path, {})
    generated = parse_iso(doc.get("generated_at"))
    now = datetime.now(timezone.utc).astimezone()
    cache_expired = generated is None or (now - generated).total_seconds() > 6 * 60 * 60
    grouped = defaultdict(list)
    for entry in doc.get("entries", []):
        if isinstance(entry, dict) and entry.get("provider"):
            grouped[str(entry["provider"])].append(entry)

    states = {}
    for provider, entries in grouped.items():
        live_pct = [] if cache_expired else [
            float(e["used_percent"])
            for e in entries
            if isinstance(e.get("used_percent"), (int, float))
            and effective_status(e) == "live"
            and e.get("error_kind") == "none"
        ]
        has_live_capacity = bool(live_pct) and min(live_pct) < 100
        # Expired cache data is observability history, not an active routing
        # gate. In particular, an old auth/exhausted row must not exclude a
        # provider forever after its capacity may have reset.
        auth_error = (not cache_expired) and any(e.get("error_kind") == "auth" for e in entries)
        exhausted = (not cache_expired) and any(e.get("error_kind") == "exhausted" for e in entries)
        counted = any(e.get("status") == "counted" for e in entries)
        notes = []
        if live_pct:
            used = max(live_pct)
            score = max(0.0, 100.0 - used)
            notes.append(f"live quota sample used={used:g}%")
        elif counted:
            score = 45.0 if cache_expired else 60.0
            notes.append("local call count only; official remainder unknown")
        else:
            score = 50.0
            notes.append("quota remainder unknown")
        # Supplemental credit exhaustion must not disable a still-available
        # subscription window (Codex commonly exposes both in one probe).
        unavailable = auth_error and not has_live_capacity
        if exhausted and not has_live_capacity:
            unavailable = True
            score = 0.0
            notes.append("reported capacity exhausted and no live subscription window remains")
        if exhausted and has_live_capacity:
            notes.append("supplemental credits exhausted; subscription window still available")
        if auth_error:
            notes.append("authentication unavailable")
        if cache_expired:
            notes.append("quota cache expired (>6h); cached percentages were not used")
        states[provider] = {
            "score": round(score, 1),
            "unavailable": unavailable,
            "generated_at": doc.get("generated_at"),
            "notes": notes,
        }
    return states


def provider_readiness(spec: dict) -> tuple[bool, str]:
    check = check_provider_readiness(spec)
    return bool(check["ready"]), str(check["note"])


def capability_score(profile: dict, task: dict) -> float:
    caps = profile.get("capabilities", {})
    weights = task.get("weights", {})
    total_weight = sum(float(w) for w in weights.values())
    if total_weight <= 0:
        return 0.0
    weighted = sum(float(weights[name]) * float(caps.get(name, 0)) for name in weights)
    return max(0.0, min(100.0, weighted / (5.0 * total_weight) * 100.0))


def choose_model(profile: dict, task_name: str, mode: str, configured_model: str | None,
                 allow_profile_hint: bool = True) -> str | None:
    task_model = (profile.get("task_models") or {}).get(task_name)
    hint = (profile.get("model_hints") or {}).get(mode)
    # The user's configured model is authoritative. Curated hints are only a
    # fallback for CLI subscriptions that do not declare one.
    if configured_model:
        return configured_model
    if not allow_profile_hint:
        return None
    return task_model or hint or None


def rank_candidates(*, task_name: str, coordinator: str, role: str, mode: str,
                    needs_tools: bool = False, days: int = 30,
                    profiles_path: Path = PROFILES_PATH) -> dict:
    doc = load_profiles(profiles_path)
    task = doc["tasks"].get(task_name)
    if task is None:
        raise ValueError(f"unknown task: {task_name}")
    providers = load_providers()
    ledger = ledger_stats(days, task_name=task_name)
    outcomes = outcome_stats()
    quotas = quota_states()
    coordinator_family = COORDINATOR_FAMILY.get(coordinator)
    if coordinator_family is None:
        raise ValueError(f"unknown coordinator: {coordinator}")
    ranked, excluded = [], []

    for provider, spec in sorted(providers.items()):
        ready, readiness_note = provider_readiness(spec)
        declared_family = str(spec.get("family") or provider)
        inferred_family = infer_provider_family(spec)
        family = inferred_family or declared_family
        profile_name = spec.get("routing_profile") or provider
        profile = doc["profiles"].get(profile_name)
        profile_fallback = ""
        if profile is None:
            fallback_name = FAMILY_PROFILE.get(family, "general")
            profile = doc["profiles"].get(fallback_name)
            if profile is None:
                excluded.append({"provider": provider, "reason": f"missing routing profile: {profile_name}"})
                continue
            profile_fallback = f"routing profile {profile_name} missing; used {fallback_name} family/general fallback"
            profile_name = fallback_name
        if role in ("specialist", "critic") and family == coordinator_family:
            reason = (
                "same model family as coordinator is not an independent critic"
                if role == "critic"
                else "specialist role routes outside the coordinator family"
            )
            excluded.append({"provider": provider, "reason": reason})
            continue
        quota = quotas.get(provider, {"score": 50.0, "unavailable": False, "notes": ["no quota sample"]})
        if not ready:
            excluded.append({"provider": provider, "reason": readiness_note})
            continue
        if quota.get("unavailable"):
            excluded.append({"provider": provider, "reason": "; ".join(quota.get("notes") or ["quota/auth unavailable"])})
            continue

        cap = capability_score(profile, task)
        health = ledger.get(provider, {
            "days": days, "attempts": 0, "successes": 0, "success_rate": None,
            "median_latency_s": None, "health_score": 66.7, "errors": {},
        })
        outcome = outcomes.get((provider, task_name))
        outcome_score = outcome["quality_score"] if outcome else 70.0
        billing = str(spec.get("billing") or "unknown")
        billing_score = BILLING_SCORE.get(billing, BILLING_SCORE["unknown"])
        weights = MODE_WEIGHTS[mode]
        score = (
            cap * weights["capability"]
            + outcome_score * weights["outcome"]
            + health["health_score"] * weights["health"]
            + float(quota.get("score", 50.0)) * weights["quota"]
            + billing_score * weights["billing"]
        )

        constraints = profile.get("constraints") or {}
        notes = [readiness_note, f"billing={billing}"] + list(quota.get("notes") or [])
        if profile_fallback:
            notes.append(profile_fallback)
        if inferred_family and inferred_family != declared_family:
            notes.append(f"declared family {declared_family} overridden by inferred family {inferred_family}")
        if needs_tools and constraints.get("tool_permission_flaky"):
            score -= 12.0
            notes.append("tool-required penalty: local headless permission failures observed")
        if constraints.get("inline_context_preferred"):
            notes.append("inline the evidence packet; do not ask this surface to rediscover files")
        if family == coordinator_family:
            execution = "current_session_or_native_agent"
            notes.append("same family as coordinator: use current session/native agent, not as independent evidence")
        else:
            execution = "external_dispatch"

        evidence_ids = [sid for sid in (profile.get("evidence") or []) if sid != "local_ledger"]
        if health.get("attempts", 0):
            evidence_ids.append("local_ledger")
        if outcome:
            evidence_ids.append("local_outcomes")
        evidence = [
            {"id": sid, **doc["sources"][sid]}
            for sid in evidence_ids
            if sid in doc.get("sources", {})
        ]
        ranked.append({
            "provider": provider,
            "family": family,
            "model": choose_model(
                profile, task_name, mode, spec.get("model"),
                allow_profile_hint=not bool(profile_fallback),
            ),
            "prior_basis": profile.get("prior_basis", ""),
            "execution": execution,
            "score": round(max(0.0, min(100.0, score)), 1),
            "components": {
                "capability": round(cap, 1),
                "verified_local_outcome": round(outcome_score, 1) if outcome else None,
                "outcome_prior": None if outcome else 70.0,
                "health": health["health_score"],
                "quota": quota.get("score", 50.0),
                "billing_utility": billing_score,
            },
            "local_health": health,
            "local_outcome": outcome,
            "notes": notes,
            "evidence": evidence,
        })

    ranked.sort(key=lambda item: (
        -item["score"],
        -item["components"]["health"],
        -item["local_health"].get("attempts", 0),
        item["provider"],
    ))
    return redact({
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "profiles_as_of": doc.get("as_of"),
        "task": task_name,
        "task_label": task.get("label"),
        "coordinator": coordinator,
        "coordinator_family": coordinator_family,
        "role": role,
        "mode": mode,
        "needs_tools": needs_tools,
        "ranking": ranked,
        "excluded": excluded,
        "warnings": [
            "Capability values are evidence-backed priors, not benchmark percentages.",
            "Official claims, community rankings and local reliability are separate signals.",
            "No model output proves task completion; replay deterministic evidence after dispatch.",
        ],
    })


def print_text(result: dict, limit: int = 5):
    print(
        f"ROUTE {result['task']} ({result['task_label']}) | coordinator={result['coordinator']} "
        f"role={result['role']} mode={result['mode']}"
    )
    print("-" * 78)
    ranking = result.get("ranking", [])[:limit]
    if not ranking:
        print("No usable provider. Review exclusions below.")
    for index, item in enumerate(ranking, 1):
        model = f" model={item['model']}" if item.get("model") else ""
        health = item["local_health"]
        rate = "unknown" if health.get("success_rate") is None else f"{health['success_rate'] * 100:.0f}%/{health['attempts']}"
        print(f"{index}. {item['provider']}{model} score={item['score']:.1f} execution={item['execution']}")
        print(
            f"   capability={item['components']['capability']:.1f} health={item['components']['health']:.1f} "
            f"quota={item['components']['quota']:.1f} billing={item['components']['billing_utility']:.1f} "
            f"local_success={rate} scope={health.get('scope', 'none')}"
        )
        print(f"   evidence={','.join(e['id'] for e in item.get('evidence', []))}")
        if item.get("notes"):
            print(f"   note={'; '.join(item['notes'])}")
    if result.get("excluded"):
        print("\nExcluded:")
        for item in result["excluded"]:
            print(f"- {item['provider']}: {item['reason']}")
    print("\nRule: route is a recommendation; deterministic proof decides completion.")


def main():
    force_utf8_stdout()
    try:
        doc = load_profiles()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="Evidence- and quota-aware provider routing (no dispatch).")
    ap.add_argument("--task", choices=sorted(doc["tasks"]), required=True)
    ap.add_argument("--coordinator", choices=sorted(COORDINATOR_FAMILY), required=True)
    ap.add_argument("--role", choices=("executor", "specialist", "critic"), default="executor")
    ap.add_argument("--mode", choices=sorted(MODE_WEIGHTS), default="balanced")
    ap.add_argument("--needs-tools", action="store_true")
    ap.add_argument("--days", type=int, default=30, help="local ledger health window")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.days <= 0 or args.limit <= 0:
        ap.error("--days and --limit must be positive")
    result = rank_candidates(
        task_name=args.task,
        coordinator=args.coordinator,
        role=args.role,
        mode=args.mode,
        needs_tools=args.needs_tools,
        days=args.days,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_text(result, args.limit)
    return 0 if result.get("ranking") else 1


if __name__ == "__main__":
    raise SystemExit(main())
