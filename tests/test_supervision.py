import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from alpha_loop.common import digest, now_iso, parse_time, read_json, write_json
from alpha_loop.fixture import create
from alpha_loop.runtime import alive, exclusive, heartbeat, owner, reconcile, status
from alpha_loop.service import operate
from alpha_loop.supervision import supervise, restore_gpu

PROJECT = Path(__file__).resolve().parents[1]


class SupervisionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {**read_json(PROJECT / "configs/hermes_demo.json"), "strategy_config": str(PROJECT / "configs/baseline.json")}
        write_json(self.root / "operation.json", self.config)
        create(self.root / "demo-input", without_turnover=True)
        self.env = {**os.environ, "PYTHONPATH": str(PROJECT / "src")}

    def tearDown(self):
        self.temp.cleanup()

    def completed(self, status_="SUCCEEDED", **kwargs):
        return subprocess.CompletedProcess([], 0, json.dumps({"status": status_, "qwen_status": "DISABLED", **kwargs}), "")

    def test_process_identity_and_pid_reuse(self):
        self.assertTrue(alive(owner()))
        with patch("alpha_loop.runtime.process_token", return_value="new-process"):
            self.assertFalse(alive({"pid": 42, "process_token": "old-process"}))
        with patch("alpha_loop.runtime.process_token", return_value=None):
            self.assertIsNone(alive({"pid": 42, "process_token": "old-process"}))

    def test_dead_running_reconciled_without_fabricated_end_time(self):
        dead = {"attempt_id": "dead", "status": "RUNNING", "started_at": now_iso(), "pid": 42, "process_token": "old"}
        path = self.root / "data/operations/service_attempts/dead.json"
        write_json(path, dead)
        with patch("alpha_loop.runtime.process_token", return_value="absent"):
            report = status(self.root)
            self.assertFalse(report["running"])
            self.assertEqual(report["latest_attempt"]["effective_status"], "INTERRUPTED_UNRECONCILED")
            self.assertEqual(reconcile(self.root), ["dead"])
        self.assertIsNone(read_json(path)["completed_at"])
        self.assertFalse(read_json(path)["completion_time_known"])

    def test_legacy_owner_unknown_not_claimed_running_or_terminated(self):
        path = self.root / "data/operations/service_attempts/legacy.json"
        write_json(path, {"attempt_id": "legacy", "status": "RUNNING", "started_at": now_iso()})
        self.assertEqual(reconcile(self.root), [])
        report = status(self.root)
        self.assertEqual(report["latest_attempt"]["effective_status"], "OWNER_UNKNOWN")
        self.assertFalse(report["running"])

    def test_heartbeat_and_collection_binding(self):
        path = self.root / "pulse.json"
        with heartbeat(path, interval=.01):
            import time
            for _ in range(100):
                if path.exists():
                    break
                time.sleep(.01)
            self.assertTrue(alive(read_json(path)))
        row = {"attempt_id": "active", "status": "RUNNING", "started_at": now_iso(), "request_id": "r", **owner()}
        write_json(self.root / "data/operations/service_attempts/active.json", row)
        write_json(self.root / "data/market/progress/r.json", {"completed": 12, "expected": 20})
        report = status(self.root)
        self.assertTrue(report["running"])
        self.assertEqual(report["collection_progress"][0]["completed"], 12)
        self.assertIsNone(report["estimated_minutes_remaining"])

    def test_supervisor_duplicate_and_foreign_worker_do_not_launch(self):
        with exclusive(self.root / "data/operations/.supervisor.lock"), patch("alpha_loop.supervision.run_bounded") as run:
            self.assertEqual(supervise(self.root, ["unused"], self.env)["status"], "SKIPPED_ALREADY_RUNNING")
            run.assert_not_called()
        write_json(self.root / "data/operations/service_attempts/live.json", {"attempt_id": "live", "status": "RUNNING", "started_at": now_iso(), **owner()})
        with patch("alpha_loop.supervision.run_bounded") as run:
            self.assertEqual(supervise(self.root, ["unused"], self.env)["status"], "SKIPPED_ALREADY_RUNNING")
            run.assert_not_called()

    def test_timeout_resume_pins_session_and_checkpoint_owner(self):
        envs = []
        def worker(command, **options):
            envs.append(dict(options["env"]))
            if len(envs) == 1:
                write_json(self.root / "data/operations/service_attempts/first.json", {
                    "attempt_id": "first", "supervision_id": envs[-1]["ALPHA_LOOP_SUPERVISION_ID"],
                    "status": "RUNNING", "started_at": now_iso(), "session": "2026-09-30", "pid": 99999999, "process_token": "old"})
                raise subprocess.TimeoutExpired(command, options["timeout"], output=b"partial")
            return self.completed()
        with patch("alpha_loop.supervision.run_bounded", side_effect=worker):
            result = supervise(self.root, ["unused"], self.env)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(result["trials"]), 2)
        self.assertEqual(envs[1]["ALPHA_LOOP_PINNED_SESSION"], "2026-09-30")
        self.assertEqual(envs[1]["ALPHA_LOOP_RESUME_ATTEMPT"], "first")
        self.assertEqual(read_json(self.root / "data/operations/service_attempts/first.json")["status"], "INTERRUPTED")

    def test_max_attempts_and_total_deadline(self):
        with patch("alpha_loop.supervision.run_bounded", side_effect=subprocess.TimeoutExpired("x", 1)) as run:
            result = supervise(self.root, ["unused"], self.env, max_attempts=2)
        self.assertEqual(result["status"], "TIMED_OUT")
        self.assertEqual(run.call_count, 2)
        # Deadline advances after setup; no worker may start when cleanup reserve is reached.
        with patch("alpha_loop.supervision.time.monotonic", side_effect=[0, 201]), patch("alpha_loop.supervision.run_bounded") as run:
            result = supervise(self.root, ["unused"], self.env, total_seconds=200)
        self.assertEqual(result["status"], "TIMED_OUT")
        run.assert_not_called()

    def test_cooldown_stops_timeout_retry(self):
        def limited(*args, **kwargs):
            write_json(self.root / "data/market/cooldown.json", {"until": (parse_time(now_iso()) + timedelta(hours=1)).isoformat(), "reason": "provider_rate_limit"})
            raise subprocess.TimeoutExpired("x", 1)
        with patch("alpha_loop.supervision.run_bounded", side_effect=limited) as run:
            result = supervise(self.root, ["unused"], self.env)
        self.assertEqual(result["status"], "DEFERRED_COOLDOWN")
        self.assertEqual(run.call_count, 1)

    def test_input_ai_invalid_output_not_blind_retried_and_logs_retained(self):
        scenarios = [subprocess.CompletedProcess([], 2, '{"error":"invalid config","type":"ValueError"}', "details"),
                     self.completed("SUCCEEDED_WITH_AI_FAILURE", qwen_status="FAILED"),
                     subprocess.CompletedProcess([], 0, "invalid json", "")]
        for proc in scenarios:
            with patch("alpha_loop.supervision.run_bounded", return_value=proc) as run:
                result = supervise(self.root, ["unused"], self.env)
            self.assertNotEqual(result["status"], "SUCCEEDED")
            self.assertEqual(run.call_count, 1)
            directory = self.root / "data/operations/supervision" / result["supervision_id"]
            self.assertEqual((directory / "1.stdout.log").read_text(), proc.stdout)

    def test_source_changed_between_retries_blocks(self):
        with patch("alpha_loop.provenance.archive_code", side_effect=["old", "old", "new"]), patch("alpha_loop.supervision.run_bounded", side_effect=subprocess.TimeoutExpired("x", 1)) as run:
            result = supervise(self.root, ["unused"], self.env)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("source changed", result["error"])
        self.assertEqual(run.call_count, 1)

    def test_gpu_cleanup_lock_failure_does_not_report_duplicate_success(self):
        with patch("alpha_loop.supervision.run_bounded", return_value=self.completed()), patch("alpha_loop.supervision.restore_gpu", side_effect=RuntimeError("ALREADY_RUNNING: GPU operational lock held")):
            result = supervise(self.root, ["unused"], self.env)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["result"]["status"], "SUCCEEDED")

    def test_wrapper_reports_failed_restoration_even_with_saved_numerical_result(self):
        import importlib.util
        import io
        from contextlib import redirect_stdout, redirect_stderr
        spec = importlib.util.spec_from_file_location("m4_wrapper", PROJECT / "integrations/hermes/alpha_loop_daily.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.PROJECT_ROOT = self.root
        record = {"supervision_id": "s", "status": "FAILED", "trials": [], "error": "GPU restoration failed",
                  "result": {"status": "SUCCEEDED", "qwen_status": "SUCCEEDED"}}
        with patch.dict(os.environ, {"ALPHA_LOOP_CONFIG": "operation.json"}), patch("alpha_loop.supervision.supervise", return_value=record), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(module.main(), 2)

    def test_abrupt_worker_exit_is_retried_but_handled_input_error_is_not(self):
        def crash(command, **options):
            write_json(self.root / "data/operations/service_attempts/crash.json", {
                "attempt_id": "crash", "supervision_id": options["env"]["ALPHA_LOOP_SUPERVISION_ID"],
                "status": "RUNNING", "started_at": now_iso(), "pid": 99999999, "process_token": "old"})
            return subprocess.CompletedProcess(command, 9, "", "terminated")
        with patch("alpha_loop.supervision.run_bounded", side_effect=[None]) as run:
            run.side_effect = lambda command, **options: crash(command, **options) if run.call_count == 1 else self.completed()
            result = supervise(self.root, ["unused"], self.env)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(result["trials"]), 2)
        self.assertEqual(result["trials"][0]["status"], "INTERRUPTED_PROCESS")

    def test_dead_supervisor_is_reconciled_on_next_entry(self):
        path = self.root / "data/operations/supervision/old/receipt.json"
        write_json(path, {"supervision_id": "old", "status": "RUNNING", "started_at": now_iso(), "pid": 99999999, "process_token": "old"})
        with patch("alpha_loop.supervision.run_bounded", return_value=self.completed()):
            result = supervise(self.root, ["unused"], self.env)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(read_json(path)["status"], "INTERRUPTED")
        self.assertIsNone(read_json(path)["completed_at"])

    def test_crash_after_screening_reuses_sealed_manifest_and_rejects_tamper(self):
        with patch("alpha_loop.service.derive_ranking", side_effect=RuntimeError("interrupted after screening")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                operate(self.root, self.root / "operation.json")
        checkpoints = list((self.root / "data/operations/service_checkpoints").glob("*.json"))
        sealed = read_json(checkpoints[0])
        csv_path = self.root / "outputs" / sealed["run_id"] / "candidates.csv"
        before = digest(csv_path.read_bytes())
        with patch("alpha_loop.service.run", side_effect=AssertionError("must reuse sealed run")):
            result = operate(self.root, self.root / "operation.json")
        self.assertEqual(result["run_id"], sealed["run_id"])
        self.assertEqual(digest(csv_path.read_bytes()), before)
        sealed["manifest_hash"] = "tampered"
        write_json(checkpoints[0], sealed)
        with self.assertRaisesRegex(RuntimeError, "checkpoint manifest hash"):
            operate(self.root, self.root / "operation.json")

    def test_resume_input_owner_and_config_are_verified(self):
        write_json(self.root / "data/operations/service_attempts/old.json", {"attempt_id": "old", "status": "FAILED", "started_at": now_iso(),
                   "config_hash": digest((self.root / "operation.json").read_bytes()), "supervision_id": "another", "input_dir": str(self.root / "demo-input"), "session": "2026-09-17"})
        with patch.dict(os.environ, {"ALPHA_LOOP_SUPERVISION_ID": "current", "ALPHA_LOOP_RESUME_ATTEMPT": "old"}):
            with self.assertRaisesRegex(ValueError, "ownership/config mismatch"):
                operate(self.root, self.root / "operation.json")
        with patch.dict(os.environ, {"ALPHA_LOOP_STRATEGY_CONFIG_HASH": "changed"}):
            with self.assertRaisesRegex(RuntimeError, "strategy config changed"):
                operate(self.root, self.root / "operation.json")

    def test_changed_resume_input_is_rejected(self):
        first = operate(self.root, self.root / "operation.json")
        path = next((self.root / "data/operations/service_attempts").glob("*.json"))
        row = read_json(path)
        row["supervision_id"] = "s"
        write_json(path, row)
        before = digest(Path(first["candidate_csv"]).read_bytes())
        source_path = self.root / "demo-input/source.json"
        source = read_json(source_path)
        source["description"] = "changed after seal"
        write_json(source_path, source)
        with patch.dict(os.environ, {"ALPHA_LOOP_SUPERVISION_ID": "s", "ALPHA_LOOP_RESUME_ATTEMPT": row["attempt_id"]}):
            with self.assertRaisesRegex(RuntimeError, "resume input changed"):
                operate(self.root, self.root / "operation.json")
        self.assertEqual(digest(Path(first["candidate_csv"]).read_bytes()), before)

    def test_wrapper_missing_config_records_startup_failure(self):
        env = {**self.env, "ALPHA_LOOP_ROOT": str(self.root), "ALPHA_LOOP_CONFIG": "missing.json"}
        proc = subprocess.run([sys.executable, str(PROJECT / "integrations/hermes/alpha_loop_daily.py")], env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stderr)
        self.assertEqual(payload["status"], "FAILED_STARTUP")
        self.assertEqual(read_json(Path(payload["receipt"]))["error_type"], "FileNotFoundError")
        with patch.dict(os.environ, {"ALPHA_LOOP_OPERATION_CONFIG_HASH": "changed"}):
            with self.assertRaisesRegex(RuntimeError, "config changed"):
                operate(self.root, self.root / "operation.json")

    def test_gpu_journal_restores_only_owned_containers_and_refuses_live_owner(self):
        lease_path = self.root / "data/operations/gpu_leases/l.json"
        lease = {"lease_id": "l", "status": "ACTIVE", "supervision_id": "s", "pid": 99999999, "process_token": "old", "before": {"openjev": "exited", "qwen": "exited"}}
        write_json(lease_path, lease)
        calls = []
        states = {"alpha-loop-openjev": "exited", "alpha-loop-qwen": "running"}
        def docker(*args, **kwargs):
            calls.append(args)
            if args[0] == "stop":
                states[args[1]] = "exited"
                return ""
            return states[args[1]]
        with patch("alpha_loop.models._docker", side_effect=docker):
            self.assertEqual(restore_gpu(self.root, "s")[0]["status"], "RESTORED")
        self.assertIn(("stop", "alpha-loop-qwen"), calls)
        self.assertTrue(all(c[1].startswith("alpha-loop-") for c in calls))
        lease.update(owner(), status="ACTIVE")
        write_json(lease_path, lease)
        with self.assertRaisesRegex(RuntimeError, "alive or unknown"):
            restore_gpu(self.root, "s")

    def test_actual_child_timeout_kills_descendants_and_resumes_from_marker(self):
        worker = self.root / "worker.py"
        worker.write_text('''import os, sys, time, subprocess, json
from pathlib import Path
root=Path.cwd()
marker=root/'checkpoint.txt'
if not marker.exists():
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    marker.write_text(str(child.pid))
    time.sleep(60)
print(json.dumps({'status':'SUCCEEDED','qwen_status':'DISABLED','cached':True}))
''', encoding="utf-8")
        result = supervise(self.root, [sys.executable, str(worker)], self.env, slice_seconds=1, max_attempts=2)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(result["trials"]), 2)
        from alpha_loop.runtime import process_token
        self.assertEqual(process_token(int((self.root / "checkpoint.txt").read_text())), "absent")

    def test_exited_parent_with_open_descendant_pipes_is_cleaned(self):
        from alpha_loop.processes import run_bounded
        from alpha_loop.runtime import process_token
        marker = self.root / "descendant.txt"
        program = "import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid))"
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, "-c", program, str(marker)], timeout=.5)
        self.assertEqual(process_token(int(marker.read_text())), "absent")

    @unittest.skipUnless(os.name == "nt", "Windows job handle closure")
    def test_windows_supervisor_abrupt_exit_closes_job_and_kills_worker(self):
        marker = self.root / "worker_pid.txt"
        child_code = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        parent_code = '''import os,sys,time,threading
from pathlib import Path
from alpha_loop.processes import run_bounded
def abort():
    for _ in range(200):
        if Path(sys.argv[1]).exists():
            os._exit(9)
        time.sleep(.01)
threading.Thread(target=abort,daemon=True).start()
run_bounded([sys.executable,'-c',sys.argv[2],sys.argv[1]],timeout=5)
'''
        result = subprocess.run([sys.executable, "-c", parent_code, str(marker), child_code], env=self.env,
                                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 9, result.stderr)
        from alpha_loop.runtime import process_token
        import time
        for _ in range(100):
            if process_token(int(marker.read_text())) == "absent":
                break
            time.sleep(.01)
        self.assertEqual(process_token(int(marker.read_text())), "absent")


if __name__ == "__main__":
    unittest.main()
