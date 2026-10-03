from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alpha_loop.common import canonical, digest, read_json, stable_id, write_json
from alpha_loop.evaluation import evaluate
from alpha_loop.fixture import create
from alpha_loop.pipeline import _code_hash, evaluate_run, replay, run
from alpha_loop.provider import FileProvider, validate_input
from alpha_loop.screening import screen

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = create(self.root / "demo-input")

    def tearDown(self):
        self.temp.cleanup()

    def run_once(self):
        return run(self.root, self.root / "demo-input", CONFIG, self.fixture["target_session"], self.fixture["as_of"])

    def test_end_to_end_duplicate_replay_evaluation(self):
        first = self.run_once()
        self.assertEqual(first["candidate_count"], 3)
        self.assertEqual(first["universe_count"], 6)
        self.assertEqual(first["run_id"], self.run_once()["run_id"])
        config = first["config"]
        replay_key = stable_id("replay", first["target_session"], first["snapshot_id"],
                               config["strategy_version"], digest(canonical(config)), _code_hash(), first["run_id"])
        stale = self.root / "outputs" / (".staging-run-" + replay_key[:20])
        stale.mkdir()
        (stale / "partial.csv").write_text("incomplete", encoding="utf-8")
        self.assertTrue(replay(self.root, first["run_id"])["replay_verified"])
        self.assertFalse(stale.exists())
        decisions = read_json(self.root / "outputs" / first["run_id"] / "decisions.json")
        self.assertEqual({r["stage"] for r in decisions if r["selected"]}, {"EARLY", "PREMOVE", "WATCH"})
        self.assertEqual(len(read_json(self.root / "data" / "snapshots" / (first["snapshot_id"] + ".json"))["rejected_disclosures"]), 1)
        result = evaluate_run(self.root, first["run_id"], self.root / "demo-input", self.fixture["next_session"])
        self.assertEqual(result["metrics"]["by_stage"]["PREMOVE"]["hits"], 1)
        self.assertEqual(result["metrics"]["net_proxy_count"], 0)
        rows = read_json(Path(result["path"]) / "outcomes.json")
        early = next(o for o in rows if o["instrument_id"] == "TSE:0002")
        self.assertEqual(early["execution_status"], "limit_up")
        self.assertIsNone(early["open_close_proxy_return"])
        self.assertEqual(result["evaluation_id"], evaluate_run(self.root, first["run_id"], self.root / "demo-input", self.fixture["next_session"])["evaluation_id"])
        self.assertTrue((self.root / "outputs" / first["run_id"] / "candidates.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue((Path(result["path"]) / "outcomes.csv").exists())

    def test_future_input_and_missing_target(self):
        provider = FileProvider(self.root / "demo-input")
        data, _ = provider.load()
        disclosures, _ = provider.load_disclosures()
        data["bars"].append({**data["bars"][-1], "session_date": self.fixture["next_session"]})
        with self.assertRaisesRegex(ValueError, "outside universe or calendar"):
            validate_input(data, disclosures, self.fixture["target_session"], self.fixture["as_of"])
        data["bars"].pop()
        data["bars"] = [b for b in data["bars"] if not (b["instrument_id"] == "TSE:0001" and b["session_date"] == self.fixture["target_session"])]
        with self.assertRaisesRegex(ValueError, "BLOCKED_DATA"):
            validate_input(data, disclosures, self.fixture["target_session"], self.fixture["as_of"])

    def test_zero_candidates_and_missing_history(self):
        first = self.run_once()
        snap = read_json(self.root / "data" / "snapshots" / (first["snapshot_id"] + ".json"))
        snap["data"]["bars"] = [b for b in snap["data"]["bars"] if b["session_date"] == self.fixture["target_session"]]
        snap["usable_disclosures"] = []
        decisions, _ = screen(snap, read_json(CONFIG), "test")
        self.assertEqual(sum(bool(d["selected"]) for d in decisions), 0)
        self.assertTrue(all(d["stage"] == "DATA_HOLD" for d in decisions))
        write_json(self.root / "demo-input" / "disclosures.json", [])
        config = read_json(CONFIG)
        config["turnover_filter_mode"] = "required"
        config["turnover_median_min_jpy"] = 10**20
        write_json(self.root / "zero-config.json", config)
        empty = run(self.root, self.root / "demo-input", self.root / "zero-config.json", self.fixture["target_session"], self.fixture["as_of"])
        self.assertEqual(empty["candidate_count"], 0)
        csv_path = self.root / "outputs" / empty["run_id"] / "candidates.csv"
        self.assertEqual(len(csv_path.read_text(encoding="utf-8-sig").splitlines()), 1)

    def test_split_rebase_and_cost(self):
        first = self.run_once()
        snap = read_json(self.root / "data" / "snapshots" / (first["snapshot_id"] + ".json"))
        decisions = read_json(self.root / "outputs" / first["run_id"] / "decisions.json")
        future = read_json(self.root / "demo-input" / "future.json")
        bar = next(b for b in future["bars"] if b["instrument_id"] == "TSE:0001")
        for key in ("adj_open", "adj_high", "adj_low", "adj_close"):
            bar[key] /= 2
        bar["prior_close_rebase_factor"] = .5
        config = read_json(CONFIG)
        config["roundtrip_cost_bps"] = 30
        outcomes, _ = evaluate(snap, decisions, future, config, self.fixture["next_session"])
        value = next(o for o in outcomes if o["instrument_id"] == "TSE:0001")
        self.assertAlmostEqual(value["discovery_return"], 112/99-1)
        self.assertAlmostEqual(value["open_close_net_proxy_return"], 105/102-1-.003)
        bar["prior_close_rebase_factor"] = None
        outcomes, _ = evaluate(snap, decisions, future, config, self.fixture["next_session"])
        self.assertEqual(next(o for o in outcomes if o["instrument_id"] == "TSE:0001")["outcome_status"], "adjustment_unknown")

    def test_holiday_unknown_and_manifest_integrity(self):
        first = self.run_once()
        with self.assertRaisesRegex(RuntimeError, "artifact failed"):
            path = self.root / "outputs" / first["run_id"] / "decisions.json"
            path.write_text("tampered", encoding="utf-8")
            replay(self.root, first["run_id"])
        with self.assertRaisesRegex(RuntimeError, "SKIPPED_NON_TRADING_DAY"):
            provider = FileProvider(self.root / "demo-input")
            data, _ = provider.load()
            disclosure, _ = provider.load_disclosures()
            validate_input(data, disclosure, "2026-09-21", self.fixture["as_of"])

    def test_cutoff_revisions_and_provider_unavailable(self):
        first = self.run_once()
        snap = read_json(self.root / "data" / "snapshots" / (first["snapshot_id"] + ".json"))
        self.assertEqual(snap["rejected_disclosures"][0]["reason"], "published_after_as_of")
        source = self.root / "demo-input" / "source.json"
        metadata = read_json(source)
        metadata["available_at"] = self.fixture["next_session"] + "T08:00:00+09:00"
        write_json(source, metadata)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.run_once()
        con = sqlite3.connect(self.root / "data" / "state.sqlite")
        try:
            self.assertEqual(con.execute("SELECT status FROM run_attempts ORDER BY rowid DESC LIMIT 1").fetchone()[0], "BLOCKED_DATA")
        finally:
            con.close()

    def test_thresholds_and_unknown_outcome(self):
        first = self.run_once()
        snap = read_json(self.root / "data" / "snapshots" / (first["snapshot_id"] + ".json"))
        decisions = read_json(self.root / "outputs" / first["run_id"] / "decisions.json")
        original = next(d for d in decisions if d["instrument_id"] == "TSE:0001")
        self.assertAlmostEqual(original["rel_volume_20d"], 2)
        self.assertAlmostEqual(original["high_distance_20d"], 99/101-1)
        self.assertEqual(original["stage"], "PREMOVE")
        future = read_json(self.root / "demo-input" / "future.json")
        future["bars"] = [b for b in future["bars"] if b["instrument_id"] != "TSE:0001"]
        outcomes, metrics = evaluate(snap, decisions, future, read_json(CONFIG), self.fixture["next_session"])
        self.assertEqual(next(o for o in outcomes if o["instrument_id"] == "TSE:0001")["outcome_status"], "price_unknown")
        self.assertIsNone(metrics["by_stage"]["PREMOVE"]["hit_rate"])
        self.assertEqual(metrics["by_stage"]["PREMOVE"]["unknown"], 1)

    def test_exact_threshold_boundary(self):
        first = self.run_once()
        snap = read_json(self.root / "data" / "snapshots" / (first["snapshot_id"] + ".json"))
        bars = [b for b in snap["data"]["bars"] if b["instrument_id"] == "TSE:0001"]
        for bar in bars[:-1]:
            bar["high"] = bar["adj_high"] = 110.0
        target = bars[-1]
        target["close"] = target["adj_close"] = 105.0
        target["volume"] = target["adj_volume"] = 1500000
        target["high"] = target["adj_high"] = 105.0
        self.assertEqual(next(d for d in screen(snap, read_json(CONFIG), "boundary")[0] if d["instrument_id"] == "TSE:0001")["stage"], "PREMOVE")
        target["volume"] = target["adj_volume"] = 1499999
        self.assertEqual(next(d for d in screen(snap, read_json(CONFIG), "below")[0] if d["instrument_id"] == "TSE:0001")["stage"], "NONE")

    def test_hypothesis_ledger_and_no_news_route(self):
        from alpha_loop.pipeline import add_hypothesis
        add_hypothesis(self.root, "H001", "出来高増で初動を捕捉", "日足", "価格基準A", "2026-10-01", "2026-12-31")
        con = sqlite3.connect(self.root / "data" / "state.sqlite")
        try:
            self.assertEqual(con.execute("SELECT state FROM hypotheses WHERE hypothesis_id='H001'").fetchone()[0], "DRAFT")
        finally:
            con.close()
        first = self.run_once()
        self.assertEqual(first["config"]["cloud_enabled"], False)
        self.assertEqual(first["config"]["orders_enabled"], False)


if __name__ == "__main__":
    unittest.main()
