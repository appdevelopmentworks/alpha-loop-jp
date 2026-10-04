from __future__ import annotations

import copy
import tempfile
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, read_json, write_json
from alpha_loop.dashboard import build, enforce_publication, export_site, summarize
from alpha_loop.dashboard_publish import publish
from alpha_loop.fixture import create
from alpha_loop.pipeline import evaluate_run, run

PROJECT = Path(__file__).resolve().parents[1]
POLICY = read_json(PROJECT / "configs/dashboard_publication.example.json")


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.fixture = create(self.input, without_turnover=True)
        calendar = read_json(self.input / "calendar.json")
        write_json(self.input / "calendar.json", calendar + [self.fixture["next_session"]])
        with patch("alpha_loop.pipeline.now_iso", return_value=self.fixture["as_of"]):
            self.run = run(self.root, self.input, PROJECT / "configs/baseline.json", self.fixture["target_session"], self.fixture["as_of"])
        self.ids = [self.run["run_id"]]

    def tearDown(self):
        self.temp.cleanup()

    def evaluate(self):
        return evaluate_run(self.root, self.run["run_id"], self.input, self.fixture["next_session"])

    def test_selected_candidates_only_not_whole_universe(self):
        evaluation = self.evaluate()
        document = build(self.root, run_ids=self.ids)
        day = document["days"][0]
        self.assertEqual(day["stats"], {"candidates":3,"evaluated":3,"hits":2,"misses":1,"unknown":0,"hit_rate":2/3})
        self.assertEqual(len(read_json(Path(evaluation["path"]) / "outcomes.json")),6)
        self.assertEqual(day["next_session"],self.fixture["next_session"])
        self.assertFalse(day["prediction_eligible"])
        self.assertEqual(day["candidates"][1]["execution_status"],"limit_up")
        self.assertNotIn("net_return", day)

    def test_pending_is_not_a_miss_and_zero_denominator_is_null(self):
        day = build(self.root, run_ids=self.ids)["days"][0]
        self.assertEqual(day["stats"]["unknown"],3)
        self.assertEqual(day["stats"]["misses"],0)
        self.assertIsNone(day["stats"]["hit_rate"])
        self.assertFalse(day["result_saved"])
        self.assertIsNone(summarize([])["hit_rate"])

    def test_missing_selected_price_is_unknown(self):
        future = read_json(self.input / "future.json")
        future["bars"] = [b for b in future["bars"] if b["instrument_id"] != "TSE:0001"]
        write_json(self.input / "future.json", future)
        self.evaluate()
        stat = build(self.root, run_ids=self.ids)["days"][0]["stats"]
        self.assertEqual((stat["hits"],stat["evaluated"],stat["unknown"]),(1,2,1))
        self.assertEqual(stat["hit_rate"],.5)

    def test_same_run_and_duplicate_receipts_count_once(self):
        for name in ("one","two"):
            write_json(self.root / f"data/operations/service_jobs/{name}.json",
                       {"run_id":self.ids[0],"status":"SUCCEEDED", "candidate_csv":"C:/secret/private.csv"})
        self.assertEqual(len(build(self.root)["days"]),1)
        self.assertEqual(len(build(self.root, run_ids=self.ids*2)["days"]),1)

    def test_native_export_does_not_scan_standalone_research_runs(self):
        self.assertEqual(build(self.root)["days"],[])

    def test_tampered_evaluation_preserves_previous_export(self):
        evaluation = self.evaluate()
        document = build(self.root, run_ids=self.ids)
        site = self.root / "site"
        export_site(document,site,PROJECT / "dashboard")
        before = (site / "data/dashboard.json").read_bytes()
        (Path(evaluation["path"]) / "outcomes.json").write_text("[]",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"hash mismatch"):
            build(self.root, run_ids=self.ids)
        self.assertEqual((site / "data/dashboard.json").read_bytes(),before)

    def test_real_data_publication_requires_grade_and_rights(self):
        doc = build(self.root, run_ids=self.ids)
        doc["days"][0]["data_grade"]="reconstructed"
        with self.assertRaisesRegex(ValueError,"grade"):
            enforce_publication(doc,POLICY)
        enabled = {**POLICY,"allowed_data_grades":["reconstructed"]}
        with self.assertRaisesRegex(ValueError,"rights"):
            enforce_publication(doc,enabled)
        enforce_publication(doc,{**enabled,"rights_confirmed":True,"rights_note":"Test permission"})

    def test_private_field_is_rejected_at_publication_boundary(self):
        doc = build(self.root, run_ids=self.ids)
        for where in (doc,doc["days"][0],doc["days"][0]["candidates"][0]):
            where["raw_ref"]="C:/private/secret"
            with self.assertRaisesRegex(ValueError,"fields"):
                enforce_publication(doc,POLICY)
            del where["raw_ref"]

    def test_public_export_contains_no_originals_or_machine_paths(self):
        doc = build(self.root, run_ids=self.ids)
        export_site(doc,self.root / "site",PROJECT / "dashboard")
        raw = (self.root / "site/data/dashboard.json").read_text(encoding="utf-8")
        for forbidden in (str(self.root),"raw_objects","snapshot_id","input_dir","qwen","future_ref"):
            self.assertNotIn(forbidden,raw)

    def test_aggregate_uses_pooled_denominator(self):
        rows = [{"hit":True}] + [{"hit":False} for _ in range(9)] + [{"hit":True}]
        self.assertEqual(summarize(rows)["hit_rate"],2/11)

    def test_publish_dry_run_has_no_subprocess_or_remote_contact(self):
        doc = build(self.root, run_ids=self.ids)
        site=self.root / "site"
        export_site(doc,site,PROJECT / "dashboard")
        with patch("alpha_loop.dashboard_publish.subprocess.run",side_effect=AssertionError("network")):
            result=publish(site,self.root,POLICY)
        self.assertEqual(result["status"],"DRY_RUN")
        self.assertFalse(result["network_used"])

    def test_publisher_rejects_extra_files_and_different_branch(self):
        site=self.root / "site"
        export_site(build(self.root, run_ids=self.ids),site,PROJECT / "dashboard")
        with self.assertRaisesRegex(ValueError,"gh-pages"):
            publish(site,self.root,{**POLICY,"branch":"main"})
        (site / "private.json").write_text("{}",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"unexpected files"):
            publish(site,self.root,POLICY)

    def test_export_is_idempotent(self):
        self.evaluate()
        self.assertEqual(build(self.root,run_ids=self.ids),build(self.root,run_ids=self.ids))

    def test_duplicate_or_foreign_outcome_is_rejected_even_when_rehashed(self):
        evaluation=self.evaluate();directory=Path(evaluation["path"])
        rows=read_json(directory / "outcomes.json");rows.append(copy.deepcopy(rows[0]))
        write_json(directory / "outcomes.json",rows)
        manifest=read_json(directory / "evaluation_manifest.json")
        manifest["artifacts"]["outcomes.json"]=digest((directory / "outcomes.json").read_bytes())
        write_json(directory / "evaluation_manifest.json",manifest)
        with self.assertRaisesRegex(ValueError,"duplicate"):
            build(self.root,run_ids=self.ids)

    def test_real_git_publish_to_isolated_bare_repo_and_idempotent_retry(self):
        bare=self.root / "remote.git"
        empty=self.root / "empty_git_config";empty.write_bytes(b"")
        env={"GIT_CONFIG_GLOBAL":str(empty),"GIT_CONFIG_NOSYSTEM":"1"}
        with patch.dict(os.environ,env):
            subprocess.run(["git","init","--bare",str(bare)],capture_output=True,check=True)
            site=self.root / "site"
            export_site(build(self.root,run_ids=self.ids),site,PROJECT / "dashboard")
            policy={**POLICY,"remote_url":str(bare)}
            with patch("alpha_loop.dashboard_publish.REMOTE",str(bare)):
                first=publish(site,self.root,policy,push=True)
                second=publish(site,self.root,policy,push=True)
            self.assertEqual(first["status"],"PUSHED")
            self.assertEqual(second["status"],"UNCHANGED")
            files=subprocess.run(["git","--git-dir="+str(bare),"ls-tree","-r","--name-only","gh-pages"],
                                 capture_output=True,check=True,text=True).stdout.splitlines()
            self.assertEqual(set(files),{"index.html","style.css","app.js",".nojekyll","data/dashboard.json"})
            self.assertFalse((self.root / ".git").exists())

    def test_conflicting_saved_evaluations_are_not_cherry_picked(self):
        first=self.evaluate()
        future=read_json(self.input / "future.json")
        future["bars"][0]["adj_high"]=104
        write_json(self.input / "future.json",future)
        second=self.evaluate()
        self.assertNotEqual(first["evaluation_id"],second["evaluation_id"])
        with self.assertRaisesRegex(ValueError,"conflicting"):
            build(self.root,run_ids=self.ids)
