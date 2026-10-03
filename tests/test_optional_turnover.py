from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import read_json, write_csv, write_json
from alpha_loop.csv_input import create_csv_fixture, derive_ranking, import_csv
from alpha_loop.fixture import create
from alpha_loop.pipeline import config_load, evaluate_run, replay, run
from alpha_loop.provider import FileProvider, validate_input
from alpha_loop.reporting import LocalQwen, qwen_retrospective_report
from alpha_loop.retrospective import retrospective, replay_retrospective
from alpha_loop.screening import screen

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"


class OptionalTurnoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.fixture = create(self.input)

    def tearDown(self):
        self.temp.cleanup()

    def without_turnover(self):
        bars = read_json(self.input / "bars.json")
        for bar in bars:
            bar.pop("turnover_jpy")
        write_json(self.input / "bars.json", bars)

    def snapshot(self):
        data, _ = FileProvider(self.input).load()
        return {"data": data, "target_session": self.fixture["target_session"],
                "as_of": self.fixture["as_of"], "data_grade": "synthetic", "usable_disclosures": []}

    def test_no_turnover_keeps_candidates_outcomes_and_replay(self):
        with_turnover = run(self.root, self.input, CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        original = read_json(self.root / "outputs" / with_turnover["run_id"] / "decisions.json")
        self.without_turnover()
        result = run(self.root, self.input, CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        rows = read_json(self.root / "outputs" / result["run_id"] / "decisions.json")
        self.assertEqual([(r["symbol"], r["stage"], r["rank"]) for r in rows],
                         [(r["symbol"], r["stage"], r["rank"]) for r in original])
        valid = next(r for r in rows if r["symbol"] == "0001")
        self.assertIsNone(valid["turnover_median_20d_jpy"])
        self.assertEqual(valid["turnover_status"], "missing")
        self.assertEqual(valid["liquidity_filter_status"], "disabled")
        with (self.root / "outputs" / result["run_id"] / "candidates.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertTrue(all(r["turnover_median_20d_jpy"] == "" for r in csv.DictReader(stream)))
        self.assertTrue(replay(self.root, result["run_id"])["replay_verified"])
        outcome = evaluate_run(self.root, result["run_id"], self.input, self.fixture["next_session"])
        self.assertEqual(outcome["metrics"]["by_stage"]["PREMOVE"]["hits"], 1)
        self.assertEqual(outcome["metrics"]["net_proxy_count"], 0)

    def test_partial_history_does_not_invent_20_day_median(self):
        self.without_turnover()
        snap = self.snapshot()
        history = [b for b in snap["data"]["bars"] if b["instrument_id"] == "TSE:0001"]
        for b in history[-6:-1]:
            b["turnover_jpy"] = 1_000_000
        config = read_json(CONFIG)
        rows, _ = screen(snap, config, "partial")
        row = rows[0]
        self.assertEqual(row["stage"], "PREMOVE")
        self.assertEqual(row["turnover_observed_sessions_20d"], 5)
        self.assertEqual(row["turnover_status"], "observed_partial")
        self.assertIsNone(row["turnover_median_20d_jpy"])
        config.update(turnover_filter_mode="required", turnover_median_min_jpy=100_000_000)
        row = screen(snap, config, "strict")[0][0]
        self.assertEqual(row["stage"], "DATA_HOLD")
        self.assertIn("turnover_required_missing", row["reason_codes"])

    def test_small_turnover_does_not_filter_default_and_legacy_is_explicit(self):
        snap = self.snapshot()
        for bar in snap["data"]["bars"]:
            bar["turnover_jpy"] = 1
        config = read_json(CONFIG)
        self.assertEqual(screen(snap, config, "small")[0][0]["stage"], "PREMOVE")
        legacy = read_json(CONFIG.with_name("baseline_turnover_v1.json"))
        self.assertEqual(screen(snap, legacy, "legacy")[0][0]["stage"], "INELIGIBLE")
        config.update(turnover_filter_mode="required", turnover_median_min_jpy=None)
        write_json(self.root / "bad.json", config)
        with self.assertRaisesRegex(ValueError, "finite nonnegative threshold"):
            config_load(self.root / "bad.json")
        for value in (None, -1, float("nan"), float("inf")):
            snap["data"]["bars"][0]["turnover_jpy"] = value
            if value is None:
                validate_input(snap["data"], [], self.fixture["target_session"], self.fixture["as_of"])
            else:
                with self.assertRaisesRegex(ValueError, "turnover"):
                    validate_input(snap["data"], [], self.fixture["target_session"], self.fixture["as_of"])

    def test_default_tied_ranking_ignores_turnover_availability(self):
        snap = self.snapshot()
        config = read_json(CONFIG)
        config["max_candidates_per_stage"] = 1
        first = next(b for b in snap["data"]["instruments"] if b["instrument_id"] == "TSE:0001")
        twin = {**first, "instrument_id": "TSE:0007", "symbol": "0007"}
        snap["data"]["instruments"].append(twin)
        for bar in list(snap["data"]["bars"]):
            if bar["instrument_id"] == first["instrument_id"]:
                bar["turnover_jpy"] = None
                snap["data"]["bars"].append({**bar, "instrument_id": twin["instrument_id"], "turnover_jpy": 10**12})
        rows, _ = screen(snap, config, "tie")
        self.assertEqual([r["symbol"] for r in rows if r["selected"] and r["stage"] == "PREMOVE"], ["0001"])

    def test_csv_retrospective_and_ai_proposals_without_turnover(self):
        create_csv_fixture(self.input)
        spec = read_json(self.input / "import.json")
        for item in spec["files"]:
            file = self.input / item["file"]
            with file.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                fields = [f for f in reader.fieldnames if f != "売買代金"]
                rows = [{f: r[f] for f in fields} for r in reader]
            write_csv(file, fields, rows)
            item["columns"].pop("turnover_jpy")
        write_json(self.input / "import.json", spec)
        imported = import_csv(self.root, self.input / "import.json", self.fixture["next_session"])
        ranking = derive_ranking(self.root, Path(imported["input_dir"]), self.fixture["next_session"])
        study = retrospective(self.root, Path(ranking["ranking_input"]), Path(imported["input_dir"]), CONFIG)
        comparison = read_json(Path(study["output_path"]) / "comparison.json")
        for group in comparison["groups"].values():
            self.assertIsNone(group["feature_medians"]["turnover_median_20d_jpy"])
            self.assertEqual(group["feature_observed_counts"]["turnover_median_20d_jpy"], 0)
        provider = LocalQwen("http://127.0.0.1:8000", "Qwen/test", "revision", {"runtime_config_version": "test"})
        response = {"model": "Qwen/test", "choices": [{"finish_reason": "stop", "message": {"content": '{"hypotheses":[{"condition_ids":["volume_ge_2"],"reason_code":"volume_expansion"}]}'}}]}
        with patch.object(provider, "ask", return_value=response) as ask:
            report = qwen_retrospective_report(self.root, study["study_id"], provider)
            self.assertEqual(ask.call_args.args[0]["comparison_directions_listed_vs_not_listed"]["turnover_median_20d_jpy"], "unknown")
        self.assertTrue(read_json(Path(report["report_path"]))["structure_validated"])
        self.assertIn("不明", Path(report["draft_path"]).read_text(encoding="utf-8"))
        self.assertTrue(replay_retrospective(self.root, study["study_id"])["replay_verified"])


if __name__ == "__main__":
    unittest.main()
