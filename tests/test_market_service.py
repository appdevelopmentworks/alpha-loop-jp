from __future__ import annotations

import copy
import io
import tempfile
import unittest
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import JST, atomic_bytes, digest, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.market import MarketRateLimited, _history, collect, completed_session, jpx_universe, normalize_history, sessions
from alpha_loop.processes import run_bounded
from alpha_loop.pipeline import evaluate_run, run
from alpha_loop.provider import FileProvider
from alpha_loop.service import _future, market_summary, operate

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / "configs" / "baseline.json"


class MarketServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = create(self.root / "input", without_turnover=True)
        self.config = read_json(PROJECT / "configs" / "hermes_operations.json")
        self.config.update(provider="file", input_dir="input", strategy_config=str(CONFIG))
        self.config.update(materials_config=None, weekly_research_enabled=False)
        self.config_path = self.root / "operation.json"
        write_json(self.config_path, self.config)
        self.instrument = {"instrument_id": "TSE:0001", "symbol": "0001"}

    def tearDown(self):
        self.temp.cleanup()

    def frame(self):
        import pandas as pd
        return pd.DataFrame({"Open": [50, 55], "High": [52, 57], "Low": [49, 54],
                             "Close": [51, 56], "Adj Close": [48, 55], "Volume": [100, 200],
                             "Stock Splits": [2, 0], "Dividends": [3, 0]},
                            index=pd.date_range("2026-09-28", periods=2, tz="Asia/Tokyo"))

    def test_native_adjustments_retained_and_future_discarded(self):
        bars = normalize_history(self.frame(), self.instrument, "2026-09-28", "2026-09-30T12:00:00+09:00")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["adj_close"], 51)  # neither split divided again nor dividend-adjusted
        self.assertEqual(bars[0]["adj_volume"], 100)
        self.assertEqual(bars[0]["stock_split_event"], 2)
        self.assertIsNone(bars[0]["turnover_jpy"])
        bad = self.frame()
        bad.iloc[0, bad.columns.get_loc("High")] = 40
        self.assertEqual(normalize_history(bad, self.instrument, "2026-09-28", "2026-09-30T12:00:00+09:00")[0]["bar_status"], "missing")

    def test_calendar_holiday_and_close_gate(self):
        now = datetime(2026, 9, 30, 19, tzinfo=JST)
        calendar = sessions(now)
        self.assertNotIn("2026-09-21", calendar)
        self.assertNotIn("2026-09-23", calendar)
        self.assertEqual(completed_session(calendar, now), "2026-09-29")
        self.assertEqual(completed_session(calendar, now.replace(hour=20)), "2026-09-30")

    def test_native_rate_limit_stops_retries_and_persists_cooldown(self):
        with patch("alpha_loop.market._fetch_frame", side_effect=MarketRateLimited("429")) as request:
            with self.assertRaises(MarketRateLimited):
                _history(self.root, self.instrument, "2026-09-29", "2026-01-01", False)
            with self.assertRaises(MarketRateLimited):
                _history(self.root, self.instrument, "2026-09-29", "2026-01-01", False)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(read_json(self.root / "data" / "market" / "cooldown.json")["reason"], "provider_rate_limit")

    def test_process_timeout_is_bounded(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.3)

    def test_windows_transient_file_lock_retry(self):
        import os
        original = os.replace
        count = 0
        def replace(src, dst):
            nonlocal count
            count += 1
            if count < 3:
                raise PermissionError("temporarily locked")
            original(src, dst)
        with patch("alpha_loop.common.os.replace", side_effect=replace), patch("alpha_loop.common.time.sleep"):
            atomic_bytes(self.root / "status.json", b"saved")
        self.assertEqual((self.root / "status.json").read_bytes(), b"saved")
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_reconstructed_evaluation_is_never_prediction_scoring(self):
        source_path = self.root / "input" / "source.json"
        source = read_json(source_path)
        source["data_grade"] = "reconstructed"
        write_json(source_path, source)
        calendar_path = self.root / "input" / "calendar.json"
        write_json(calendar_path, read_json(calendar_path) + [self.fixture["next_session"]])
        future_path = self.root / "input" / "future.json"
        future = read_json(future_path)
        future["data_grade"] = "reconstructed"
        write_json(future_path, future)
        with patch("alpha_loop.pipeline.now_iso", return_value=self.fixture["as_of"]):
            manifest = run(self.root, self.root / "input", CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        self.assertFalse(manifest["history_only"])
        evaluation = evaluate_run(self.root, manifest["run_id"], self.root / "input", self.fixture["next_session"])
        self.assertFalse(read_json(Path(evaluation["path"]) / "evaluation_manifest.json")["prediction_score_eligible"])

    def test_jpx_class_shares_excluded_and_alphanumeric_codes_preserved(self):
        import openpyxl
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(["日付", "コード", "銘柄名", "市場・商品区分"])
        for code in (1301, "130A", 25935):
            sheet.append([20260831, code, "test", "スタンダード（内国株式）"])
        raw = io.BytesIO()
        book.save(raw)
        with patch("alpha_loop.market.urllib.request.urlopen", return_value=io.BytesIO(raw.getvalue())):
            result = jpx_universe(self.root, ["Standard"])
        self.assertEqual([i["symbol"] for i in result["instruments"]], ["1301", "130A"])
        self.assertEqual(result["excluded"][0]["reason"], "class_share")

    def test_native_history_resume_and_tamper_detection(self):
        with patch("alpha_loop.market._fetch_frame", return_value=(self.frame(), self.frame().to_csv().encode("utf-8"))) as request:
            first = _history(self.root, self.instrument, "2026-09-29", "2026-01-01", False)
            second = _history(self.root, self.instrument, "2026-09-29", "2026-01-01", False)
        self.assertEqual(first, second)
        self.assertEqual(request.call_count, 1)
        (self.root / "data" / "raw" / first[1]["raw_hash"]).write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            _history(self.root, self.instrument, "2026-09-29", "2026-01-01", False)

    def test_partial_collection_retains_unknown_and_all_failure_blocks(self):
        data, _ = FileProvider(self.root / "input").load()
        instruments = data["instruments"][:2]
        config = {**self.config, "provider": "yfinance"}
        fetched = data["source"]["fetched_at"]
        universe = {"instruments": instruments, "fetched_at": fetched, "effective_date": instruments[0]["effective_date"], "raw_hash": "test"}
        history = [b for b in data["bars"] if b["instrument_id"] == instruments[0]["instrument_id"]]
        original = {"params": {"ticker": "0001.T"}, "raw_hash": "test", "fetched_at": fetched}
        def fetch(root, instrument, *args):
            if instrument == instruments[1]:
                raise ValueError("unavailable")
            return history, original
        with patch("alpha_loop.market.sessions", return_value=data["calendar"]), patch("alpha_loop.market.jpx_universe", return_value=universe), patch("alpha_loop.market._history", side_effect=fetch):
            result = collect(self.root, config, self.fixture["target_session"])
        self.assertEqual(result["missing_target_count"], 1)
        source = read_json(Path(result["input_dir"]) / "source.json")
        self.assertEqual(source["data_grade"], "reconstructed")
        with patch("alpha_loop.market.sessions", return_value=data["calendar"]), patch("alpha_loop.market.jpx_universe", return_value=universe), patch("alpha_loop.market._history", side_effect=ValueError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "all target prices unavailable"):
                collect(self.root, config, self.fixture["target_session"])

    def test_operation_ai_failure_preserves_csv_retry_and_duplicate(self):
        with patch("alpha_loop.service._qwen", side_effect=RuntimeError("Docker offline")):
            first = operate(self.root, self.config_path)
        self.assertEqual(first["qwen_status"], "FAILED")
        csv_hash = digest(Path(first["candidate_csv"]).read_bytes())
        with patch("alpha_loop.service._qwen", return_value={"daily_report": "mock"}) as ai:
            duplicate = operate(self.root, self.config_path)
            self.assertFalse(duplicate["first_recorded"])
            self.assertEqual(ai.call_count, 0)
            retried = operate(self.root, self.config_path, retry_ai=True)
        self.assertEqual(retried["run_id"], first["run_id"])
        self.assertEqual(retried["qwen_status"], "SUCCEEDED")
        self.assertEqual(digest(Path(first["candidate_csv"]).read_bytes()), csv_hash)
        Path(first["market_summary_csv"]).write_text("tampered")
        with self.assertRaisesRegex(RuntimeError, "artifact hash mismatch"):
            operate(self.root, self.config_path)

    def test_missing_prior_prices_remain_unknown_in_retrospective(self):
        bars_path = self.root / "input" / "bars.json"
        bars = read_json(bars_path)
        previous = read_json(self.root / "input" / "calendar.json")[-2]
        bars = [b for b in bars if not (b["instrument_id"] == "TSE:0002" and b["session_date"] == previous)]
        write_json(bars_path, bars)
        result = operate(self.root, self.config_path, no_ai=True)
        rows = read_json(Path(result["retrospective_csv"]).parent / "decisions.json")
        row = next(r for r in rows if r["instrument_id"] == "TSE:0002")
        self.assertEqual(row["stage"], "DATA_HOLD")
        self.assertIsNone(row["ret_1d"])

    def test_m5_failure_preserves_csv_and_later_refresh_recovers(self):
        with patch("alpha_loop.research.refresh", side_effect=RuntimeError("comparison unavailable")):
            first = operate(self.root, self.config_path, no_ai=True)
        self.assertEqual(first["status"], "SUCCEEDED_WITH_M5_FAILURE")
        self.assertEqual(first["m5"]["status"], "FAILED")
        before = digest(Path(first["candidate_csv"]).read_bytes())
        recovered = operate(self.root, self.config_path, no_ai=True)
        self.assertEqual(recovered["m5"]["status"], "NOT_REGISTERED")
        self.assertEqual(recovered["status"], "SUCCEEDED")
        self.assertEqual(digest(Path(first["candidate_csv"]).read_bytes()), before)

    def test_automatic_next_session_evaluation_and_unknown_execution(self):
        first = operate(self.root, self.config_path, no_ai=True)
        data, _ = FileProvider(self.root / "input").load()
        future = read_json(self.root / "input" / "future.json")
        next_day = self.fixture["next_session"]
        fetched = next_day + "T19:30:00+09:00"
        data["calendar"].append(next_day)
        for bar in future["bars"]:
            prices = {key: bar[key] for key in ("adj_open", "adj_high", "adj_low", "adj_close")}
            data["bars"].append({**bar, **prices, "open": prices["adj_open"], "high": prices["adj_high"],
                                 "low": prices["adj_low"], "close": prices["adj_close"], "volume": 1000000,
                                 "adj_volume": 1000000, "turnover_jpy": None, "fetched_at": fetched})
        for bar in data["bars"]:
            bar["fetched_at"] = fetched
        for instrument in data["instruments"]:
            instrument["fetched_at"] = fetched
        data["source"]["fetched_at"] = data["source"]["available_at"] = fetched
        for name, value in data.items():
            write_json(self.root / "input" / (name + ".json"), value)
        second = operate(self.root, self.config_path, no_ai=True)
        self.assertEqual(len(second["evaluations"]), 1)
        outcomes = read_json(Path(second["evaluations"][0]["outcomes_csv"]).with_suffix(".json"))
        valid = [o for o in outcomes if o["outcome_status"] == "ok"]
        self.assertTrue(valid)
        self.assertTrue(all(o["execution_status"] == "unknown" and o["open_close_proxy_return"] is None for o in valid))
        self.assertFalse(operate(self.root, self.config_path, no_ai=True)["first_recorded"])
        self.assertEqual(first["data_grade"], "synthetic")

    def test_split_rebase_and_unexplained_correction(self):
        manifest = run(self.root, self.root / "input", CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        data, _ = FileProvider(self.root / "input").load()
        target, following = self.fixture["target_session"], self.fixture["next_session"]
        data["calendar"].append(following)
        base = next(b for b in data["bars"] if b["instrument_id"] == "TSE:0001" and b["session_date"] == target)
        base["adj_close"] /= 2
        data["bars"].append({**base, "session_date": following, "stock_split_event": 2})
        path = _future(self.root, manifest["run_id"], data, following, self.root / "forward")
        self.assertEqual(read_json(path / "future.json")["bars"][0]["prior_close_rebase_factor"], .5)
        base["adj_close"] += 1
        _future(self.root, manifest["run_id"], data, following, path)
        self.assertIsNone(read_json(path / "future.json")["bars"][0]["prior_close_rebase_factor"])

    def test_snapshot_tampering_rejected_by_next_day_evaluation(self):
        manifest = run(self.root, self.root / "input", CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        path = self.root / "data" / "snapshots" / (manifest["snapshot_id"] + ".json")
        snapshot = read_json(path)
        snapshot["data"]["bars"][0]["adj_close"] = 1000
        write_json(path, snapshot)
        with self.assertRaisesRegex(RuntimeError, "snapshot hash mismatch"):
            evaluate_run(self.root, manifest["run_id"], self.root / "input", self.fixture["next_session"])

    def test_market_comparison_uses_full_denominators_and_unknowns(self):
        data, _ = FileProvider(self.root / "input").load()
        rows = [{"instrument_id": i["instrument_id"], "selected": False} for i in data["instruments"]]
        summary = market_summary(data, rows, self.fixture["target_session"], .1, ["Prime"])[0]
        self.assertEqual(summary["universe_count"], 6)
        self.assertEqual(summary["known_price_pairs"], 5)
        self.assertEqual(summary["unknown_price_pairs"], 1)
        self.assertEqual(summary["gainers_close_threshold"], 1)
        self.assertEqual(summary["rate_among_known_prices"], .2)


if __name__ == "__main__":
    unittest.main()
