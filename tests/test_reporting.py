from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, read_json
from alpha_loop.fixture import create
from alpha_loop.pipeline import run
from alpha_loop.reporting import LocalQwen, json_content, qwen_report
from alpha_loop.hypothesis_plans import validate_plans


CONFIG = Path(__file__).resolve().parents[1] / "configs" / "baseline.json"


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture = create(self.root / "input")
        self.manifest = run(self.root, self.root / "input", CONFIG,
                            fixture["target_session"], fixture["as_of"])
        self.provider = LocalQwen("http://127.0.0.1:8000", "Qwen/test", "revision-1",
                                  {"runtime_config_version": "test-v1"})

    def tearDown(self):
        self.temp.cleanup()

    def test_report_cache_and_price_artifact_unchanged(self):
        run_id = self.manifest["run_id"]
        csv_file = self.root / "outputs" / run_id / "candidates.csv"
        prior = digest(csv_file.read_bytes())
        def answer(facts):
            return {"model": "Qwen/test", "choices": [{"message": {"content": json.dumps({"note_codes": facts["required_note_codes"]})}}]}
        with patch.object(LocalQwen, "ask", side_effect=answer) as mock:
            first = qwen_report(self.root, run_id, self.provider)
            second = qwen_report(self.root, run_id, self.provider)
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(digest(csv_file.read_bytes()), prior)
        saved = read_json(Path(first["report_path"]))
        self.assertEqual(saved["data_grade"], "synthetic")
        self.assertTrue(saved["price_csv_unchanged"])
        self.assertEqual(saved["text_rendered_by"], "Python")
        self.assertEqual(saved["selected_stage_counts"], {"EARLY": 1, "PREMOVE": 1, "WATCH": 1})

    def test_daily_numeric_invention_and_missing_constraints_rejected(self):
        for content in ('21候補です。', '{"note_codes":["unvalidated"]}', '{"note_codes":["unknown_code"]}'):
            response = {"model": "Qwen/test", "choices": [{"message": {"content": content}}]}
            with patch.object(LocalQwen, "ask", return_value=response):
                with self.assertRaises(ValueError):
                    qwen_report(self.root, self.manifest["run_id"], self.provider)

    def test_json_transport_envelope_keeps_strict_hypothesis_validation(self):
        value = '{"hypotheses":[{"condition_ids":["volume_ge_1_5"],"reason_code":"volume_expansion"}]}'
        self.assertEqual(len(validate_plans(json_content('```json\n' + value + '\n```'), "study-test", "synthetic")), 1)
        for text in ('説明です。\n```json\n' + value + '\n```', '```json\n' + value + '\n```\n説明です。'):
            with self.assertRaises(ValueError):
                validate_plans(json_content(text), "study-test", "synthetic")

    def test_response_and_endpoint_validation(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            LocalQwen("https://example.com", "Qwen/test", "revision-1",
                      {"runtime_config_version": "test-v1"})
        with patch.object(LocalQwen, "ask", return_value={"model": "Qwen/test", "choices": []}):
            with self.assertRaisesRegex(ValueError, "invalid Qwen response"):
                qwen_report(self.root, self.manifest["run_id"], self.provider)
