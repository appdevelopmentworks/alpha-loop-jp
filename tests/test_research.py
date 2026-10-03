from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, read_json, write_json
from alpha_loop.pipeline import list_hypotheses
from alpha_loop.research import compare, register, prepare_batch, refresh
from alpha_loop.research_fixture import create_research_fixture
from alpha_loop.research_metrics import comparison_metrics, decide

PROJECT = Path(__file__).resolve().parents[1]


class ResearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = tempfile.TemporaryDirectory()
        cls.base = Path(cls.master.name) / "fixture"
        create_research_fixture(cls.base, PROJECT / "configs" / "baseline.json")

    @classmethod
    def tearDownClass(cls):
        cls.master.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "research"
        shutil.copytree(self.base, self.root)
        self.plan = read_json(self.root / "plan.json")

    def tearDown(self):
        self.temp.cleanup()

    def registered(self):
        write_json(self.root / "plan.json", self.plan)
        return register(self.root, self.root / "plan.json")["experiment_id"]

    def report(self):
        return compare(self.root, self.registered(), self.root / "batch.json")

    def rehash_evaluation(self, entry):
        directory = self.root / "outputs" / entry["run_id"] / "evaluation" / entry["evaluation_id"]
        manifest = read_json(directory / "evaluation_manifest.json")
        for name in manifest["artifacts"]:
            manifest["artifacts"][name] = digest((directory / name).read_bytes())
        write_json(directory / "evaluation_manifest.json", manifest)

    def test_complete_demo_hand_counts_costs_and_research_only(self):
        before = (self.root / "baseline.json").read_bytes()
        result = self.report()
        metrics = read_json(Path(result["metrics_path"]))
        a, b = metrics["overall"]["A"], metrics["overall"]["B"]
        self.assertEqual((a["selected"], a["valid"], a["hits"]), (100, 100, 50))
        self.assertEqual((b["selected"], b["valid"], b["hits"]), (50, 50, 50))
        self.assertEqual(a["hit_rate"], .5)
        self.assertEqual(b["hit_rate"], 1)
        self.assertEqual(a["capture_rate"], b["capture_rate"])
        self.assertEqual(metrics["by_stage"]["EARLY"]["paired"]["delta_daily_mean_hit_rate"], .5)
        self.assertEqual(a["trade_proxy_count"], 90)  # unfilled / unknown never earn returns
        self.assertEqual(b["trade_proxy_count"], 40)
        self.assertAlmostEqual(b["cost_sensitivity"][2]["mean_net_proxy_return"], .007)
        self.assertIsNone(a["actual_trading_return"])
        self.assertEqual(result["recommendation"], "HOLD")
        self.assertEqual(result["statistical_decision"], "ADOPTION_CANDIDATE")
        self.assertIn("synthetic_or_reconstructed_research_only", result["reason_codes"])
        self.assertFalse(result["automatic_adoption"])
        self.assertFalse(result["performance_claim_allowed"])
        self.assertEqual((self.root / "baseline.json").read_bytes(), before)
        self.assertEqual(list_hypotheses(self.root)[0]["state"], "HOLD")
        self.assertEqual(len(read_json(Path(result["sample_csv"]).with_suffix(".json"))), 154)

    def test_registration_immutable_duplicate_and_required_fields(self):
        experiment = self.registered()
        self.assertEqual(register(self.root, self.root / "plan.json")["experiment_id"], experiment)
        self.plan["criteria"]["min_valid"] += 1
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.registered()
        self.plan["version"] = 2
        del self.plan["criteria"]["min_days"]
        with self.assertRaisesRegex(ValueError, "explicit"):
            self.registered()

    def test_dates_holidays_overlap_and_prospective_freeze(self):
        self.plan["periods"]["holdout"]["start"] = "2026-09-13"  # Sunday is absent
        with self.assertRaisesRegex(ValueError, "exchange-session"):
            self.registered()
        self.plan = read_json(self.base / "plan.json")
        self.plan["periods"]["holdout"]["start"] = "2026-09-03"
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.registered()
        self.plan = read_json(self.base / "plan.json")
        self.plan["mode"] = "prospective"
        with patch("alpha_loop.research.now_iso", return_value="2026-09-14T08:00:00+09:00"):
            with self.assertRaisesRegex(ValueError, "actual registration"):
                self.registered()
        with patch("alpha_loop.research.now_iso", return_value="2026-09-13T20:00:00+09:00"):
            self.registered()

    def test_no_peek_before_complete_window_and_no_partial_metrics(self):
        experiment = self.registered()
        with patch("alpha_loop.research.now_iso", return_value="2026-09-18T21:00:00+09:00"), patch("alpha_loop.research._rows", side_effect=AssertionError("must not inspect labels")):
            waiting = compare(self.root, experiment, self.root / "does-not-exist.json")
        self.assertEqual(waiting["status"], "WAITING")
        self.assertFalse(waiting["metrics_revealed"])
        batch = read_json(self.root / "batch.json")
        batch["entries"] = batch["entries"][:-1]
        write_json(self.root / "partial.json", batch)
        result = compare(self.root, experiment, self.root / "partial.json")
        self.assertEqual(result["status"], "WAITING")
        self.assertNotIn("statistical_decision", result)

    def test_replay_same_input_reports_identical_and_altered_report_rejected(self):
        experiment = self.registered()
        first = compare(self.root, experiment, self.root / "batch.json")
        second = compare(self.root, experiment, self.root / "batch.json")
        self.assertEqual(first, second)
        manifest_path = Path(first["report_path"]).with_name("manifest.json")
        before = manifest_path.read_bytes()
        compare(self.root, experiment, prepare_batch(self.root, experiment))
        self.assertEqual(before, manifest_path.read_bytes())
        Path(first["report_path"]).write_text("fake completion")
        with self.assertRaisesRegex(RuntimeError, "artifact hash mismatch"):
            compare(self.root, experiment, self.root / "batch.json")

    def test_duplicate_days_incomplete_universe_and_tampering_rejected(self):
        experiment = self.registered()
        batch = read_json(self.root / "batch.json")
        batch["entries"].append(batch["entries"][-1])
        write_json(self.root / "duplicate.json", batch)
        with self.assertRaisesRegex(ValueError, "duplicate daily"):
            compare(self.root, experiment, self.root / "duplicate.json")
        entry = batch["entries"][-1]
        path = self.root / "outputs" / entry["run_id"] / "evaluation" / entry["evaluation_id"] / "outcomes.json"
        original = read_json(path)
        write_json(path, original[:-1])
        with self.assertRaisesRegex(RuntimeError, "artifact hash mismatch"):
            compare(self.root, experiment, self.root / "batch.json")
        self.rehash_evaluation(entry)
        with self.assertRaisesRegex(ValueError, "SAME complete universe"):
            compare(self.root, experiment, self.root / "batch.json")

    def test_registered_plan_tampering_and_unknown_negative_rejected(self):
        experiment = self.registered()
        path = self.root / "data" / "research" / "experiments" / (experiment + ".json")
        frozen = read_json(path)
        changed = copy.deepcopy(frozen)
        changed["criteria"]["min_days"] = 1
        write_json(path, changed)
        with self.assertRaisesRegex(RuntimeError, "registered experiment hash"):
            compare(self.root, experiment, self.root / "batch.json")
        write_json(path, frozen)
        entry = read_json(self.root / "batch.json")["entries"][-1]
        outcome_path = self.root / "outputs" / entry["run_id"] / "evaluation" / entry["evaluation_id"] / "outcomes.json"
        rows = read_json(outcome_path)
        self.assertIsNone(rows[-1]["discovery_hit"])
        rows[-1]["discovery_hit"] = False
        write_json(outcome_path, rows)
        self.rehash_evaluation(entry)
        with self.assertRaisesRegex(ValueError, "unknown outcomes"):
            compare(self.root, experiment, self.root / "batch.json")

    def test_trial_budget_family_freeze_and_unused_period(self):
        first = self.registered()
        self.plan["hypothesis_id"] = "H-M5-SECOND"
        self.registered()
        self.plan["hypothesis_id"] = "H-M5-THIRD"
        with self.assertRaisesRegex(ValueError, "trial budget"):
            self.registered()
        compare(self.root, first, self.root / "batch.json")
        self.plan["family_id"] = "DIFFERENT-FAMILY"
        with self.assertRaisesRegex(ValueError, "revealed period"):
            self.registered()

    def test_changed_outcomes_after_reveal_cannot_overwrite(self):
        experiment = self.registered()
        compare(self.root, experiment, self.root / "batch.json")
        entry = read_json(self.root / "batch.json")["entries"][-1]
        path = self.root / "outputs" / entry["run_id"] / "evaluation" / entry["evaluation_id"] / "outcomes.json"
        rows = read_json(path)
        rows[0]["discovery_return"] += .001
        write_json(path, rows)
        self.rehash_evaluation(entry)
        with self.assertRaisesRegex(RuntimeError, "saved outcomes differ"):
            compare(self.root, experiment, self.root / "batch.json")

    def test_raw_future_tampering_and_missing_provenance_gate(self):
        experiment = self.registered()
        batch = read_json(self.root / "batch.json")
        path = self.root / batch["entries"][-1]["future_ref"]
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(RuntimeError, "future evaluation input hash"):
            compare(self.root, experiment, self.root / "batch.json")
        path.write_bytes(original)
        for entry in batch["entries"]:
            entry.pop("future_ref")
        write_json(self.root / "without-raw.json", batch)
        result = compare(self.root, experiment, self.root / "without-raw.json")
        self.assertIn("evaluation_input_raw_missing_labels_not_recomputed", result["reason_codes"])
        self.assertEqual(result["recommendation"], "HOLD")

    def test_material_event_groups_purged_and_shared_clusters_not_inflated(self):
        import alpha_loop.research as research
        original = research._read_entry
        def shared(root, entry, spec):
            result = original(root, entry, spec)
            result[4]["TSE:0001"].add("same-material-event")
            result[4]["TSE:0002"].add("same-material-event")
            return result
        with patch("alpha_loop.research._read_entry", side_effect=shared):
            result = self.report()
        rows = read_json(Path(result["sample_csv"]).with_suffix(".json"))
        repeated = [r for r in rows if r["split"] == "holdout" and r["instrument_id"] in ("TSE:0001", "TSE:0002")]
        self.assertTrue(all(r["exclusion_reason"] == "event_group_crosses_partition" for r in repeated))
        self.assertEqual(len({r["cluster_id"] for r in repeated}), 1)

    def test_staging_interruption_not_completed_and_retry_succeeds(self):
        experiment = self.registered()
        with patch("alpha_loop.research.write_csv", side_effect=OSError("simulated interruption")):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                compare(self.root, experiment, self.root / "batch.json")
        self.assertEqual(list_hypotheses(self.root)[0]["state"], "REGISTERED")
        result = compare(self.root, experiment, self.root / "batch.json")
        self.assertEqual(result["status"], "COMPLETE")

    def test_publish_before_ledger_commit_recovered_by_full_recalculation(self):
        import sqlite3
        experiment = self.registered()
        original = compare(self.root, experiment, self.root / "batch.json")
        con = sqlite3.connect(self.root / "data" / "state.sqlite")
        try:
            with con:
                con.execute("DELETE FROM m5_reports WHERE report_id=?", (original["report_id"],))
                con.execute("UPDATE m5_experiments SET consumed_hash=NULL,report_id=NULL WHERE experiment_id=?", (experiment,))
                con.execute("UPDATE hypotheses SET state='REGISTERED'")
        finally:
            con.close()
        recovered = compare(self.root, experiment, self.root / "batch.json")
        self.assertEqual(original, recovered)

    def test_later_evaluation_cannot_be_cherry_picked(self):
        from datetime import datetime, timedelta
        from alpha_loop.pipeline import evaluate_run
        experiment = self.registered()
        batch = read_json(self.root / "batch.json")
        entry = batch["entries"][-1]
        directory = self.root / entry["future_ref"]
        later = evaluate_run(self.root, entry["run_id"], directory.parent, "2026-09-30")
        rows_path = Path(later["path"]) / "outcomes.json"
        rows = read_json(rows_path)
        for row in rows:
            row["evaluated_at"] = (datetime.fromisoformat(row["evaluated_at"]) + timedelta(minutes=5)).isoformat()
        write_json(rows_path, rows)
        entry["evaluation_id"] = later["evaluation_id"]
        self.rehash_evaluation(entry)
        write_json(self.root / "later.json", batch)
        with self.assertRaisesRegex(ValueError, "first-run/first-evaluation"):
            compare(self.root, experiment, self.root / "later.json")

    def test_five_session_purging_includes_nontrading_days(self):
        self.plan["periods"]["tuning"]["start"] = "2026-08-28"
        self.plan["periods"]["tuning"]["end"] = "2026-09-03"
        result = self.report()
        inputs = read_json(Path(result["report_path"]).with_name("inputs.json"))
        purged = [r for r in inputs["purging"] if r["split"] == "discovery"]
        self.assertEqual(len(purged), 22)
        self.assertTrue(all(r["reason"] == "five_session_horizon_crosses_partition" for r in purged))

    def test_unknowns_zero_candidates_and_capture_guardrails(self):
        result = self.report()
        samples = [r for r in read_json(Path(result["sample_csv"]).with_suffix(".json")) if r["split"] == "holdout" and not r["exclusion_reason"]]
        criteria = self.plan["criteria"]
        for row in samples:
            if row["B_selected"]:
                row["discovery_hit"] = None
        metrics = comparison_metrics(samples, sorted({r["session"] for r in samples}), criteria)
        decision = decide(metrics, criteria, [])
        self.assertEqual(decision["recommendation"], "HOLD")
        self.assertIsNone(metrics["overall"]["B"]["hit_rate"])
        self.assertEqual(metrics["overall"]["B"]["unknown_as_failure"], 0)
        self.assertEqual(metrics["overall"]["B"]["unknown_as_success"], 1)
        for row in samples:
            row["B_selected"] = False
        metrics = comparison_metrics(samples, sorted({r["session"] for r in samples}), criteria)
        self.assertIsNone(metrics["overall"]["B"]["hit_rate"])
        self.assertEqual(metrics["overall"]["B"]["empty_days"], 5)
        self.assertEqual(decide(metrics, criteria, [])["recommendation"], "HOLD")

    def test_adoption_candidate_requires_precision_and_capture_and_no_quality_hold(self):
        result = self.report()
        metrics = read_json(Path(result["metrics_path"]))
        candidate = decide(metrics, self.plan["criteria"], [])
        self.assertEqual(candidate["recommendation"], "ADOPTION_CANDIDATE")
        self.assertEqual(candidate["release_state"], "HUMAN_REVIEW_REQUIRED")
        metrics["overall"]["B"]["capture_rate"] = 0
        self.assertEqual(decide(metrics, self.plan["criteria"], [])["recommendation"], "REJECT")
        self.assertEqual(decide(metrics, self.plan["criteria"], ["reconstructed"])["recommendation"], "HOLD")

    def test_refresh_no_experiments_and_failure_visible(self):
        self.assertEqual(refresh(self.root)["status"], "NOT_REGISTERED")
        experiment = self.registered()
        self.assertEqual(refresh(self.root)["experiments"][0]["status"], "COMPLETE")
        path = self.root / "data" / "research" / "experiments" / (experiment + ".json")
        path.write_text("{}")
        result = refresh(self.root)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["experiments"][0]["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
