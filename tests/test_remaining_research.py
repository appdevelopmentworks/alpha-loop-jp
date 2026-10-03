import csv
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from alpha_loop.auxiliary import evaluate_auxiliary
from alpha_loop.common import canonical, digest, read_json, write_json
from alpha_loop.csv_input import derive_ranking
from alpha_loop.fixture import create
from alpha_loop.hypothesis_plans import validate_plans
from alpha_loop.pipeline import run
from alpha_loop.research import compare, register
from alpha_loop.research_fixture import create_research_fixture
from alpha_loop.research_workflow import record_review, weekly
from alpha_loop.retrospective import retrospective

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / "configs/baseline.json"


class AuxiliaryAndWeeklyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.f = create(self.root / "input", without_turnover=True)
        target = date.fromisoformat(self.f["target_session"])
        self.friday = (target + timedelta(days=(4 - target.weekday()) % 7)).isoformat()
        self.m = run(self.root, self.root / "input", CONFIG, self.f["target_session"], self.f["as_of"])

    def tearDown(self):
        self.temp.cleanup()

    def future(self):
        calendar = read_json(self.root / "input/calendar.json")
        future_days, day = [], date.fromisoformat(calendar[-1])
        while len(future_days) < 5:
            day += timedelta(days=1)
            if day.weekday() < 5:
                future_days.append(day.isoformat())
        write_json(self.root / "input/calendar.json", calendar + future_days)
        bars = read_json(self.root / "input/bars.json")
        baseline = {b["instrument_id"]: b for b in bars if b["session_date"] == self.f["target_session"]}
        for day in future_days:
            for iid, bar in baseline.items():
                if bar["adj_close"] is None:
                    continue
                bars.append({**bar, "session_date": day, "adj_high": bar["adj_close"] * 1.12, "high": bar["adj_close"] * 1.12,
                             "adj_close": bar["adj_close"] * 1.02, "close": bar["adj_close"] * 1.02, "fetched_at": future_days[-1] + "T19:30:00+09:00"})
        write_json(self.root / "input/bars.json", bars)
        return future_days

    def test_auxiliary_hand_calculation_weekends_and_no_trade_revenue(self):
        days = self.future()
        result = evaluate_auxiliary(self.root, self.m["run_id"], self.root / "input", days[-1])
        with Path(result["outcomes_csv"]).open(encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
        valid = [r for r in rows if r["instrument_id"] == "TSE:0001"]
        self.assertEqual({r["horizon_sessions"] for r in valid}, {"3", "5"})
        for r in valid:
            self.assertAlmostEqual(float(r["high_return"]), .12)
            self.assertAlmostEqual(float(r["close_return"]), .02)
            self.assertEqual(r["trade_return"], "")
        self.assertEqual(result, evaluate_auxiliary(self.root, self.m["run_id"], self.root / "input", days[-1]))

    def test_auxiliary_missing_and_incomplete_are_not_negative(self):
        days = self.future()
        bars = read_json(self.root / "input/bars.json")
        bars = [b for b in bars if not (b["instrument_id"] == "TSE:0001" and b["session_date"] == days[1])]
        write_json(self.root / "input/bars.json", bars)
        result = evaluate_auxiliary(self.root, self.m["run_id"], self.root / "input", days[2])
        with Path(result["outcomes_csv"]).open(encoding="utf-8-sig") as file:
            rows = [r for r in csv.DictReader(file) if r["instrument_id"] == "TSE:0001"]
        self.assertEqual([r["outcome_status"] for r in rows], ["unknown", "horizon_incomplete"])
        self.assertTrue(all(r["high_return"] == "" for r in rows))

    def test_auxiliary_split_unknown_correction_and_hash(self):
        days = self.future()
        bars = read_json(self.root / "input/bars.json")
        # Native vendor history is on the split-adjusted scale; original sealed snapshot stays on old scale.
        for b in bars:
            if b["instrument_id"] == "TSE:0001":
                for key in ("open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close"):
                    b[key] /= 2
                if b["session_date"] == days[0]:
                    b["stock_split_event"] = 2
        write_json(self.root / "input/bars.json", bars)
        result = evaluate_auxiliary(self.root, self.m["run_id"], self.root / "input", days[-1])
        with Path(result["outcomes_csv"]).open(encoding="utf-8-sig") as file:
            row = next(r for r in csv.DictReader(file) if r["instrument_id"] == "TSE:0001")
        self.assertAlmostEqual(float(row["high_return"]), .12)
        Path(result["outcomes_csv"]).write_text("tampered")
        with self.assertRaisesRegex(RuntimeError, "artifact hash"):
            evaluate_auxiliary(self.root, self.m["run_id"], self.root / "input", days[-1])

    def weekly_source(self):
        source = read_json(self.root / "input/source.json")
        source["data_grade"] = "reconstructed"
        write_json(self.root / "input/source.json", source)
        real = run(self.root, self.root / "input", CONFIG, self.f["target_session"], self.f["as_of"])
        ranked = derive_ranking(self.root, self.root / "input", self.f["target_session"], .1, 50)
        study = retrospective(self.root, Path(ranked["ranking_input"]), self.root / "input", CONFIG)
        proposals = validate_plans(json.dumps({"hypotheses": [{"condition_ids": ["volume_ge_1_5"], "reason_code": "volume_expansion"}]}), study["study_id"], "reconstructed")
        path = self.root / "outputs/retrospectives" / study["study_id"] / "qwen/report.json"
        write_json(path, {"hypothesis_plans": proposals, "study_id": study["study_id"], "data_grade": "reconstructed", "structure_validated": True})
        write_json(self.root / "data/operations/service_jobs/source.json", {"run_id": real["run_id"], "qwen_status": "SUCCEEDED", "qwen": {"hypotheses": {"report_path": str(path)}}})
        return proposals

    def test_weekly_saved_retrospective_source_no_holdout_read(self):
        self.weekly_source()
        result = weekly(self.root, self.friday)
        self.assertEqual(result["status"], "DRAFTS_READY")
        self.assertEqual(result["source_days"], 1)
        self.assertEqual(result["new_drafts"], 1)
        self.assertFalse(result["automatic_registration"])
        self.assertEqual(result, weekly(self.root, self.friday))
        Path(result["drafts_path"]).write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "artifact hash"):
            weekly(self.root, self.friday)

    def test_weekly_registered_duplicate_and_preview_is_separate(self):
        plans = self.weekly_source()
        write_json(self.root / "data/research/experiments/exp-test.json", {})
        with patch("alpha_loop.research_workflow._load", return_value={"condition_ids": plans[0]["condition_ids"]}):
            preview = weekly(self.root, self.f["target_session"], force_draft=True)
            result = weekly(self.root, self.friday)
        self.assertEqual(result["status"], "NO_NEW_HYPOTHESIS")
        self.assertEqual(result["registered_duplicate_conditions"], 1)
        self.assertNotEqual(preview["drafts_path"], result["drafts_path"])
        self.assertEqual(weekly(self.root, (date.fromisoformat(self.friday) + timedelta(days=3)).isoformat())["status"], "NOT_DUE")

    def test_weekly_protected_holdout_not_used_for_new_proposals(self):
        self.weekly_source()
        write_json(self.root / "data/research/experiments/exp-test.json", {})
        frozen = {"condition_ids": [], "periods": {"holdout": {"start": self.f["target_session"], "end": self.friday}}}
        with patch("alpha_loop.research_workflow._load", return_value=frozen):
            result = weekly(self.root, self.friday)
        self.assertEqual(result["new_drafts"], 0)
        self.assertEqual(result["protected_holdout_sessions_skipped"], [self.f["target_session"]])


class HumanReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = tempfile.TemporaryDirectory()
        cls.base = Path(cls.master.name)
        create_research_fixture(cls.base, CONFIG)
        cls.exp = register(cls.base, cls.base / "plan.json")["experiment_id"]
        compare(cls.base, cls.exp, cls.base / "batch.json")

    @classmethod
    def tearDownClass(cls):
        cls.master.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "review"
        shutil.copytree(self.base, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_human_hold_record_idempotent_and_baseline_unchanged(self):
        before = digest((self.root / "baseline.json").read_bytes())
        receipt = record_review(self.root, self.exp, "HOLD", "fixture reviewer", "合成検証のみ", True)
        self.assertEqual(receipt, record_review(self.root, self.exp, "HOLD", "fixture reviewer", "合成検証のみ", True))
        self.assertFalse(receipt["production_config_changed"])
        self.assertEqual(before, digest((self.root / "baseline.json").read_bytes()))

    def test_synthetic_approval_and_missing_human_declaration_rejected(self):
        for decision, declared in (("APPROVE_SHADOW", True), ("HOLD", False)):
            with self.assertRaises(ValueError):
                record_review(self.root, self.exp, decision, "reviewer", "test", declared)

    def test_review_tamper_rejected(self):
        receipt = record_review(self.root, self.exp, "REJECT", "reviewer", "test", True)
        path = self.root / "data/research/reviews" / (receipt["review_id"] + ".json")
        value = read_json(path)
        value["decision"] = "APPROVE_SHADOW"
        write_json(path, value)
        with self.assertRaisesRegex(RuntimeError, "receipt hash"):
            record_review(self.root, self.exp, "REJECT", "reviewer", "test", True)
