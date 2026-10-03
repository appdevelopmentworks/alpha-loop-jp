"""Offline M4 acceptance: interrupt actual worker, auto resume, evaluate next day."""
from pathlib import Path
import json
import os
import shutil
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from alpha_loop.common import digest, now_iso, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.pipeline import replay
from alpha_loop.runtime import status
from alpha_loop.service import operate
from alpha_loop.supervision import supervise


def main():
    root = PROJECT / "data/m4_demo" / uuid.uuid4().hex[:12]
    fixture = create(root / "demo-input", without_turnover=True)
    config = {**read_json(PROJECT / "configs/hermes_demo.json"),
              "strategy_config": str(PROJECT / "configs/baseline.json"), "qwen_enabled": True}
    write_json(root / "operation.json", config)
    worker = root / "acceptance_worker.py"
    worker.write_text('''import json, time
from pathlib import Path
from alpha_loop.service import operate
import alpha_loop.service as service
root = Path.cwd()
def fake_qwen(*args):
    marker = root / 'synthetic_interrupt_marker.txt'
    if not marker.exists():
        marker.write_text('simulated AI wait; no model/GPU call', encoding='utf-8')
        time.sleep(60)
    return {'daily_report': 'synthetic stub, not real model quality'}
service._qwen = fake_qwen
print(json.dumps(operate(root, root/'operation.json')))
''', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(PROJECT / "src"), "PYTHONIOENCODING": "utf-8",
           "ALPHA_LOOP_OPERATION_CONFIG_HASH": digest((root / "operation.json").read_bytes())}
    first = supervise(root, [sys.executable, str(worker)], env, slice_seconds=3, max_attempts=2)
    if first["status"] != "SUCCEEDED" or len(first["trials"]) != 2 or first["trials"][0].get("status") != "TIMED_OUT":
        raise AssertionError(first)
    result = first["result"]
    attempts = [read_json(p) for p in (root / "data/operations/service_attempts").glob("*.json")]
    if len({a["run_id"] for a in attempts}) != 1 or sum(a["status"] == "INTERRUPTED" for a in attempts) != 1:
        raise AssertionError("sealed run was not reused or interrupted owner was not reconciled")
    before = digest(Path(result["candidate_csv"]).read_bytes())
    config["qwen_enabled"] = False
    write_json(root / "operation.json", config)
    # Complete the following synthetic session; retain all source timestamps as synthetic.
    future = read_json(root / "demo-input/future.json")
    bars = read_json(root / "demo-input/bars.json")
    fetched = fixture["next_session"] + "T19:30:00+09:00"
    for bar in future["bars"]:
        bars.append({**bar, "open": bar["adj_open"], "high": bar["adj_high"], "low": bar["adj_low"],
                     "close": bar["adj_close"], "volume": 1000000, "adj_volume": 1000000,
                     "turnover_jpy": None, "fetched_at": fetched})
    for bar in bars:
        bar["fetched_at"] = fetched
    write_json(root / "demo-input/bars.json", bars)
    calendar = read_json(root / "demo-input/calendar.json")
    calendar.append(fixture["next_session"])
    write_json(root / "demo-input/calendar.json", calendar)
    source = read_json(root / "demo-input/source.json")
    source.update(fetched_at=fetched, available_at=fetched)
    write_json(root / "demo-input/source.json", source)
    following = operate(root, root / "operation.json")
    repeated = operate(root, root / "operation.json")
    if following["run_id"] != repeated["run_id"] or repeated["first_recorded"] or len(following["evaluations"]) != 1:
        raise AssertionError("next-session evaluation/duplicate gate failed")
    if digest(Path(result["candidate_csv"]).read_bytes()) != before:
        raise AssertionError("earlier candidate CSV changed")
    replay_result = replay(root, result["run_id"])
    report = status(root)
    if report["running"]:
        raise AssertionError("completed process still reported running")
    # Prepare the isolated root for the real installed wrapper's smoke test.
    shutil.copytree(PROJECT / "src/alpha_loop", root / "src/alpha_loop", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "configs").mkdir(exist_ok=True)
    shutil.copyfile(PROJECT / "configs/baseline.json", root / "configs/baseline.json")
    shutil.copyfile(PROJECT / "configs/hermes_demo.json", root / "configs/hermes_demo.json")
    receipt = {"verified_at": now_iso(), "root": str(root), "data_grade": "synthetic",
               "site_packages_disabled": sys.flags.no_site == 1, "api_required": False, "gpu_required": False,
               "interrupted_and_resumed": True, "same_sealed_run_reused": True,
               "earlier_csv_unchanged": True, "next_session_evaluation": following["evaluations"],
               "duplicate_reused": True, "replay": replay_result, "running_after_completion": report["running"],
               "supervision": first, "candidate_csv": result["candidate_csv"],
               "unverified": ["next scheduled full-market success", "actual logout/sleep/power recovery", "real GPU OOM recovery", "long-term provider coverage"]}
    write_json(PROJECT / "data/operations/m4_acceptance.json", receipt)
    print(json.dumps({k: receipt[k] for k in ("root", "candidate_csv", "next_session_evaluation", "interrupted_and_resumed", "same_sealed_run_reused", "running_after_completion")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
