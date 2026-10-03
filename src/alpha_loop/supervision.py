"""Bounded automatic resume for the existing Hermes script-only entry point."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .common import JST, atomic_bytes, now_iso, parse_time, read_json, write_json
from .processes import run_bounded
from .runtime import alive, exclusive, heartbeat, owner, reconcile

TOTAL_SECONDS = 7100
SLICE_SECONDS = 3000
MAX_ATTEMPTS = 3
CLEANUP_RESERVE_SECONDS = 180


def _attempts(root: Path, supervision_id: str) -> list[dict]:
    result = [read_json(p) for p in (root / "data/operations/service_attempts").glob("*.json")]
    return sorted((r for r in result if r.get("supervision_id") == supervision_id), key=lambda r: (r.get("supervision_trial", 0), r["started_at"], r["attempt_id"]))


def restore_gpu(root: Path, supervision_id: str, deadline: float | None = None) -> list[dict]:
    """Restore only a dead worker's journaled project-owned model states."""
    from .models import CONTAINERS, _docker
    def docker(*args, timeout=10):
        remaining = deadline - time.monotonic() - 2 if deadline is not None else timeout
        if remaining <= 0:
            raise TimeoutError("total deadline reached during GPU restoration")
        return _docker(*args, timeout=min(timeout, remaining))
    def states():
        return {kind: docker("inspect", name, "--format", "{{.State.Status}}") for kind, name in CONTAINERS.items()}
    results = []
    for path in (root / "data/operations/gpu_leases").glob("*.json"):
        lease = read_json(path)
        if lease.get("supervision_id") != supervision_id or lease["status"] == "RESTORED":
            continue
        if alive(lease) is not False:
            raise RuntimeError("GPU lease owner is still alive or unknown")
        before = lease["before"]
        if sum(v == "running" for v in before.values()) > 1:
            raise RuntimeError("invalid exclusive GPU lease")
        try:
            with exclusive(root / "data/operations/.gpu.lock"):
                current = states()
                for kind in CONTAINERS:
                    if before[kind] != "running" and current[kind] == "running":
                        docker("stop", CONTAINERS[kind], timeout=20)
                for kind in CONTAINERS:
                    if before[kind] == "running" and current[kind] != "running":
                        docker("start", CONTAINERS[kind], timeout=20)
                after = states()
                if any((after[k] == "running") != (before[k] == "running") for k in before):
                    raise RuntimeError("GPU state restoration incomplete")
            lease.update(status="RESTORED", restored_at=now_iso(), after=after,
                         restored_by="supervisor; container state only, API readiness not asserted")
        except Exception as error:
            lease.update(status="RESTORE_FAILED", error=str(error)[:500], detected_at=now_iso())
            write_json(path, lease)
            raise
        write_json(path, lease)
        results.append({"lease_id": lease["lease_id"], "status": lease["status"]})
    return results


