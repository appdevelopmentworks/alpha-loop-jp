"""Local file-driven daily orchestration. No market API, LLM, or order side effects."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
import ctypes
from datetime import datetime
from pathlib import Path

from .common import canonical, digest, now_iso, parse_time, read_json, write_json
from .pipeline import _code_hash, _verified_manifest, evaluate_run, run


@contextmanager
def _daily_lock(root: Path):
    path = root / "data" / "operations" / ".daily.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 60
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("daily job lock timeout")
                    time.sleep(.1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _job_key(root: Path, input_dir: Path, config_path: Path, session: str) -> str:
    names = ("calendar.json", "instruments.json", "bars.json", "source.json", "disclosures.json")
    inputs = {name: digest((input_dir / name).read_bytes()) if (input_dir / name).exists() else None
              for name in names}
    manual = root / "data" / "manual_disclosures"
    documents = {p.name: digest(p.read_bytes()) for p in sorted(manual.glob("*.json"))} if manual.exists() else {}
    return digest(canonical({"session": session, "inputs": inputs, "manual": documents,
                             "config": digest(config_path.read_bytes()), "code": _code_hash()}))


def doctor(root: Path, input_dir: Path) -> dict:
    """Record operational facts without reading credentials or changing services."""
    files = {name: (input_dir / name).is_file() for name in
             ("calendar.json", "instruments.json", "bars.json", "source.json")}
    disk = shutil.disk_usage(root)
    result = {"checked_at": now_iso(), "platform": platform.platform(),
              "python": platform.python_version(), "project_root": str(root.resolve()),
              "input_dir": str(input_dir.resolve()), "input_files": files,
              "free_disk_bytes": disk.free, "uv_executable": shutil.which("uv"),
              "docker_executable": shutil.which("docker"),
              "hermes_executable": shutil.which("hermes")}
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended", ctypes.c_ulonglong)]
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            result["ram_total_bytes"] = memory.total_physical
            result["ram_available_bytes"] = memory.available_physical
    if shutil.which("nvidia-smi"):
        try:
            proc = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                  timeout=10, check=True)
            result["gpu_query"] = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            result["gpu_query"] = "unavailable"
    return result


def daily(root: Path, input_dir: Path, config_path: Path, session: str, as_of: str,
          evaluate_run_id: str | None = None, through: str | None = None) -> dict:
    """Run screening before optional outcome evaluation and keep every attempt."""
    datetime.strptime(session, "%Y-%m-%d")
    parse_time(as_of)
    if evaluate_run_id and not through or through and not evaluate_run_id:
        raise ValueError("--evaluate-run-id and --through must be supplied together")
    if through:
        datetime.strptime(through, "%Y-%m-%d")
        if through > session:
            raise ValueError("future evaluation date is unavailable")
    attempt_id = uuid.uuid4().hex
    attempt_path = root / "data" / "operations" / "attempts" / f"{attempt_id}.json"
    attempt = {"attempt_id": attempt_id, "started_at": now_iso(), "session": session,
               "as_of_requested": as_of, "input_dir": str(input_dir.resolve()),
               "config_path": str(config_path.resolve()), "status": "RUNNING"}
    write_json(attempt_path, attempt)
    try:
        with _daily_lock(root):
            job_key = _job_key(root, input_dir, config_path, session)
            job_path = root / "data" / "operations" / "jobs" / f"{job_key}.json"
            prior_job = read_json(job_path) if job_path.exists() else None
            if prior_job:
                manifest = _verified_manifest(root / "outputs" / prior_job["run_id"])
                if manifest is None or digest((root / "outputs" / prior_job["run_id"] / "run_manifest.json").read_bytes()) != prior_job["manifest_hash"]:
                    raise RuntimeError("daily job artifact failed verification")
            else:
                manifest = run(root, input_dir, config_path, session, as_of)
            run_id = manifest["run_id"]
            result = {"run_id": run_id, "candidate_csv": str(root / "outputs" / run_id / "candidates.csv"),
                      "data_grade": manifest["data_grade"], "history_only": manifest["history_only"],
                      "prediction_eligible": manifest["data_grade"] in ("observed", "vendor_pit") and not manifest["history_only"]}
            manifest_hash = digest((root / "outputs" / run_id / "run_manifest.json").read_bytes())
            if not prior_job:
                write_json(job_path, {"job_key": job_key, "run_id": run_id,
                                      "manifest_hash": manifest_hash, "recorded_at": now_iso()})
            if evaluate_run_id:
                prior_manifest = read_json(root / "outputs" / evaluate_run_id / "run_manifest.json")
                if prior_manifest["target_session"] >= session:
                    raise ValueError("evaluation run must precede daily session")
                evaluation = evaluate_run(root, evaluate_run_id, input_dir, through)
                result["evaluation_id"] = evaluation["evaluation_id"]
                result["outcome_csv"] = str(Path(evaluation["path"]) / "outcomes.csv")
            receipt_path = root / "data" / "operations" / "receipts" / f"{run_id}.json"
            receipt = {"run_id": run_id, "manifest_hash": manifest_hash,
                       "candidate_csv": result["candidate_csv"],
                       "prediction_eligible": result["prediction_eligible"]}
            if receipt_path.exists():
                if read_json(receipt_path) != receipt:
                    raise RuntimeError("daily receipt differs from completed run")
                result["first_recorded"] = False
            else:
                write_json(receipt_path, receipt)
                result["first_recorded"] = True
        attempt.update(result)
        attempt.update({"status": "SUCCEEDED", "completed_at": now_iso()})
        write_json(attempt_path, attempt)
        return attempt
    except Exception as error:
        attempt.update({"status": "FAILED", "completed_at": now_iso(),
                        "error_type": type(error).__name__, "error": str(error)})
        write_json(attempt_path, attempt)
        raise
