"""Verify installed Hermes boundary, existing real outputs and frozen experiments."""
from pathlib import Path
import json
import re
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from alpha_loop.common import canonical, digest, now_iso, read_json, write_json
from alpha_loop.pipeline import _code_hash, _verified_manifest, config_load
from alpha_loop.research import _load, engine_hash
from alpha_loop.runtime import status


def main():
    native = Path.home() / "AppData/Local/hermes"
    latest = read_json(PROJECT / "data/operations/latest_service.json")
    real = _verified_manifest(PROJECT / "outputs" / latest["run_id"])
    registered = [_load(PROJECT, p.stem) for p in (PROJECT / "data/research/experiments").glob("*.json")]
    wrapper = read_json(PROJECT / "data/operations/remaining_wrapper_stdout.json")
    job = next(j for j in read_json(native / "cron/jobs.json")["jobs"] if j["id"] == "8a4841ec482c")
    log_path = PROJECT / "data/operations/remaining_tests_stderr.log"
    tests = re.search(r"Ran (\d+) tests in ([0-9.]+)s\s+OK", log_path.read_text(encoding="utf-8-sig"))
    operation = read_json(PROJECT / "configs/hermes_operations.json")
    checks = {
        "wrapper_matches": digest((PROJECT / "integrations/hermes/alpha_loop_daily.py").read_bytes()) == digest((native / "scripts/alpha_loop_daily.py").read_bytes()),
        "skill_matches": digest((PROJECT / "integrations/hermes/alpha-loop-jp/SKILL.md").read_bytes()) == digest((native / "skills/alpha-loop-jp/SKILL.md").read_bytes()),
        "installed_wrapper_synthetic_succeeded": wrapper["status"] == "SUCCEEDED" and wrapper["result"]["materials"]["shadow_only"] and not wrapper["result"]["side_failures"],
        "price_code_unchanged": _code_hash() == real["code_hash"] and all(e["numerical_code_hash"] == _code_hash() for e in registered),
        "price_config_unchanged": real["config_hash"] == digest(canonical(config_load(PROJECT / "configs/baseline.json"))),
        "frozen_m5_engine_matches": all(e["engine_hash"] == engine_hash() for e in registered),
        "real_service_artifacts_unchanged": all(digest(Path(p).read_bytes()) == h for p, h in latest["service_artifacts"].items()),
        "existing_daily_schedule_unchanged": job["enabled"] and job["no_agent"] and job["schedule"]["expr"] == "0 20 * * 1-5" and job["script"] == "alpha_loop_daily.py",
        "local_only_enabled": operation["cloud_enabled"] is False and operation["orders_enabled"] is False and operation["weekly_research_enabled"] is True,
        "tests_passed": bool(tests and int(tests[1]) >= 138),
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    current = status(PROJECT)
    receipt = {"verified_at": now_iso(), "checks": checks, "tests": int(tests[1]), "test_seconds": float(tests[2]),
               "test_log_hash": digest(log_path.read_bytes()), "real_run_id": latest["run_id"], "real_candidate_count": latest["candidate_count"],
               "numerical_code_hash": _code_hash(), "m5_engine_hash": engine_hash(), "registered_experiments": [e["experiment_id"] for e in registered],
               "next_run_at": job["next_run_at"], "job_id": job["id"], "running": current["running"],
               "owner_unknown": current["owner_unknown"], "new_material_weekly_scheduled_run_verified": False,
               "material_quality_or_market_performance_validated": False}
    write_json(PROJECT / "data/operations/remaining_deployment.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
