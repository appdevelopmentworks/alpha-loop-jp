"""Read-only deployment verification; no market/model/scheduler actions."""
from pathlib import Path
import json
import re
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from alpha_loop.common import canonical, digest, now_iso, read_json, write_json
from alpha_loop.pipeline import _code_hash, config_load
from alpha_loop.research import engine_hash
from alpha_loop.runtime import status


def main():
    native = Path.home() / "AppData/Local/hermes"
    source = PROJECT / "integrations/hermes/alpha_loop_daily.py"
    installed = native / "scripts/alpha_loop_daily.py"
    latest = read_json(PROJECT / "data/operations/latest_service.json")
    manifest = read_json(PROJECT / "outputs" / latest["run_id"] / "run_manifest.json")
    experiments = [read_json(p) for p in (PROJECT / "data/research/experiments").glob("*.json")]
    first = read_json(PROJECT / "data/operations/m4_wrapper_stdout.json")
    repeated = read_json(PROJECT / "data/operations/m4_wrapper_repeat_stdout.json")
    log = (PROJECT / "data/operations/m4_tests_stderr.log").read_text(encoding="utf-8-sig")
    match = re.search(r"Ran (\d+) tests in ([0-9.]+)s\s+OK", log)
    jobs = read_json(native / "cron/jobs.json")["jobs"]
    job = next(j for j in jobs if j["id"] == "8a4841ec482c")
    checks = {
        "installed_wrapper_matches": digest(source.read_bytes()) == digest(installed.read_bytes()),
        "numerical_code_unchanged": _code_hash() == manifest["code_hash"] and all(e["numerical_code_hash"] == _code_hash() for e in experiments),
        "baseline_config_unchanged": digest(canonical(config_load(PROJECT / "configs/baseline.json"))) == manifest["config_hash"],
        "m5_engine_matches_frozen_experiments": all(e["engine_hash"] == engine_hash() for e in experiments),
        "real_candidate_artifacts_unchanged": all(digest(Path(p).read_bytes()) == h for p, h in latest["service_artifacts"].items()),
        "installed_wrapper_synthetic_success": first["status"] == "SUCCEEDED",
        "installed_wrapper_repeat_reused": repeated["status"] == "SUCCEEDED" and not repeated["result"]["first_recorded"] and repeated["result"]["run_id"] == first["result"]["run_id"],
        "all_tests_passed": bool(match and int(match[1]) == 112),
        "existing_job_enabled_unchanged_schedule": job["enabled"] and job["no_agent"] and job["schedule"]["expr"] == "0 20 * * 1-5" and job["script"] == "alpha_loop_daily.py",
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    receipt = {"verified_at": now_iso(), "checks": checks, "test_count": int(match[1]), "test_seconds": float(match[2]),
               "test_log_hash": digest((PROJECT / "data/operations/m4_tests_stderr.log").read_bytes()),
               "wrapper_hash": digest(installed.read_bytes()), "numerical_code_hash": _code_hash(), "m5_engine_hash": engine_hash(),
               "real_run_id": latest["run_id"], "candidate_count": latest["candidate_count"],
               "next_run_at": job["next_run_at"], "job_id": job["id"], "operations": status(PROJECT),
               "acceptance": str(PROJECT / "data/operations/m4_acceptance.json"),
               "scheduled_new_wrapper_execution_verified": False, "real_gpu_oom_recovery_verified": False,
               "logout_sleep_power_recovery_verified": False, "market_quality_or_performance_proven": False}
    write_json(PROJECT / "data/operations/m4_deployment.json", receipt)
    print(json.dumps({"checks": checks, "tests": receipt["test_count"], "next_run_at": job["next_run_at"],
                      "real_run_id": latest["run_id"], "running": receipt["operations"]["running"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
