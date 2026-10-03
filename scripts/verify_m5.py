"""Offline, no site-packages/GPU acceptance run for the M5 report pipeline."""
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.research import compare, engine_hash, prepare_batch, register, refresh
from alpha_loop.research_fixture import create_research_fixture


def main():
    root = PROJECT / "data" / "m5_demo" / engine_hash()[:20]
    fixture = create_research_fixture(root, PROJECT / "configs" / "baseline.json")
    baseline_before = digest((root / "baseline.json").read_bytes())
    registration = register(root, root / "plan.json")
    result = compare(root, registration["experiment_id"], root / "batch.json")
    repeated = compare(root, registration["experiment_id"], prepare_batch(root, registration["experiment_id"]))
    if result != repeated or result["recommendation"] != "HOLD" or result["automatic_adoption"]:
        raise AssertionError("synthetic report/replay/release gate failed")
    if digest((root / "baseline.json").read_bytes()) != baseline_before:
        raise AssertionError("baseline config changed")
    metrics = read_json(Path(result["metrics_path"]))
    receipt = {"verified_at": now_iso(), "engine_hash": engine_hash(), "data_grade": "synthetic",
               "site_packages_disabled": sys.flags.no_site == 1, "api_key_required": False, "gpu_required": False,
               "fixture": fixture, "result": result, "A": metrics["overall"]["A"], "B": metrics["overall"]["B"],
               "replay_verified": True, "baseline_unchanged": True, "daily_refresh": refresh(root),
               "unverified": ["real unused holdout performance", "real disclosure accuracy", "automatic production adoption (not implemented)"]}
    write_json(PROJECT / "data" / "operations" / "m5_acceptance.json", receipt)
    print(__import__("json").dumps({"root": str(root), "report": result["report_path"], "recommendation": result["recommendation"],
                                 "statistical_decision": result["statistical_decision"], "replay_verified": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
