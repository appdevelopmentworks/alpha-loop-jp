"""Offline acceptance for remaining material/research operations (uv Python -S)."""
from pathlib import Path
import json
import shutil
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.material_fixture import FixtureMaterialProvider, create_material_fixture, fixture_response
from alpha_loop.materials import batch
from alpha_loop.material_experiments import preview
from alpha_loop.material_quality import register_labels, evaluate_quality
from alpha_loop.pipeline import evaluate_run, replay
from alpha_loop.service import operate
from alpha_loop.research import register, compare
from alpha_loop.research_fixture import create_research_fixture
from alpha_loop.research_workflow import record_review, weekly


def main():
    root = PROJECT / "data/remaining_demo" / uuid.uuid4().hex[:12]
    f = create(root / "input", without_turnover=True)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    for name in ("baseline.json", "questions_v1.json"):
        shutil.copyfile(PROJECT / "configs" / name, root / "configs" / name)
    config = read_json(PROJECT / "configs/hermes_demo.json")
    config.update(input_dir="input", materials_config="configs/material.json", weekly_research_enabled=True)
    write_json(root / "configs/operation.json", config)
    fixture = create_material_fixture(root, f["target_session"], root / "configs/questions_v1.json")
    write_json(root / "configs/mock.json", fixture_response())
    write_json(root / "configs/material.json", {"schema_version": 1, "mode": "shadow", "provider": "mock", "purpose": "synthetic_test",
               "input_manifest": "disclosure-input/manifest.json", "question_set": "configs/questions_v1.json", "mock_response": "configs/mock.json",
               "max_documents": 100, "budget_seconds": 30, "cloud_enabled": False, "orders_enabled": False})
    result = operate(root, root / "configs/operation.json")
    before = digest(Path(result["candidate_csv"]).read_bytes())
    repeated = operate(root, root / "configs/operation.json")
    if repeated["first_recorded"] or result["run_id"] != repeated["run_id"] or result["side_failures"]:
        raise AssertionError("daily reuse/side output failed")
    materials = batch(root, result["run_id"], FixtureMaterialProvider(), root / "configs/questions_v1.json", max_documents=100)
    quality = evaluate_quality(root, register_labels(root, Path(fixture["labels"]), root / "configs/questions_v1.json")["gold_id"], Path(materials["documents_path"]))
    if quality["status"] != "SYNTHETIC_ONLY" or quality["metrics"]["dilution"]["accuracy_all_known"] != 1:
        raise AssertionError("fixture quality/grade gate failed")
    selectors = preview(root, result["run_id"], Path(materials["documents_path"]))
    evaluation = evaluate_run(root, result["run_id"], root / "input", f["next_session"])
    replayed = replay(root, result["run_id"])
    if digest(Path(result["candidate_csv"]).read_bytes()) != before:
        raise AssertionError("candidate CSV changed")
    research_root = root / "m5"
    create_research_fixture(research_root, root / "configs/baseline.json")
    experiment = register(research_root, research_root / "plan.json")["experiment_id"]
    comparison = compare(research_root, experiment, research_root / "batch.json")
    review = record_review(research_root, experiment, "HOLD", "synthetic acceptance fixture", "実市場の採用判断ではない", True)
    receipt = {"verified_at": now_iso(), "root": str(root), "data_grade": "synthetic", "site_packages_disabled": sys.flags.no_site == 1,
               "api_required": False, "gpu_required": False, "candidate_csv": result["candidate_csv"],
               "outcomes_csv": str(Path(evaluation["path"]) / "outcomes.csv"), "materials": materials, "quality": quality,
               "material_previews": selectors, "comparison": comparison, "human_review_fixture": review,
               "duplicate_reused": True, "price_csv_unchanged": True, "replay": replayed,
               "weekly_preview": weekly(root, f["target_session"], force_draft=True),
               "market_performance_validated": False}
    shutil.copytree(PROJECT / "src/alpha_loop", root / "src/alpha_loop", ignore=shutil.ignore_patterns("__pycache__"))
    write_json(PROJECT / "data/operations/remaining_acceptance.json", receipt)
    print(json.dumps({k: receipt[k] for k in ("root", "candidate_csv", "outcomes_csv", "site_packages_disabled", "duplicate_reused", "price_csv_unchanged")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
