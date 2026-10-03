"""Evaluate a reconstructed preceding session; never a prospective performance test."""
from pathlib import Path

from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.pipeline import evaluate_run, run
from alpha_loop.provider import FileProvider
from alpha_loop.retrospective import _verify
from alpha_loop.service import _future


root = Path(__file__).resolve().parents[1]
latest = read_json(root / "data" / "operations" / "latest_service.json")
_, study, saved = _verify(root, latest["study_id"])
if saved["prior"]["data_grade"] != "reconstructed":
    raise ValueError("this check requires reconstructed market data")
prior = saved["prior"]
directory = root / "data" / "operations" / "research_bootstrap" / latest["study_id"]
for name, value in prior["data"].items():
    write_json(directory / (name + ".json"), value)
write_json(directory / "disclosures.json", prior["usable_disclosures"])
manifest = run(root, directory, root / "configs" / "baseline.json", prior["target_session"], now_iso())
current, _ = FileProvider(Path(latest["input_dir"])).load()
forward = _future(root, manifest["run_id"], current, latest["session"], directory / "evaluation_input")
evaluated = evaluate_run(root, manifest["run_id"], forward, latest["session"])
outcome_path = Path(evaluated["path"]) / "outcomes.csv"
result = {"research_only": True, "prediction_score_eligible": False, "data_grade": "reconstructed",
          "feature_session": prior["target_session"], "outcome_session": latest["session"],
          "run_id": manifest["run_id"], "outcome_csv": str(outcome_path), "outcome_hash": digest(outcome_path.read_bytes()),
          "metrics": evaluated["metrics"], "completed_at": now_iso()}
write_json(root / "data" / "operations" / "hermes_research_evaluation.json", result)
print(outcome_path)