def supervise(root: Path, command: list[str], env: dict, *, total_seconds: float = TOTAL_SECONDS,
              slice_seconds: float = SLICE_SECONDS, max_attempts: int = MAX_ATTEMPTS) -> dict:
    if total_seconds <= CLEANUP_RESERVE_SECONDS or slice_seconds <= 0 or not 1 <= max_attempts <= 3:
        raise ValueError("invalid supervision bounds")
    supervision_id = uuid.uuid4().hex
    directory = root / "data/operations/supervision" / supervision_id
    record = {"supervision_id": supervision_id, **owner(), "started_at": datetime.now(JST).isoformat(), "status": "RUNNING",
              "total_seconds": total_seconds, "slice_seconds": slice_seconds, "max_attempts": max_attempts,
              "command": command, "trials": []}
    deadline = time.monotonic() + total_seconds
    receipt = directory / "receipt.json"
    record["deadline_at"] = (parse_time(record["started_at"]) + timedelta(seconds=total_seconds)).isoformat()
    claimed = False
    try:
        with exclusive(root / "data/operations/.supervisor.lock"):
            claimed = True
            if any(alive(r) is True and r["status"] == "RUNNING" for r in _attempts_all(root)):
                record.update(status="SKIPPED_ALREADY_RUNNING", completed_at=now_iso())
                write_json(receipt, record)
                return record
            reconcile(root)
            record["recovered_supervisors"] = []
            for prior_path in (root / "data/operations/supervision").glob("*/receipt.json"):
                prior = read_json(prior_path)
                if alive(prior) is False:
                    if prior["status"] == "RUNNING":
                        prior.update(status="INTERRUPTED", detected_at=now_iso(), completed_at=None)
                        write_json(prior_path, prior)
                    restoration = restore_gpu(root, prior["supervision_id"], deadline)
                    record["recovered_supervisors"].append({"supervision_id": prior["supervision_id"], "gpu_restoration": restoration})
            write_json(receipt, record)
            env = {**env, "ALPHA_LOOP_SUPERVISION_ID": supervision_id}
            env.pop("ALPHA_LOOP_RESUME_ATTEMPT", None)
            env.pop("ALPHA_LOOP_PINNED_SESSION", None)
            # Freeze the source/config arguments over all retries; never force refresh.
            from .provenance import archive_code
            record["code_bundle_id"] = archive_code(root)
            with heartbeat(directory / "heartbeat.json"):
                for number in range(1, max_attempts + 1):
                    env["ALPHA_LOOP_SUPERVISION_TRIAL"] = str(number)
                    remaining = deadline - time.monotonic() - CLEANUP_RESERVE_SECONDS
                    if remaining <= 0:
                        record.update(status="TIMED_OUT", error="total deadline exhausted")
                        break
                    cooldown_path = root / "data/market/cooldown.json"
                    if number > 1 and cooldown_path.exists() and parse_time(read_json(cooldown_path)["until"]) > parse_time(now_iso()):
                        record.update(status="DEFERRED_COOLDOWN", error="provider cooldown active; no automatic retry")
                        break
                    if archive_code(root) != record["code_bundle_id"]:
                        raise RuntimeError("source changed during supervision; refusing mixed-code resume")
                    previous = _attempts(root, supervision_id)
                    if previous:
                        env["ALPHA_LOOP_RESUME_ATTEMPT"] = previous[-1]["attempt_id"]
                        if previous[-1].get("session"):
                            env["ALPHA_LOOP_PINNED_SESSION"] = previous[-1]["session"]
                    trial = {"number": number, "started_at": now_iso(), "timeout_seconds": min(slice_seconds, remaining)}
                    record["trials"].append(trial)
                    write_json(receipt, record)
                    timed_out = False
                    restartable = False
                    try:
                        proc = run_bounded(command, cwd=root, env=env, text=True, encoding="utf-8", timeout=trial["timeout_seconds"])
                        atomic_bytes(directory / f"{number}.stdout.log", proc.stdout.encode("utf-8"))
                        atomic_bytes(directory / f"{number}.stderr.log", proc.stderr.encode("utf-8"))
                        trial["returncode"] = proc.returncode
                        try:
                            payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
                            if not isinstance(payload, dict):
                                raise ValueError("JSON object required")
                        except ValueError:
                            payload = {}
                        if proc.returncode:
                            record.update(status="FAILED", error=payload.get("error", proc.stderr[-500:]), error_type=payload.get("type"))
                            workers = _attempts(root, supervision_id)
                            restartable = bool(proc.returncode != 2 and not payload.get("error") and workers and workers[-1]["status"] == "RUNNING" and alive(workers[-1]) is False)
                            if restartable:
                                trial["status"] = "INTERRUPTED_PROCESS"
                        elif payload.get("status") not in ("SUCCEEDED", "SUCCEEDED_WITH_GAPS", "SUCCEEDED_WITH_AI_FAILURE", "SUCCEEDED_WITH_M5_FAILURE", "SUCCEEDED_WITH_SIDE_FAILURE"):
                            record.update(status="FAILED", error="worker returned invalid result JSON/status")
                        else:
                            record.update(status=payload["status"], result=payload)
                            record.pop("error", None)
                            record.pop("error_type", None)
                    except subprocess.TimeoutExpired as error:
                        timed_out = True
                        restartable = True
                        trial["status"] = "TIMED_OUT"
                        # Partial stdout/stderr can contain bytes even in text mode.
                        for name, value in (("stdout", error.output), ("stderr", error.stderr)):
                            if value:
                                atomic_bytes(directory / f"{number}.{name}.log", value.encode("utf-8") if isinstance(value, str) else value)
                        record.update(status="TIMED_OUT", error="worker time slice exhausted")
                    finally:
                        trial["ended_at"] = now_iso()
                        trial["reconciled_attempts"] = reconcile(root, supervision_id, "worker timeout" if timed_out else "worker exited")
                        write_json(receipt, record)
                        trial["gpu_restoration"] = restore_gpu(root, supervision_id, deadline)
                    write_json(receipt, record)
                    if not restartable:
                        break  # deterministic input/AI failures are not blind-retried
                    record["status"] = "RUNNING" if number < max_attempts else "TIMED_OUT" if timed_out else "FAILED"
                    write_json(receipt, record)
                record["completed_at"] = now_iso()
                write_json(receipt, record)
                return record
    except RuntimeError as error:
        if str(error).startswith("ALREADY_RUNNING") and not claimed:
            record.update(status="SKIPPED_ALREADY_RUNNING", completed_at=now_iso())
        else:
            record.update(status="FAILED", error=str(error), completed_at=now_iso())
        write_json(receipt, record)
        return record
    except Exception as error:
        record.update(status="FAILED", error=str(error), error_type=type(error).__name__, completed_at=now_iso())
        write_json(receipt, record)
        return record


def _attempts_all(root: Path) -> list[dict]:
    return [read_json(p) for p in (root / "data/operations/service_attempts").glob("*.json")]
