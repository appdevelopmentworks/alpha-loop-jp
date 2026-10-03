from __future__ import annotations

import csv
import io
import tempfile
import unittest
from pathlib import Path

from alpha_loop.common import read_json, write_json
from alpha_loop.csv_input import create_csv_fixture, derive_ranking, import_csv
from alpha_loop.provider import FileProvider
from alpha_loop.retrospective import FileRankingProvider, retrospective, replay_retrospective

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"


class CsvInputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "export"
        self.fixture = create_csv_fixture(self.input)
        self.spec_path = self.input / "import.json"

    def tearDown(self):
        self.temp.cleanup()

    def imported(self):
        return import_csv(self.root, self.spec_path, self.fixture["next_session"])

    def test_csv_to_derived_ranking_to_prior_replay(self):
        first, second = self.imported(), self.imported()
        self.assertEqual(first, second)
        self.assertTrue((self.root / "data" / "code_bundles" / first["code_bundle_id"] / "src" / "alpha_loop" / "cli.py").is_file())
        directory = Path(first["input_dir"])
        ranking = derive_ranking(self.root, directory, self.fixture["next_session"])
        self.assertEqual(ranking, derive_ranking(self.root, directory, self.fixture["next_session"]))
        self.assertEqual(ranking["ranking_count"], 2)
        self.assertEqual(ranking["unknown_count"], 1)
        meta, ranked, _ = FileRankingProvider(Path(ranking["ranking_input"])).load()
        self.assertEqual(meta["ranking_method"], "split_adjusted_close_to_close")
        self.assertEqual(ranked[0]["symbol"], "0001")
        self.assertAlmostEqual(ranked[0]["listed_return"], 110/99-1)
        study = retrospective(self.root, Path(ranking["ranking_input"]), directory, CONFIG)
        rows = read_json(Path(study["output_path"]) / "decisions.json")
        self.assertEqual(rows[0]["stage"], "PREMOVE")
        self.assertEqual(rows[1]["stage"], "EARLY")
        self.assertEqual(study["provenance"]["after_cutoff_bars_removed"], 6)
        self.assertTrue(replay_retrospective(self.root, study["study_id"])["replay_verified"])

    def test_real_reconstruction_keeps_actual_timestamps_and_derived_time(self):
        spec = read_json(self.spec_path)
        spec.update(data_grade="reconstructed", acquisition_method="licensed_export", usage_basis="test declared permission")
        write_json(self.spec_path, spec)
        imported = self.imported()
        data, _ = FileProvider(Path(imported["input_dir"])).load()
        self.assertEqual(data["source"]["fetched_at"], spec["fetched_at"])
        ranking = derive_ranking(self.root, Path(imported["input_dir"]), self.fixture["next_session"])
        meta, rows, _ = FileRankingProvider(Path(ranking["ranking_input"])).load()
        self.assertEqual(meta["acquisition_method"], "derived_prices")
        self.assertGreater(meta["fetched_at"], self.fixture["next_session"])
        study = retrospective(self.root, Path(ranking["ranking_input"]), Path(imported["input_dir"]), CONFIG)
        self.assertEqual(study["data_grade"], "reconstructed")
        self.assertFalse(study["prediction_score_eligible"])

    def test_optional_turnover_and_missing_factor_is_blocked_and_logged(self):
        spec = read_json(self.spec_path)
        del spec["files"][0]["columns"]["turnover_jpy"]
        write_json(self.spec_path, spec)
        imported = self.imported()
        data, _ = FileProvider(Path(imported["input_dir"])).load()
        self.assertTrue(all(b["turnover_jpy"] is None for b in data["bars"] if b["instrument_id"] == "TSE:0001"))
        del spec["files"][0]["columns"]["split_factor"]
        write_json(self.spec_path, spec)
        with self.assertRaisesRegex(ValueError, "explicit split_factor"):
            self.imported()
        attempt = next(read_json(p) for p in (self.root / "data" / "operations" / "import_attempts").glob("*.json") if read_json(p)["status"] == "FAILED")
        self.assertEqual(attempt["status"], "FAILED")

    def test_split_factor_price_and_volume_are_consistent(self):
        spec = read_json(self.spec_path)
        file = self.input / spec["files"][0]["file"]
        with file.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        for row in rows:
            row["分割調整係数"] = ".5"
        from alpha_loop.common import write_csv
        write_csv(file, fields, rows)
        data, _ = FileProvider(Path(self.imported()["input_dir"])).load()
        first = data["bars"][0]
        self.assertEqual(first["adj_close"], first["close"] / 2)
        self.assertEqual(first["adj_volume"], first["volume"] * 2)
        self.assertEqual(first["turnover_jpy"], float(rows[0]["売買代金"]))

    def test_invalid_csv_duplicate_date_and_unsupported_action(self):
        spec = read_json(self.spec_path)
        spec["corporate_action_scope"] = "includes_rights_issues"
        write_json(self.spec_path, spec)
        with self.assertRaisesRegex(ValueError, "separate adjustment"):
            self.imported()
        spec["corporate_action_scope"] = "split_and_consolidation_only"
        write_json(self.spec_path, spec)
        file = self.input / "0001.csv"
        raw = file.read_text(encoding="utf-8-sig")
        file.write_text(raw + raw.splitlines()[1] + "\n", encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "duplicate daily_bar"):
            self.imported()

    def test_path_escape_tamper_and_empty_ranking(self):
        imported = self.imported()
        ranking = derive_ranking(self.root, Path(imported["input_dir"]), self.fixture["next_session"], min_return=1)
        self.assertEqual(ranking["ranking_count"], 0)
        (Path(imported["input_dir"]) / "bars.json").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "normalized import"):
            self.imported()
        with self.assertRaisesRegex(RuntimeError, "normalized import"):
            derive_ranking(self.root, Path(imported["input_dir"]), self.fixture["next_session"])
        spec = read_json(self.spec_path)
        spec["files"][0]["file"] = "../outside.csv"
        write_json(self.spec_path, spec)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.imported()

    def test_exact_ranking_threshold_and_code_archive_integrity(self):
        file = self.input / "0001.csv"
        with file.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        rows[-1]["終値"] = rows[-1]["高値"] = "118.8"
        from alpha_loop.common import write_csv
        write_csv(file, fields, rows)
        imported = self.imported()
        ranking = derive_ranking(self.root, Path(imported["input_dir"]), self.fixture["next_session"], min_return=.20)
        self.assertEqual(ranking["ranking_count"], 1)
        code = self.root / "data" / "code_bundles" / imported["code_bundle_id"] / "src" / "alpha_loop" / "csv_input.py"
        code.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "code bundle file"):
            self.imported()


if __name__ == "__main__":
    unittest.main()
