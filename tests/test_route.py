import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dispatch
import record_outcome
import route
import config


def quota_entry(provider, metric, *, status="live", used=None, error="none"):
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "provider": provider,
        "metric": metric,
        "status": status,
        "used_percent": used,
        "sample_at": now,
        "error_kind": error,
    }


class RouteTests(unittest.TestCase):
    def test_openai_readiness_rejects_missing_model_and_insecure_key_transport(self):
        base = {
            "name": "api", "type": "openai", "enabled": True,
            "base_url": "https://example.com/v1", "api_key_env": "", "model": None,
        }
        missing_model = config.check_provider_readiness(base)
        self.assertFalse(missing_model["ready"])
        self.assertIn("model", missing_model["note"])
        insecure = dict(base, base_url="http://example.com/v1", api_key_env="ROUTER_TEST_KEY", model="x")
        with mock.patch.dict("os.environ", {"ROUTER_TEST_KEY": "not-a-real-secret"}, clear=False):
            state = config.check_provider_readiness(insecure)
        self.assertFalse(state["ready"])
        self.assertEqual(state["status"], "INSECURE")

    def test_inferred_family_overrides_free_text_for_independence(self):
        spec = {
            "type": "openai", "kind": "openai", "model": "claude-sonnet-5",
            "base_url": "https://proxy.example/v1", "family": "other",
        }
        self.assertEqual(config.infer_provider_family(spec), "anthropic")

    def test_namespaced_model_family_is_inferred_before_proxy_host(self):
        openai_proxy = {
            "type": "openai", "kind": "openai", "model": "openai/gpt-5.6",
            "base_url": "https://openrouter.ai/api/v1", "family": "other",
        }
        anthropic_proxy = dict(openai_proxy, model="anthropic/claude-sonnet-5")
        self.assertEqual(config.infer_provider_family(openai_proxy), "openai")
        self.assertEqual(config.infer_provider_family(anthropic_proxy), "anthropic")

    def test_codex_supplemental_credit_exhaustion_does_not_disable_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            path.write_text(json.dumps({
                "schema": 3,
                "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "entries": [
                    quota_entry("codex", "week", used=3),
                    quota_entry("codex", "credits", status="live", error="exhausted"),
                ],
            }), encoding="utf-8")
            state = route.quota_states(path)["codex"]
        self.assertFalse(state["unavailable"])
        self.assertEqual(state["score"], 97.0)
        self.assertTrue(any("supplemental" in note for note in state["notes"]))

    def test_auth_without_live_capacity_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            path.write_text(json.dumps({
                "schema": 3,
                "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "entries": [quota_entry("unknownsvc", "usage", status="unknown", error="auth")],
            }), encoding="utf-8")
            state = route.quota_states(path)["unknownsvc"]
        self.assertTrue(state["unavailable"])

    def test_exhausted_plus_counted_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            path.write_text(json.dumps({
                "schema": 3,
                "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "entries": [
                    quota_entry("agy", "usage", status="counted"),
                    quota_entry("agy", "capacity", status="error", error="exhausted"),
                ],
            }), encoding="utf-8")
            state = route.quota_states(path)["agy"]
        self.assertTrue(state["unavailable"])
        self.assertEqual(state["score"], 0.0)
        self.assertTrue(any("exhausted" in note for note in state["notes"]))

    def test_expired_quota_cache_does_not_use_old_percentage(self):
        old = (datetime.now(timezone.utc).astimezone() - timedelta(hours=7)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            entry = quota_entry("codex", "week", used=4)
            entry["sample_at"] = old
            path.write_text(json.dumps({
                "schema": 3, "generated_at": old, "entries": [entry],
            }), encoding="utf-8")
            state = route.quota_states(path)["codex"]
        self.assertEqual(state["score"], 50.0)
        self.assertTrue(any("expired" in note for note in state["notes"]))

    def test_expired_exhaustion_does_not_permanently_exclude_provider(self):
        old = (datetime.now(timezone.utc).astimezone() - timedelta(hours=7)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            entry = quota_entry("agy", "capacity", status="error", error="exhausted")
            entry["sample_at"] = old
            path.write_text(json.dumps({
                "schema": 3, "generated_at": old, "entries": [entry],
            }), encoding="utf-8")
            state = route.quota_states(path)["agy"]
        self.assertFalse(state["unavailable"])
        self.assertEqual(state["score"], 50.0)

    def test_ledger_health_prefers_task_scope_and_falls_back(self):
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        rows = [
            {"ts": now, "provider": "agy", "task": "design", "ok": True, "duration_s": 10},
            {"ts": now, "provider": "agy", "task": "bulk_text", "ok": False, "duration_s": 20},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            scoped = route.ledger_stats(7, path, "design")["agy"]
            fallback = route.ledger_stats(7, path, "code_review")["agy"]
        self.assertEqual(scoped["scope"], "task")
        self.assertEqual(scoped["attempts"], 1)
        self.assertEqual(scoped["success_rate"], 1.0)
        self.assertEqual(fallback["scope"], "provider_fallback")
        self.assertEqual(fallback["attempts"], 2)

    def test_empty_success_is_normalized_to_explicit_failure(self):
        rc, text, err = dispatch.normalize_provider_result(0, "  ", "")
        self.assertEqual(rc, 2)
        self.assertEqual(text, "")
        self.assertIn("empty output", err)
        self.assertEqual(dispatch.classify_err(err, rc), "empty_output")

    def test_critic_excludes_coordinator_family(self):
        providers = {
            "codex": {
                "name": "codex", "type": "cli", "command": "codex", "enabled": True,
                "family": "openai", "billing": "subscription", "routing_profile": "codex", "model": None,
            },
            "claude": {
                "name": "claude", "type": "cli", "command": "claude", "enabled": True,
                "family": "anthropic", "billing": "subscription", "routing_profile": "claude", "model": None,
            },
        }
        with mock.patch.object(route, "load_providers", return_value=providers), \
                mock.patch.object(route, "provider_readiness", return_value=(True, "ready")), \
                mock.patch.object(route, "ledger_stats", return_value={}), \
                mock.patch.object(route, "quota_states", return_value={}), \
                mock.patch.object(route, "outcome_stats", return_value={}):
            result = route.rank_candidates(
                task_name="adversarial_review", coordinator="codex", role="critic", mode="balanced"
            )
        self.assertEqual([item["provider"] for item in result["ranking"]], ["claude"])
        self.assertNotIn("local_ledger", [item["id"] for item in result["ranking"][0]["evidence"]])
        self.assertTrue(any(item["provider"] == "codex" for item in result["excluded"]))

    def test_unknown_coordinator_is_rejected_cleanly(self):
        with self.assertRaisesRegex(ValueError, "unknown coordinator"):
            route.rank_candidates(
                task_name="code_review", coordinator="codex-cli",
                role="critic", mode="balanced",
            )

    def test_agy_tool_penalty_is_applied(self):
        providers = {
            "agy": {
                "name": "agy", "type": "cli", "command": "agy", "enabled": True,
                "family": "google", "billing": "subscription", "routing_profile": "agy", "model": None,
            }
        }
        patches = (
            mock.patch.object(route, "load_providers", return_value=providers),
            mock.patch.object(route, "provider_readiness", return_value=(True, "ready")),
            mock.patch.object(route, "ledger_stats", return_value={}),
            mock.patch.object(route, "quota_states", return_value={}),
            mock.patch.object(route, "outcome_stats", return_value={}),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            plain = route.rank_candidates(
                task_name="implementation", coordinator="codex", role="specialist", mode="balanced"
            )["ranking"][0]
            tool = route.rank_candidates(
                task_name="implementation", coordinator="codex", role="specialist", mode="balanced", needs_tools=True
            )["ranking"][0]
        self.assertAlmostEqual(plain["score"] - tool["score"], 12.0)
        self.assertTrue(any("permission" in note for note in tool["notes"]))

    def test_unknown_provider_uses_general_profile_and_configured_model(self):
        providers = {
            "custom": {
                "name": "custom", "type": "openai", "enabled": True,
                "base_url": "https://example.invalid/v1", "api_key_env": "",
                "model": "custom-model", "family": "other", "billing": "unknown",
                "routing_profile": "custom",
            }
        }
        with mock.patch.object(route, "load_providers", return_value=providers), \
                mock.patch.object(route, "provider_readiness", return_value=(True, "ready")), \
                mock.patch.object(route, "ledger_stats", return_value={}), \
                mock.patch.object(route, "quota_states", return_value={}), \
                mock.patch.object(route, "outcome_stats", return_value={}):
            result = route.rank_candidates(
                task_name="bulk_text", coordinator="codex", role="executor", mode="balanced"
            )
        self.assertEqual(result["ranking"][0]["provider"], "custom")
        self.assertEqual(result["ranking"][0]["model"], "custom-model")
        self.assertTrue(any("general fallback" in note for note in result["ranking"][0]["notes"]))

    def test_profile_fallback_does_not_invent_model_for_custom_cli(self):
        providers = {
            "custom": {
                "name": "custom", "type": "cli", "kind": "generic", "enabled": True,
                "command": "custom", "model": None, "family": "openai",
                "billing": "subscription", "routing_profile": "missing",
            }
        }
        with mock.patch.object(route, "load_providers", return_value=providers), \
                mock.patch.object(route, "provider_readiness", return_value=(True, "ready")), \
                mock.patch.object(route, "ledger_stats", return_value={}), \
                mock.patch.object(route, "quota_states", return_value={}), \
                mock.patch.object(route, "outcome_stats", return_value={}):
            result = route.rank_candidates(
                task_name="implementation", coordinator="claude", role="executor", mode="balanced"
            )
        self.assertIsNone(result["ranking"][0]["model"])

    def test_record_outcome_writes_only_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(record_outcome, "load_providers", return_value={"agy": {}}):
            path = Path(tmp) / "outcomes.jsonl"
            row = record_outcome.write_outcome(
                provider="agy", task="design", verdict="pass", label="case-1",
                evidence="explicit visual acceptance", human_accepted=True, path=path,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(row["score"], 5.0)
        self.assertEqual(saved["source"], "human_acceptance")
        self.assertNotIn("prompt", saved)

    def test_pass_outcome_requires_bound_verification(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(record_outcome, "load_providers", return_value={"agy": {}}):
            with self.assertRaisesRegex(ValueError, "pass requires"):
                record_outcome.write_outcome(
                    provider="agy", task="design", verdict="pass",
                    path=Path(tmp) / "outcomes.jsonl",
                    ledger_path=Path(tmp) / "missing-ledger.jsonl",
                )

    def test_pass_outcome_accepts_successful_prove_label(self):
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(record_outcome, "load_providers", return_value={"agy": {}}):
            ledger = Path(tmp) / "ledger.jsonl"
            ledger.write_text(json.dumps({
                "ts": now, "provider": "prove", "label": "proof-1", "ok": True,
            }) + "\n", encoding="utf-8")
            row = record_outcome.write_outcome(
                provider="agy", task="design", verdict="pass", proof_label="proof-1",
                path=Path(tmp) / "outcomes.jsonl", ledger_path=ledger,
            )
        self.assertEqual(row["source"], "prove:proof-1")

    def test_latest_failed_proof_overrides_older_success(self):
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(record_outcome, "load_providers", return_value={"agy": {}}):
            ledger = Path(tmp) / "ledger.jsonl"
            ledger.write_text("\n".join(json.dumps(row) for row in (
                {"ts": now, "provider": "prove", "label": "proof-1", "ok": True},
                {"ts": now, "provider": "prove", "label": "proof-1", "ok": False},
            )) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pass requires"):
                record_outcome.write_outcome(
                    provider="agy", task="design", verdict="pass", proof_label="proof-1",
                    path=Path(tmp) / "outcomes.jsonl", ledger_path=ledger,
                )

    def test_failed_outcome_keeps_failed_proof_source(self):
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(record_outcome, "load_providers", return_value={"agy": {}}):
            ledger = Path(tmp) / "ledger.jsonl"
            ledger.write_text(json.dumps({
                "ts": now, "provider": "prove", "label": "proof-fail", "ok": False,
            }) + "\n", encoding="utf-8")
            row = record_outcome.write_outcome(
                provider="agy", task="design", verdict="fail", proof_label="proof-fail",
                path=Path(tmp) / "outcomes.jsonl", ledger_path=ledger,
            )
        self.assertEqual(row["source"], "prove:proof-fail:failed")

    def test_unknown_outcome_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(record_outcome, "load_providers", return_value={"agy": {}}):
            with self.assertRaisesRegex(ValueError, "unknown routing task"):
                record_outcome.write_outcome(
                    provider="agy", task="desgin_typo", verdict="fail",
                    path=Path(tmp) / "outcomes.jsonl",
                )


if __name__ == "__main__":
    unittest.main()
