from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, read_json, write_csv, write_json
from alpha_loop.pipeline import list_hypotheses, run
from alpha_loop.reporting import LocalQwen, qwen_retrospective_report
from alpha_loop.retrospective import (FileRankingProvider, RANKING_FIELDS, create_ranking_fixture,
                                     register_hypothesis, replay_retrospective, retrospective)

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"


class RetrospectiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.fixture = create_ranking_fixture(self.input)

    def tearDown(self):
        self.temp.cleanup()

    def study(self):
        return retrospective(self.root, self.input, self.input, CONFIG)

    def rows(self, study):
        return read_json(Path(study["output_path"]) / "decisions.json")

    def test_prior_features_full_universe_unmatched_and_replay(self):
        first, second = self.study(), self.study()
        self.assertEqual(first["study_id"], second["study_id"])
        rows = self.rows(first)
        self.assertEqual(len(rows), 6)
        premove = next(r for r in rows if r["symbol"] == "0001")
        self.assertEqual(premove["stage"], "PREMOVE")
        self.assertAlmostEqual(premove["rel_volume_20d"], 2)
        self.assertAlmostEqual(premove["ret_5d"], -.01)
        self.assertTrue(premove["in_ranking"])
        self.assertEqual(first["unmatched_count"], 1)
        self.assertEqual(first["matched_count"], 2)
        self.assertTrue(all(not r["prediction_score_eligible"] for r in rows))
        self.assertTrue(replay_retrospective(self.root, first["study_id"])["replay_verified"])
        comparison = read_json(Path(first["output_path"]) / "comparison.json")
        self.assertNotIn("hit_rate", comparison)
        self.assertEqual(comparison["groups"]["not_listed"]["count"], 4)
        self.assertTrue((Path(first["output_path"]) / "ranked_prior.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_today_prices_and_material_do_not_enter_prior_features(self):
        before = self.rows(self.study())
        bars = read_json(self.input / "bars.json")
        bars.append({**bars[-1], "session_date": self.fixture["next_session"], "adj_close": 999999, "close": 999999})
        write_json(self.input / "bars.json", bars)
        disclosures = read_json(self.input / "disclosures.json")
        disclosures.append({**disclosures[0], "disclosure_id": "today", "instrument_id": "TSE:0001",
                            "published_at": self.fixture["next_session"] + "T08:00:00+09:00"})
        write_json(self.input / "disclosures.json", disclosures)
        study = self.study()
        after = self.rows(study)
        self.assertEqual(study["provenance"]["after_cutoff_bars_removed"], 1)
        comparable = lambda rows: [{key: value for key, value in row.items() if key not in ("decision_id", "run_id", "study_id")} for row in rows]
        self.assertEqual(comparable(before), comparable(after))
        features = read_json(Path(study["output_path"]) / "features.json")
        self.assertTrue(all(not ref.endswith(self.fixture["next_session"]) for row in features for ref in row.get("feature_bar_ids", [])))

    def test_reconstruction_preserves_actual_retrieval_and_cutoff_revisions(self):
        collected = self.fixture["next_session"] + "T19:00:00+09:00"
        source = read_json(self.input / "source.json")
        source.update(data_grade="reconstructed", fetched_at=collected, available_at=collected)
        write_json(self.input / "source.json", source)
        meta = read_json(self.input / "ranking_source.json")
        meta.update(data_grade="observed", acquisition_method="manual")
        write_json(self.input / "ranking_source.json", meta)
        disclosures = read_json(self.input / "disclosures.json")
        disclosures[0].update(first_seen_at=collected, fetched_at=collected)
        disclosures.append({**disclosures[0], "revision_id": "2", "published_at": self.fixture["next_session"] + "T12:00:00+09:00"})
        write_json(self.input / "disclosures.json", disclosures)
        study = self.study()
        saved = read_json(self.root / "data" / "retrospectives" / (study["snapshot_hash"] + ".json"))
        self.assertEqual(saved["prior"]["data"]["source"]["fetched_at"], collected)
        self.assertEqual(saved["prior"]["usable_disclosures"][0]["first_seen_at"], collected)
        self.assertEqual(saved["prior"]["usable_disclosures"][0]["revision_id"], "1")
        self.assertEqual(study["data_grade"], "reconstructed")
        self.assertEqual(study["provenance"]["availability_basis"], "published_only_reconstruction")
        self.assertFalse(study["prediction_score_eligible"])

    def test_saved_prior_snapshot(self):
        prior = run(self.root, self.input, CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        study = retrospective(self.root, self.input, prior_run_id=prior["run_id"])
        self.assertEqual(study["provenance"]["availability_basis"], "saved_cutoff")
        self.assertEqual(self.rows(study)[0]["rel_volume_20d"], 2)
        snapshot = self.root / "data" / "snapshots" / (prior["snapshot_id"] + ".json")
        saved = read_json(snapshot)
        saved["as_of"] = "2026-09-17T21:00:00+09:00"
        write_json(snapshot, saved)
        with self.assertRaisesRegex(RuntimeError, "snapshot hash"):
            retrospective(self.root, self.input, prior_run_id=prior["run_id"])

    def test_empty_ranking_duplicates_missing_history_and_holiday(self):
        meta, rows, _ = FileRankingProvider(self.input).load()
        write_csv(self.input / "ranking.csv", RANKING_FIELDS, rows + [rows[0]])
        self.assertEqual(FileRankingProvider(self.input).load()[0]["duplicate_rows"], 1)
        self.assertEqual(self.study()["matched_count"], 2)
        write_csv(self.input / "ranking.csv", RANKING_FIELDS, [])
        empty = self.study()
        self.assertEqual(empty["matched_count"], 0)
        self.assertEqual(len(self.rows(empty)), 6)
        self.assertEqual(read_json(Path(empty["output_path"]) / "hypothesis_drafts.json"), [])
        self.assertEqual(len((Path(empty["output_path"]) / "ranked_prior.csv").read_text(encoding="utf-8-sig").splitlines()), 1)
        bars = read_json(self.input / "bars.json")
        write_json(self.input / "bars.json", [b for b in bars if not (b["instrument_id"] == "TSE:0001" and b["session_date"] == bars[1]["session_date"])])
        self.assertEqual(self.rows(self.study())[0]["stage"], "DATA_HOLD")
        meta["ranking_session"] = "2026-09-21"
        meta["fetched_at"] = "2026-09-21T20:00:00+09:00"
        write_json(self.input / "ranking_source.json", meta)
        with self.assertRaisesRegex(ValueError, "known trading session"):
            self.study()

    def test_invalid_times_codes_duplicates_and_mixed_grades(self):
        meta, rows, _ = FileRankingProvider(self.input).load()
        rows[0]["symbol"] = "1"
        write_csv(self.input / "ranking.csv", RANKING_FIELDS, rows)
        with self.assertRaisesRegex(ValueError, "symbol conflicts"):
            self.study()
        rows[0]["symbol"] = "0001"
        conflicting = {**rows[0], "rank": 42}
        write_csv(self.input / "ranking.csv", RANKING_FIELDS, rows + [conflicting])
        with self.assertRaisesRegex(ValueError, "conflicting ranking"):
            self.study()
        rows[0]["source_updated_at"] = self.fixture["target_session"] + "T20:00:00+09:00"
        write_csv(self.input / "ranking.csv", RANKING_FIELDS, rows)
        with self.assertRaisesRegex(ValueError, "date/time mismatch"):
            self.study()
        rows[0]["source_updated_at"] = self.fixture["next_session"] + "T18:00:00+09:00"
        write_csv(self.input / "ranking.csv", RANKING_FIELDS, rows)
        meta.update(data_grade="observed", acquisition_method="manual")
        write_json(self.input / "ranking_source.json", meta)
        with self.assertRaisesRegex(ValueError, "synthetic grades"):
            self.study()

    def test_known_late_availability_and_future_instrument_attribute(self):
        bars = read_json(self.input / "bars.json")
        bars[1]["available_at"] = self.fixture["next_session"] + "T08:00:00+09:00"
        write_json(self.input / "bars.json", bars)
        self.assertEqual(self.rows(self.study())[0]["stage"], "DATA_HOLD")
        instruments = read_json(self.input / "instruments.json")
        instruments[0]["effective_date"] = self.fixture["next_session"]
        write_json(self.input / "instruments.json", instruments)
        with self.assertRaisesRegex(ValueError, "future instrument"):
            self.study()

    def test_tampering_is_detected(self):
        study = self.study()
        path = Path(study["output_path"]) / "retrospective.csv"
        path.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "artifact failed"):
            replay_retrospective(self.root, study["study_id"])
        with self.assertRaisesRegex(RuntimeError, "artifact failed"):
            self.study()

    def test_split_basis_invariance_and_raw_integrity(self):
        before = self.rows(self.study())[0]
        bars = read_json(self.input / "bars.json")
        for bar in bars:
            if bar["instrument_id"] == "TSE:0001":
                for field in ("adj_open", "adj_high", "adj_low", "adj_close"):
                    bar[field] /= 2
                bar["adj_volume"] *= 2
        write_json(self.input / "bars.json", bars)
        study = self.study()
        after = self.rows(study)[0]
        self.assertEqual(before["stage"], after["stage"])
        for field in ("ret_1d", "ret_5d", "rel_volume_20d", "high_distance_20d", "ma25_gap"):
            self.assertAlmostEqual(before[field], after[field])
        saved = read_json(self.root / "data" / "retrospectives" / (study["snapshot_hash"] + ".json"))
        (self.root / saved["raw_objects"][0]["stored_at"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "raw object hash"):
            replay_retrospective(self.root, study["study_id"])

    def test_failed_input_is_recorded(self):
        (self.input / "ranking.csv").unlink()
        with self.assertRaises(FileNotFoundError):
            self.study()
        attempt = read_json(next((self.root / "data" / "operations" / "retrospective_attempts").glob("*.json")))
        self.assertEqual(attempt["status"], "FAILED")

    def test_holdout_registration_is_idempotent_and_cannot_overlap_discovery(self):
        study = self.study()
        args = (self.root, study["study_id"], "2026-10-01", "2026-12-31")
        self.assertEqual(register_hypothesis(*args), register_hypothesis(*args))
        self.assertEqual(len(list_hypotheses(self.root)), 1)
        self.assertEqual(list_hypotheses(self.root)[0]["state"], "DRAFT")
        with self.assertRaisesRegex(ValueError, "follow discovery"):
            register_hypothesis(self.root, study["study_id"], self.fixture["next_session"], "2026-12-31")
        with self.assertRaisesRegex(ValueError, "different period"):
            register_hypothesis(self.root, study["study_id"], "2026-11-01", "2026-12-31")

    def test_optional_qwen_uses_cache_and_cannot_modify_features(self):
        study = self.study()
        artifact = Path(study["output_path"]) / "retrospective.csv"
        original = digest(artifact.read_bytes())
        provider = LocalQwen("http://127.0.0.1:8000", "Qwen/test", "revision-1", {"runtime_config_version": "test-v1"})
        response = {"model": "Qwen/test", "choices": [{"message": {"content": '{"hypotheses":[{"condition_ids":["volume_ge_1_5","near_high_ge_minus_0_05"],"reason_code":"volume_expansion"}]}'}}]}
        with patch.object(provider, "ask", return_value=response) as mock:
            first = qwen_retrospective_report(self.root, study["study_id"], provider)
            second = qwen_retrospective_report(self.root, study["study_id"], provider)
        self.assertEqual(first, second)
        self.assertRegex(first["cache_key"], r"^[a-f0-9]{64}$")
        self.assertEqual(first["cache_key"], Path(first["report_path"]).stem)
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(mock.call_args.args[0]["feature_session"], self.fixture["target_session"])
        self.assertNotIn("ranked_prior_states", mock.call_args.args[0])
        self.assertEqual(mock.call_args.args[0]["comparison_directions_listed_vs_not_listed"]["rel_volume_20d"], "higher")
        rendered = Path(first["draft_path"]).read_text(encoding="utf-8")
        self.assertIn("| TSE:0001 | PREMOVE | True | 2.0 | -0.01 |", rendered)
        self.assertEqual(digest(artifact.read_bytes()), original)
        self.assertTrue(read_json(Path(first["report_path"]))["needs_review"])
        self.assertTrue(read_json(Path(first["report_path"]))["structure_validated"])
        self.assertIn("固定した価格基準A", rendered)
        self.assertTrue(replay_retrospective(self.root, study["study_id"])["replay_verified"])

    def test_qwen_truncated_response_is_not_cached_as_success(self):
        study = self.study()
        provider = LocalQwen("http://127.0.0.1:8000", "Qwen/test", "revision-1", {"runtime_config_version": "test-v1"})
        response = {"model": "Qwen/test", "choices": [{"finish_reason": "length", "message": {"content": "途中までの説明"}}]}
        with patch.object(provider, "ask", return_value=response):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                qwen_retrospective_report(self.root, study["study_id"], provider)
        self.assertFalse((self.root / "data" / "qwen_cache").exists())
        attempt = read_json(next((self.root / "data" / "operations" / "qwen_attempts").glob("*.json")))
        self.assertEqual(attempt["status"], "INVALID_RESPONSE")
        self.assertEqual(attempt["response"]["choices"][0]["finish_reason"], "length")


if __name__ == "__main__":
    unittest.main()
