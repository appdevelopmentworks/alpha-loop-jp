"""Explicit, local Docker switching for the two project-owned GPU containers."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .common import now_iso, write_json


CONTAINERS = {"openjev": "alpha-loop-openjev", "qwen": "alpha-loop-qwen"}
ENDPOINTS = {"openjev": ("http://127.0.0.1:8080/v1/models", "openjev-0.1"),
             "qwen": ("http://127.0.0.1:8000/v1/models", "Inferact/Qwen3.8-27B-NVFP4")}


def _docker(*args: str, timeout: float = 60) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def _state(kind: str) -> str:
    return _docker("inspect", CONTAINERS[kind], "--format", "{{.State.Status}}")


def model_status() -> dict:
    states = {kind: _state(kind) for kind in CONTAINERS}
    return {"containers": states, "exclusive": sum(state == "running" for state in states.values()) <= 1}


def _ready(kind: str) -> bool:
    url, expected = ENDPOINTS[kind]
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.loads(response.read(100_000))
        ids = ({item.get("name") for item in data.get("models", [])} if kind == "openjev"
               else {item.get("id") for item in data.get("data", [])})
        return expected in ids
    except (OSError, ValueError, KeyError):
        return False


@contextmanager
def _gpu_lock(root: Path):
    path = root / "data" / "operations" / ".gpu.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
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


def switch_model(root: Path, target: str, timeout_seconds: float = 1200, *, _lock_held: bool = False) -> dict:
    if target not in CONTAINERS or timeout_seconds <= 0:
        raise ValueError("invalid model switch target or timeout")
    other = "qwen" if target == "openjev" else "openjev"
    attempt_id = uuid.uuid4().hex
    path = root / "data" / "operations" / "model_switches" / f"{attempt_id}.json"
    record = {"attempt_id": attempt_id, "target": target, "started_at": now_iso(), "status": "RUNNING"}
    write_json(path, record)
    started_target = False
    try:
        with nullcontext() if _lock_held else _gpu_lock(root):
            states = model_status()["containers"]
            record["before"] = states
            if states[target] == "running" and states[other] == "running":
                raise RuntimeError("both project GPU containers are running")
            if states[other] == "running":
                _docker("stop", CONTAINERS[other])
                if _state(other) == "running":
                    raise RuntimeError("other model did not stop")
            if _state(target) != "running":
                _docker("start", CONTAINERS[target])
                started_target = True
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if _state(target) != "running":
                    raise RuntimeError("target container exited before ready")
                if _ready(target):
                    after = model_status()
                    if not after["exclusive"]:
                        raise RuntimeError("project GPU containers lost exclusivity")
                    record.update({"status": "SUCCEEDED", "completed_at": now_iso(),
                                   "after": after["containers"]})
                    write_json(path, record)
                    return record
                time.sleep(3)
            raise TimeoutError("target model API did not become ready")
    except Exception as error:
        if started_target:
            try:
                _docker("stop", CONTAINERS[target])
            except Exception as cleanup_error:
                record["cleanup_error"] = str(cleanup_error)[:300]
        record.update({"status": "FAILED", "completed_at": now_iso(),
                       "error_type": type(error).__name__, "error": str(error)})
        write_json(path, record)
        raise
