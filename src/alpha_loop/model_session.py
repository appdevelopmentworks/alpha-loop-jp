"""Journaled exclusive local model use for optional material analysis."""
import os
import uuid
from contextlib import contextmanager

from .common import now_iso, write_json
from .runtime import owner


@contextmanager
def local_model(root, kind, timeout_seconds=180):
    from .models import _gpu_lock
    with _gpu_lock(root):
        with _session(root, kind, timeout_seconds):
            yield


@contextmanager
def _session(root, kind, timeout_seconds):
    from .models import CONTAINERS, _docker, model_status, switch_model
    states = model_status()
    if not states["exclusive"]:
        raise RuntimeError("both local model containers are running")
    before = states["containers"]
    previous = next((k for k, v in before.items() if v == "running"), None)
    lease_id = uuid.uuid4().hex
    path = root / "data/operations/gpu_leases" / (lease_id + ".json")
    lease = {"lease_id": lease_id, **owner(), "before": before, "target": kind, "status": "ACTIVE",
             "supervision_id": os.environ.get("ALPHA_LOOP_SUPERVISION_ID"), "started_at": now_iso()}
    write_json(path, lease)
    try:
        switch_model(root, kind, timeout_seconds, _lock_held=True)
        yield
    finally:
        try:
            if previous != kind:
                _docker("stop", CONTAINERS[kind])
                if previous:
                    switch_model(root, previous, timeout_seconds, _lock_held=True)
            lease.update(status="RESTORED", restored_at=now_iso())
        except Exception as error:
            lease.update(status="RESTORE_FAILED", error=str(error)[:300])
            raise
        finally:
            write_json(path, lease)
