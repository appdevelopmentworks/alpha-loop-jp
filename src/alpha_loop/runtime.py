"""Local process ownership and operational receipts; no market or AI calls."""
from __future__ import annotations

import ctypes
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .common import now_iso, parse_time, read_json, write_json


def process_token(pid: int) -> str | None:
    """OS creation identity prevents a reused PID from being treated as the owner."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
                return "absent"
            return None  # access denied is unknown, never proof of death
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return "absent"
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return None
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw.rsplit(")", 1)[1].split()[19]  # field 22, process start ticks
    except FileNotFoundError:
        return "absent"
    except OSError:
        return None


def owner() -> dict:
    return {"pid": os.getpid(), "process_token": process_token(os.getpid())}


def alive(record: dict) -> bool | None:
    expected = record.get("process_token")
    if not record.get("pid") or not expected or expected == "absent":
        return None
    actual = process_token(record["pid"])
    return None if actual is None else actual == expected


@contextmanager
def exclusive(path: Path):
    """Immediate OS lock; existence of the file is not the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise RuntimeError("ALREADY_RUNNING: operational lock held") from error
        try:
            yield
        finally:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def heartbeat(path: Path, interval: float = 5):
    record = owner()
    stop = threading.Event()
    def pulse():
        while not stop.is_set():
            write_json(path, {**record, "heartbeat_at": now_iso()})
            stop.wait(interval)
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


def reconcile(root: Path, supervision_id: str | None = None, reason: str = "owner process ended") -> list[str]:
    """Only OS-proven dead owners are changed. Old records without PID remain unknown."""
    changed = []
    for path in (root / "data/operations/service_attempts").glob("*.json"):
        record = read_json(path)
        if record["status"] != "RUNNING" or supervision_id and record.get("supervision_id") != supervision_id:
            continue
        if alive(record) is False:
            record.update(status="INTERRUPTED", detected_at=now_iso(), interruption_reason=reason,
                          completed_at=None, completion_time_known=False)
            # Detection time is not invented as the actual termination time.
            write_json(path, record)
            changed.append(record["attempt_id"])
    return changed


def status(root: Path) -> dict:
    checked = now_iso()
    attempts = []
    for path in (root / "data/operations/service_attempts").glob("*.json"):
        row = read_json(path)
        state = alive(row) if row["status"] == "RUNNING" else False
        row["owner_alive"] = state
        row["effective_status"] = ("INTERRUPTED_UNRECONCILED" if state is False else "OWNER_UNKNOWN" if state is None else "RUNNING") if row["status"] == "RUNNING" else row["status"]
        pulse = root / "data/operations/heartbeats" / (row["attempt_id"] + ".json")
        if pulse.exists():
            value = read_json(pulse)
            row["heartbeat_at"] = value["heartbeat_at"]
            row["heartbeat_age_seconds"] = max(0, (parse_time(checked) - parse_time(value["heartbeat_at"])).total_seconds())
            row["heartbeat_health"] = "RECENT" if row["heartbeat_age_seconds"] <= 30 else "STALE"
        attempts.append(row)
    attempts.sort(key=lambda row: (row["started_at"], row["attempt_id"]), reverse=True)
    latest_path = root / "data/operations/latest_service.json"
    supervisors = [read_json(p) for p in (root / "data/operations/supervision").glob("*/receipt.json")]
    supervisors.sort(key=lambda row: (row["started_at"], row["supervision_id"]), reverse=True)
    for row in supervisors:
        if row["status"] == "RUNNING":
            state = alive(row)
            row["owner_alive"] = state
            row["effective_status"] = "RUNNING" if state else "INTERRUPTED_UNRECONCILED" if state is False else "OWNER_UNKNOWN"
    supervisor = next((r for r in supervisors if r.get("effective_status") == "RUNNING"), supervisors[0] if supervisors else None)
    active = [row for row in attempts if row["status"] == "RUNNING" and row["owner_alive"] is not False]
    progress = []
    for row in active:
        request_id = row.get("request_id")
        path = root / "data/market/progress" / (str(request_id) + ".json")
        if request_id and path.exists():
            progress.append(read_json(path))
    cooldown_path = root / "data/market/cooldown.json"
    cooldown = read_json(cooldown_path) if cooldown_path.exists() else None
    if cooldown:
        cooldown["active"] = parse_time(cooldown["until"]) > parse_time(checked)
    return {"checked_at": checked, "running": any(r["owner_alive"] is True for r in active) or bool(supervisor and supervisor.get("effective_status") == "RUNNING"),
            "owner_unknown": any(r["owner_alive"] is None for r in active) or bool(supervisor and supervisor.get("effective_status") == "OWNER_UNKNOWN"),
            "active_attempts": active, "latest_attempt": attempts[0] if attempts else None,
            "latest_result": read_json(latest_path) if latest_path.exists() else None,
            "supervision": supervisor, "collection_progress": progress, "cooldown": cooldown,
            "estimated_minutes_remaining": None, "estimate_reason": "remaining fetch/model latency is not bounded by observed throughput"}
