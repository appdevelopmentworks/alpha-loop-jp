from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from alpha_loop.common import read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.operations import daily, doctor


CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = create(self.root / "incoming" / "current")

    def tearDown(self):
        self.temp.cleanup()

    def test_daily_is_idempotent_and_evaluates_only_after_screening(self):
        args = (self.root, self.root / "incoming" / "current", CONFIG,
                self.fixture["target_session"], self.fixture["as_of"])
        first = daily(*args)
        second = daily(*args[:-1], self.fixture["target_session"] + "T20:01:00+09:00")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(first["first_recorded"])
        self.assertFalse(second["first_recorded"])
        self.assertFalse(first["prediction_eligible"])
        self.assertTrue(Path(first["candidate_csv"]).exists())
        current = self.root / "incoming" / "current"
        following = self.root / "incoming" / "following"
        following.mkdir()
        for name in ("calendar", "instruments", "bars", "source", "disclosures", "future"):
            data = read_json(current / f"{name}.json")
            fetched = self.fixture["next_session"] + "T19:30:00+09:00"
            if name == "calendar":
                data.append(self.fixture["next_session"])
            elif name == "instruments":
                for item in data:
                    item["fetched_at"] = fetched
            elif name == "bars":
                for item in data:
                    item["fetched_at"] = fetched
                for future in read_json(current / "future.json")["bars"]:
                    prices = {key: future[key] for key in ("adj_open", "adj_high", "adj_low", "adj_close")}
                    data.append({"instrument_id": future["instrument_id"],
                                 "session_date": self.fixture["next_session"], "bar_status": future["bar_status"],
                                 "open": prices["adj_open"], "high": prices["adj_high"],
                                 "low": prices["adj_low"], "close": prices["adj_close"],
                                 **prices, "volume": 1000000, "adj_volume": 1000000,
                                 "turnover_jpy": (prices["adj_close"] or 0) * 1000000,
                                 "adjustment_basis": "split_only", "fetched_at": fetched})
            elif name == "source":
                data["fetched_at"] = data["available_at"] = fetched
            write_json(following / f"{name}.json", data)
        evaluated = daily(self.root, following, CONFIG, self.fixture["next_session"],
                          self.fixture["next_session"] + "T20:00:00+09:00",
                          evaluate_run_id=first["run_id"], through=self.fixture["next_session"])
        self.assertTrue(Path(evaluated["outcome_csv"]).exists())
        self.assertEqual(len(list((self.root / "data" / "operations" / "receipts").glob("*.json"))), 2)

    def test_missing_file_creates_failed_attempt_and_no_receipt(self):
        with self.assertRaises(FileNotFoundError):
            daily(self.root, self.root / "missing", CONFIG,
                  self.fixture["target_session"], self.fixture["as_of"])
        attempts = list((self.root / "data" / "operations" / "attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(read_json(attempts[0])["status"], "FAILED")
        self.assertFalse((self.root / "data" / "operations" / "receipts").exists())

    def test_future_evaluation_date_rejected_before_attempt(self):
        with self.assertRaisesRegex(ValueError, "future evaluation date"):
            daily(self.root, self.root / "incoming" / "current", CONFIG,
                  self.fixture["target_session"], self.fixture["as_of"],
                  "some-run", self.fixture["next_session"])

    def test_doctor_is_read_only(self):
        result = doctor(self.root, self.root / "incoming" / "current")
        self.assertTrue(all(result["input_files"].values()))
        self.assertGreater(result["free_disk_bytes"], 0)
        self.assertFalse((self.root / "data").exists())
