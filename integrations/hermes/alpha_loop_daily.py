"""Hermes no-agent cron entry point. Copy this file into Hermes' scripts directory."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(os.environ.get("ALPHA_LOOP_ROOT", str(Path.home() / "Desktop" / "alpha-loop-jp")))
# Full-market collection can exceed one hour with one bounded request at a time.
# Set Hermes cron.script_timeout_seconds=7200; leave time for descendant cleanup.
PROCESS_TIMEOUT_SECONDS = 7100


def main() -> int:
    if not PROJECT_ROOT.is_dir():
        print(f"Alpha Loop project missing: {PROJECT_ROOT}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from alpha_loop.supervision import supervise
    from alpha_loop.common import digest
    uv = shutil.which("uv") or str(Path.home() / "AppData" / "Local" / "hermes" / "bin" / "uv.exe")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["UV_CACHE_DIR"] = str(PROJECT_ROOT / ".uv-cache")
    env["PYTHONIOENCODING"] = "utf-8"
    # Hermes sets VIRTUAL_ENV to its own environment; select the project's uv environment.
    env["VIRTUAL_ENV"] = str(PROJECT_ROOT / ".venv")
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("UV_PYTHON", None)
    operation_config = os.environ.get('ALPHA_LOOP_CONFIG', 'configs/hermes_self_improving.json')
    config_path = PROJECT_ROOT / operation_config
    env["ALPHA_LOOP_OPERATION_CONFIG_HASH"] = digest(config_path.read_bytes())
    env["ALPHA_LOOP_STRATEGY_CONFIG_HASH"] = digest((PROJECT_ROOT / json.loads(config_path.read_text(encoding="utf-8"))["strategy_config"]).read_bytes())
    command = [uv, "run", "--managed-python", "--no-project", "--python", "3.11", "--offline",
               "python", "-m", "alpha_loop.cli", "operate", "--operation-config",
               operation_config]
    if os.environ.get("ALPHA_LOOP_SESSION"):
        command.extend(["--session", os.environ["ALPHA_LOOP_SESSION"]])
    if os.environ.get("ALPHA_LOOP_LIMIT"):
        command.extend(["--limit", os.environ["ALPHA_LOOP_LIMIT"]])
    if os.environ.get("ALPHA_LOOP_NO_AI") == "1":
        command.append("--no-ai")
    record = supervise(PROJECT_ROOT, command, env, total_seconds=PROCESS_TIMEOUT_SECONDS)
    payload = record.get("result")
    # Every invocation produces a receipt, including unchanged input and failure.
    print(json.dumps({"supervision_id": record["supervision_id"], "status": record["status"],
                      "trials": len(record["trials"]), "result": payload,
                      "receipt": str(PROJECT_ROOT / "data/operations/supervision" / record["supervision_id"] / "receipt.json")}, ensure_ascii=False))
    if record["status"] == "SKIPPED_ALREADY_RUNNING":
        return 0
    if payload is None or record["status"] not in ("SUCCEEDED", "SUCCEEDED_WITH_GAPS", "SUCCEEDED_WITH_AI_FAILURE", "SUCCEEDED_WITH_M5_FAILURE", "SUCCEEDED_WITH_SIDE_FAILURE"):
        print(record.get("error", record["status"]), file=sys.stderr)
        return 2
    if payload["qwen_status"] == "FAILED":
        print("Local Qwen failed; numerical outputs saved. " + payload.get("qwen_error", ""), file=sys.stderr)
        return 2
    if payload.get("m5", {}).get("status") == "FAILED":
        print("M5 report failed; numerical outputs saved. " + json.dumps(payload["m5"], ensure_ascii=False), file=sys.stderr)
        return 2
    if payload.get("side_failures"):
        print("Optional side outputs failed; numerical outputs saved: " + ", ".join(payload["side_failures"]), file=sys.stderr)
        return 2
    # The local export contains market results and remains ignored by Git.
    # Remote publication requires a separate local policy; it is disabled by default.
    try:
        from alpha_loop.dashboard import after_daily
        from alpha_loop.common import write_json
        dashboard = after_daily(PROJECT_ROOT)
        write_json(PROJECT_ROOT / "data/operations/dashboard/latest.json", dashboard)
    except Exception as error:
        from alpha_loop.common import now_iso, write_json
        write_json(PROJECT_ROOT / "data/operations/dashboard/failure.json",
                   {"status": "FAILED", "error_type": type(error).__name__, "at": now_iso()})
        print("Dashboard export/publication failed; numerical outputs are preserved.", file=sys.stderr)
        return 2
    return 0


def entrypoint() -> int:
    try:
        return main()
    except Exception as error:
        import uuid
        from datetime import datetime, timezone
        record = {"status": "FAILED_STARTUP", "error_type": type(error).__name__, "error": str(error)[:500],
                  "detected_at": datetime.now(timezone.utc).isoformat()}
        try:
            path = PROJECT_ROOT / "data/operations/startup_failures" / (uuid.uuid4().hex + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            record["receipt"] = str(path)
        except OSError:
            pass
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(entrypoint())
