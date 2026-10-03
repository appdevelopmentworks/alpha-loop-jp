"""Prepare/register the latest saved Qwen hypotheses for future research observation."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from datetime import timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from alpha_loop.common import JST, now_iso, parse_time, read_json, write_json
from alpha_loop.research import draft_from_qwen, refresh, register


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true", help="Freeze the explicit design defaults in the hypothesis ledger")
    args = parser.parse_args()
    import exchange_calendars as xc
    latest = read_json(PROJECT / "data" / "operations" / "latest_service.json")
    report_path = Path(latest["qwen"]["hypotheses"]["report_path"])
    report = read_json(report_path)
    study = read_json(PROJECT / "outputs" / "retrospectives" / report["study_id"] / "study_manifest.json")
    now = parse_time(now_iso()).astimezone(JST)
    calendar = xc.get_calendar("XTKS", start=study["feature_session"], end=(now.date() + timedelta(days=180)).isoformat())
    sessions = [day.strftime("%Y-%m-%d") for day in calendar.sessions]
    directory = PROJECT / "data" / "research" / "plans" / report["study_id"]
    calendar_path = directory / "calendar.json"
    write_json(calendar_path, sessions)
    write_json(directory / "calendar_source.json", {"calendar": "XTKS", "provider": "exchange_calendars",
               "version": importlib.metadata.version("exchange_calendars"), "generated_at": now_iso(),
               "source_report": str(report_path), "future_holidays_frozen_at_registration": True})
    drafted = draft_from_qwen(PROJECT, report_path, calendar_path, directory, PROJECT / "configs" / "baseline.json")
    registrations = []
    if args.register:
        for path in drafted["plan_paths"]:
            spec = register(PROJECT, Path(path))
            registrations.append({"experiment_id": spec["experiment_id"], "hypothesis_id": spec["hypothesis_id"], "registered_at": spec["registered_at"]})
    receipt = {"drafted": drafted, "registrations": registrations, "mode": "research", "automatic_adoption": False}
    if args.register:
        receipt["initial_status"] = refresh(PROJECT)
    write_json(PROJECT / "data" / "operations" / "m5_research_registration.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
