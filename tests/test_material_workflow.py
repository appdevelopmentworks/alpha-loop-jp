import copy
import csv
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import canonical, digest, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.material_fixture import FixtureMaterialProvider, create_material_fixture, fixture_response
from alpha_loop.materials import batch, infer, ingest, seal, verified_document
from alpha_loop.material_operations import MockMaterialProvider, analyze_pending, import_pending
from alpha_loop.material_quality import evaluate_quality, register_labels
from alpha_loop.material_experiments import preview
from alpha_loop.model_session import local_model
from alpha_loop.pipeline import run
from alpha_loop.service import operate

PROJECT = Path(__file__).resolve().parents[1]
QUESTIONS = PROJECT / "configs/questions_v1.json"
BASELINE = PROJECT / "configs/baseline.json"


class MaterialWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.f = create(self.root / "input", without_turnover=True)
        self.price = run(self.root, self.root / "input", BASELINE, self.f["target_session"], self.f["as_of"])
        self.material = create_material_fixture(self.root, self.f["target_session"], QUESTIONS)
        self.manifest = Path(self.material["manifest"])
        self.provider = FixtureMaterialProvider()

    def tearDown(self):
        self.temp.cleanup()

    def batch(self, **kwargs):
        return batch(self.root, self.price["run_id"], self.provider, QUESTIONS, max_documents=100, **kwargs)

    def doc(self, index=0):
        return verified_document(self.root, self.root / "data/materials/documents" / (self.material["document_ids"][index] + ".json"))

    def test_original_dedup_and_first_seen_unchanged(self):
        before = self.doc()
        repeated = ingest(self.root, self.manifest)
        self.assertEqual(repeated["created"], 0)
        self.assertEqual(before, self.doc())
        self.assertEqual(digest((self.root / before["raw_ref"]).read_bytes()), before["content_hash"])

    def test_changed_revision_rejected_and_new_revision_supersedes(self):
        spec = read_json(self.manifest)
        spec["documents"] = spec["documents"][:1]
        Path(self.manifest.parent / spec["documents"][0]["file"]).write_text("訂正情報", encoding="utf-8")
        write_json(self.manifest, spec)
        with self.assertRaisesRegex(ValueError, "new revision"):
            ingest(self.root, self.manifest)
        spec["documents"][0].update(revision_id="2", published_at=self.f["target_session"] + "T17:00:00+09:00", synthetic_first_seen_at=self.f["target_session"] + "T17:01:00+09:00")
        write_json(self.manifest, spec)
        new = ingest(self.root, self.manifest)["document_ids"][0]
        sealed = seal(self.root, self.price["run_id"])
        self.assertIn(new, {d["document_id"] for d in sealed["documents"]})
        self.assertIn({"document_id": self.doc()["document_id"], "reason": "superseded_revision"}, sealed["excluded"])

    def test_permissions_issuer_path_and_duplicate_rejected_before_publish(self):
        original = read_json(self.manifest)
        for update in ({"permission_confirmed": False}, {"issuer_code": "9999"}, {"file": "../outside.txt"}):
            value = copy.deepcopy(original)
            value["documents"][0].update(update)
            write_json(self.manifest, value)
            with self.assertRaises(ValueError):
                ingest(self.root, self.manifest)
        original["documents"].append(original["documents"][0])
        write_json(self.manifest, original)
        with self.assertRaisesRegex(ValueError, "duplicate revision"):
            ingest(self.root, self.manifest)

    def test_late_publication_and_utc_session(self):
        value = read_json(self.manifest)
        value["documents"] = value["documents"][:1]
        row = value["documents"][0]
        row.update(disclosure_id="late", published_at=self.f["target_session"] + "T20:01:00+09:00", synthetic_first_seen_at=self.f["target_session"] + "T20:02:00+09:00")
        write_json(self.manifest, value)
        doc_id = ingest(self.root, self.manifest)["document_ids"][0]
        self.assertIn({"document_id": doc_id, "reason": "after_as_of"}, seal(self.root, self.price["run_id"])["excluded"])
        row.update(disclosure_id="utc", published_at=self.f["target_session"] + "T07:00:00+00:00", synthetic_first_seen_at=self.f["target_session"] + "T16:01:00+09:00")
        write_json(self.manifest, value)
        doc_id = ingest(self.root, self.manifest)["document_ids"][0]
        self.assertIn(doc_id, {d["document_id"] for d in seal(self.root, self.price["run_id"])["documents"]})

    def test_observed_first_seen_cannot_be_backdated(self):
        value = read_json(self.manifest)
        value.update(data_grade="observed")
        value["documents"] = value["documents"][:1]
        value["documents"][0]["available_at"] = "2000-01-01T00:00:00+09:00"
        write_json(self.manifest, value)
        with patch("alpha_loop.materials.now_iso", return_value="2026-10-01T22:00:00+09:00"):
            did = ingest(self.root, self.manifest)["document_ids"][0]
        doc = verified_document(self.root, self.root / "data/materials/documents" / (did + ".json"))
        self.assertEqual(doc["first_seen_at"], "2026-10-01T22:00:00+09:00")
        self.assertEqual(doc["asserted_available_at"], "2000-01-01T00:00:00+09:00")
        self.assertIn({"document_id": did, "reason": "synthetic_grade_mismatch"}, seal(self.root, self.price["run_id"])["excluded"])

    def test_raw_tamper_rejected(self):
        doc = self.doc()
        (self.root / doc["raw_ref"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "raw hash"):
            seal(self.root, self.price["run_id"])

    def test_observed_material_can_join_reconstructed_price_without_upgrading_price(self):
        observed = self.f["target_session"] + "T18:00:00+09:00"
        value = read_json(self.manifest)
        value.update(data_grade="observed")
        value["documents"] = value["documents"][:1]
        write_json(self.manifest, value)
        with patch("alpha_loop.materials.now_iso", return_value=observed):
            did = ingest(self.root, self.manifest)["document_ids"][0]
        source = read_json(self.root / "input/source.json")
        source["data_grade"] = "reconstructed"
        write_json(self.root / "input/source.json", source)
        price = run(self.root, self.root / "input", BASELINE, self.f["target_session"], self.f["as_of"])
        sealed = seal(self.root, price["run_id"])
        self.assertIn(did, {d["document_id"] for d in sealed["documents"]})
        self.assertEqual(sealed["price_data_grade"], "reconstructed")
        self.assertFalse(sealed["prediction_eligible"])

    def test_response_cache_evidence_and_unknown_not_negative(self):
        with patch.object(self.provider, "ask", wraps=self.provider.ask) as ask:
            first = infer(self.root, self.doc(), self.provider, QUESTIONS)
            repeated = infer(self.root, self.doc(), self.provider, QUESTIONS)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(first, repeated)
        self.assertEqual(first["evidence"]["core_business"]["quote"], self.doc()["paragraphs"][0]["text"])
        ambiguous = infer(self.root, self.doc(2), self.provider, QUESTIONS)
        self.assertEqual(ambiguous["semantic_status"], "missing_evidence")
        self.assertTrue(all(v is None for v in ambiguous["answers"].values()))

    def test_cache_tamper_and_invalid_confidence_rejected(self):
        first = infer(self.root, self.doc(), self.provider, QUESTIONS)
        path = self.root / "data/materials/cache" / (first["cache_id"] + ".json")
        cache = read_json(path)
        cache["answers"]["core_business"]["probability_yes"] = 0
        write_json(path, cache)
        with self.assertRaisesRegex(RuntimeError, "cache hash"):
            infer(self.root, self.doc(), self.provider, QUESTIONS)
        value = fixture_response(False)
        value["response"]["answers"]["material_type"]["confidence"] = True
        result = infer(self.root, self.doc(1), MockMaterialProvider(value), QUESTIONS)
        self.assertEqual(result["semantic_status"], "invalid_response")

    def test_missing_long_and_injection_text_never_executes(self):
        self.assertEqual(infer(self.root, self.doc(6), self.provider, QUESTIONS)["semantic_status"], "needs_review")
        self.assertEqual(infer(self.root, self.doc(), self.provider, QUESTIONS, max_chars=10)["semantic_status"], "needs_review")
        result = infer(self.root, self.doc(7), self.provider, QUESTIONS)
        self.assertEqual(result["semantic_status"], "ok")
        self.assertFalse(result["evidence"]["material_type"]["semantic_entailment_verified"])

    def test_batch_cap_immutable_retry_and_price_unchanged(self):
        before = digest((self.root / "outputs" / self.price["run_id"] / "candidates.csv").read_bytes())
        first = batch(self.root, self.price["run_id"], self.provider, QUESTIONS, max_documents=1)
        again = batch(self.root, self.price["run_id"], self.provider, QUESTIONS, max_documents=1)
        self.assertEqual(first, again)
        retry = batch(self.root, self.price["run_id"], self.provider, QUESTIONS, max_documents=1, retry_unavailable=True)
        self.assertNotEqual(first["documents_path"], retry["documents_path"])
        self.assertEqual(read_json(Path(first["documents_path"]).parent / "snapshot.json"), read_json(Path(retry["documents_path"]).parent / "snapshot.json"))
        self.assertEqual(before, digest((self.root / "outputs" / self.price["run_id"] / "candidates.csv").read_bytes()))

    def test_no_source_does_not_start_gpu(self):
        settings = read_json(PROJECT / "configs/materials_operations.json")
        settings.update(question_set=str(QUESTIONS), runtime_config=str(PROJECT / "configs/openjev_runtime_rtx5090_v1.json"))
        write_json(self.root / "material.json", settings)
        shutil.rmtree(self.root / "data/materials/documents")
        with patch("alpha_loop.material_operations.local_model", side_effect=AssertionError("must not start GPU")):
            self.assertEqual(import_pending(self.root, "material.json")["status"], "UNAVAILABLE_SOURCE")
            result = analyze_pending(self.root, self.price["run_id"], "material.json")
        self.assertEqual(result["status"], "NO_AVAILABLE_DOCUMENTS")
        self.assertEqual(result["unknown_instruments"], 6)

    def test_labels_confusion_unknown_and_synthetic_gate(self):
        b = self.batch()
        gold = register_labels(self.root, Path(self.material["labels"]), QUESTIONS)
        report = evaluate_quality(self.root, gold["gold_id"], Path(b["documents_path"]))
        self.assertEqual(report["status"], "SYNTHETIC_ONLY")
        self.assertEqual(report["metrics"]["dilution"]["unknown_truth"], 2)
        self.assertEqual(report["metrics"]["dilution"]["accuracy_all_known"], 1)
        self.assertEqual(report["metrics"]["dilution"]["brier_mean"], 0)
        self.assertEqual(report, evaluate_quality(self.root, gold["gold_id"], Path(b["documents_path"])))
        Path(report["report_path"]).write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "report artifact"):
            evaluate_quality(self.root, gold["gold_id"], Path(b["documents_path"]))

    def test_ai_labels_invalid_evidence_and_grade_rejected(self):
        original = read_json(Path(self.material["labels"]))
        for change in ("ai", "human"):
            write_json(self.root / "bad-labels.json", {**original, "label_origin": change})
            with self.assertRaises(ValueError):
                register_labels(self.root, self.root / "bad-labels.json", QUESTIONS)
        original["labels"][0]["evidence_ids"]["dilution"] = ["p999"]
        write_json(self.root / "bad-labels.json", original)
        with self.assertRaisesRegex(ValueError, "evidence"):
            register_labels(self.root, self.root / "bad-labels.json", QUESTIONS)

    def test_b1_b2_previews_are_non_active_and_do_not_refill_b1(self):
        b = self.batch()
        result = preview(self.root, self.price["run_id"], Path(b["documents_path"]))
        self.assertFalse(result["active"])
        self.assertLessEqual(result["B1_count"], result["A_count"])
        self.assertFalse(result["score_is_surge_probability"])

    def test_model_restores_after_inference_exception_and_journals(self):
        with patch("alpha_loop.models.model_status", return_value={"exclusive": True, "containers": {"qwen": "running", "openjev": "exited"}}), patch("alpha_loop.models.switch_model") as switch, patch("alpha_loop.models._docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "inference error"):
                with local_model(self.root, "openjev"):
                    raise RuntimeError("inference error")
        self.assertEqual([c.args[1] for c in switch.call_args_list], ["openjev", "qwen"])
        self.assertEqual(docker.call_args.args[0], "stop")
        lease = read_json(next((self.root / "data/operations/gpu_leases").glob("*.json")))
        self.assertEqual(lease["status"], "RESTORED")

    def test_daily_material_error_preserves_candidates_and_repeated_job(self):
        config = read_json(PROJECT / "configs/hermes_demo.json")
        config.update(input_dir="input", strategy_config=str(BASELINE), materials_config="missing.json", weekly_research_enabled=True)
        write_json(self.root / "operation.json", config)
        first = operate(self.root, self.root / "operation.json", no_ai=True)
        again = operate(self.root, self.root / "operation.json", no_ai=True)
        self.assertEqual(first["status"], "SUCCEEDED_WITH_SIDE_FAILURE")
        self.assertIn("materials", first["side_failures"])
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertFalse(again["first_recorded"])
        self.assertEqual(first["candidate_count"], 3)
