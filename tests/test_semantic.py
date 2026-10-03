from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.pipeline import run
from alpha_loop.semantic import MockSemanticProvider, OpenJevProvider, import_document, shadow, validate_response

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / "configs" / "baseline.json"
QUESTIONS = PROJECT / "configs" / "questions_v1.json"


def response():
    questions = read_json(QUESTIONS)["questions"]
    answers = {}
    for key, definition in questions.items():
        if definition["type"] == "noul":
            answers[key] = {"noul": .8}
        else:
            choices = list(definition["criteria"])
            chosen = choices[0]
            answers[key] = {"choice": chosen, "probabilities": {k: 1.0 if k == chosen else 0.0 for k in choices}, "confidence": 1.0}
    return {"model": "mock-materials-v1", "answers": answers, "usage": {"input_tokens": 20, "output_tokens": 0}}


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = create(self.root / "demo-input")
        self.manifest = run(self.root, self.root / "demo-input", CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        self.source = self.root / "manual.txt"
        self.source.write_text("正式契約を締結しました。\n\n売上への反映時期は未定です。", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def document(self, first_seen=None):
        return import_document(self.root, self.source, "TSE:0003", "event-watch", "1",
                               self.fixture["target_session"] + "T16:00:00+09:00",
                               first_seen or self.fixture["target_session"] + "T16:02:00+09:00")

    def test_shadow_cache_evidence_and_baseline_isolation(self):
        document = self.document()
        self.assertEqual(document["data_grade"], "synthetic")
        candidate = self.root / "outputs" / self.manifest["run_id"] / "candidates.csv"
        before = digest(candidate.read_bytes())
        provider = MockSemanticProvider(response())
        first = shadow(self.root, self.manifest["run_id"], document["document_id"], provider, QUESTIONS)
        self.assertEqual(first["semantic_status"], "missing_evidence")
        self.assertEqual(first["baseline_stage"], "WATCH")
        second = shadow(self.root, self.manifest["run_id"], document["document_id"], provider, QUESTIONS, ["p001"])
        self.assertEqual(second["semantic_status"], "ok")
        self.assertEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(before, digest(candidate.read_bytes()))

    def test_cutoff_invalid_and_unreadable(self):
        document = self.document(self.fixture["next_session"] + "T08:00:00+09:00")
        result = shadow(self.root, self.manifest["run_id"], document["document_id"], MockSemanticProvider(response()), QUESTIONS)
        self.assertEqual(result["reason"], "after_as_of")
        self.source.write_bytes(b"\xff\xfe")
        bad = self.document()
        self.assertEqual(bad["status"], "needs_review")
        self.assertEqual(shadow(self.root, self.manifest["run_id"], bad["document_id"], MockSemanticProvider(response()), QUESTIONS)["semantic_status"], "needs_review")
        broken = response()
        broken["answers"]["dilution"]["noul"] = 2.0
        with self.assertRaisesRegex(ValueError, "invalid noul"):
            validate_response(broken, read_json(QUESTIONS)["questions"], "mock-materials-v1")

    def test_openjev_adapter_loopback_and_wire_contract(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            OpenJevProvider("https://api.example.com", "openjev-0.1", "rev", "commit", "nvfp4", {"runtime_config_version": "test-v1"})
        with self.assertRaisesRegex(ValueError, "runtime config"):
            OpenJevProvider("http://127.0.0.1:8080", "openjev-0.1", "rev", "commit", "nvfp4", {})
        provider = OpenJevProvider("http://127.0.0.1:8080", "openjev-0.1", "rev", "commit", "nvfp4", {"runtime_config_version": "test-v1"})
        calls = []
        class FakeResponse:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, *_):
                return self.data
        def fake_urlopen(request, timeout):
            calls.append(request)
            return FakeResponse(b'{"models":[{"name":"openjev-0.1"}]}' if request.get_method() == "GET" else b'{"model":"openjev-0.1","answers":{}}')
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(provider.ask("text", {})["model"], "openjev-0.1")
        self.assertEqual([c.get_method() for c in calls], ["GET", "POST"])
        self.assertIn(b'"model":"openjev-0.1"', calls[1].data)

    def test_runtime_setting_changes_semantic_cache_identity(self):
        document = self.document()
        run_id = self.manifest["run_id"]
        answers = response()
        answers["model"] = "openjev-0.1"
        first = OpenJevProvider("http://127.0.0.1:8080", "openjev-0.1", "rev", "commit", "nvfp4",
                                {"runtime_config_version": "test-v1", "moe_backend": "marlin"})
        second = OpenJevProvider("http://127.0.0.1:8080", "openjev-0.1", "rev", "commit", "nvfp4",
                                 {"runtime_config_version": "test-v2", "moe_backend": "auto"})
        with patch.object(OpenJevProvider, "ask", return_value=answers):
            one = shadow(self.root, run_id, document["document_id"], first, QUESTIONS, ["p001"])
            two = shadow(self.root, run_id, document["document_id"], second, QUESTIONS, ["p001"])
        self.assertEqual(one["semantic_status"], "ok")
        self.assertEqual(two["semantic_status"], "ok")
        self.assertNotEqual(one["cache_key"], two["cache_key"])

    def test_score_distribution_is_validated(self):
        question = {"specificity": {"type": "score", "criteria": ["検討", "合意", "正式契約"]}}
        answer = {"model": "openjev-0.1", "answers": {"specificity": {
            "score": 1.8, "legend": {"0": "検討", "1": "合意", "2": "正式契約"},
            "probabilities": {"0": 0.0, "1": 0.2, "2": 0.8}, "confidence": 0.5}}}
        self.assertEqual(validate_response(answer, question, "openjev-0.1")["specificity"]["score"], 1.8)
        answer["answers"]["specificity"]["score"] = 0.1
        with self.assertRaisesRegex(ValueError, "invalid score"):
            validate_response(answer, question, "openjev-0.1")

    def test_manual_document_enters_next_sealed_run(self):
        document = self.document()
        write_json(self.root / "demo-input" / "disclosures.json", [])
        later = run(self.root, self.root / "demo-input", CONFIG, self.fixture["target_session"], self.fixture["as_of"])
        snapshot = read_json(self.root / "data" / "snapshots" / (later["snapshot_id"] + ".json"))
        self.assertEqual(snapshot["usable_disclosures"][0]["content_hash"], document["content_hash"])
        self.assertEqual(next(d for d in read_json(self.root / "outputs" / later["run_id"] / "decisions.json")
                              if d["instrument_id"] == "TSE:0003")["stage"], "WATCH")
        self.assertEqual(shadow(self.root, later["run_id"], document["document_id"],
                                MockSemanticProvider(response()), QUESTIONS, ["p001"])["semantic_status"], "ok")

    def test_long_text_is_not_silently_truncated(self):
        self.source.write_text("契約の資料。" * 3000, encoding="utf-8")
        document = self.document()
        result = shadow(self.root, self.manifest["run_id"], document["document_id"],
                        MockSemanticProvider(response()), QUESTIONS)
        self.assertEqual(result["reason"], "state_exceeds_limit")

    def test_malformed_response_is_not_negative_label(self):
        document = self.document()
        broken = response()
        broken["answers"].pop("dilution")
        result = shadow(self.root, self.manifest["run_id"], document["document_id"],
                        MockSemanticProvider(broken), QUESTIONS)
        self.assertEqual(result["semantic_status"], "invalid_response")
        self.assertIsNone(result["answers"])


if __name__ == "__main__":
    unittest.main()
