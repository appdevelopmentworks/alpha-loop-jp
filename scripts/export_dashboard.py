"""Generate a static dashboard using uv-managed Python, without extra packages."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from alpha_loop.common import read_json, write_json
from alpha_loop.dashboard import build, enforce_publication, export_site


def demo(root: Path, count: int = 22) -> dict:
    from alpha_loop.fixture import create
    from alpha_loop.pipeline import run, evaluate_run
    dates = []
    cursor = date(2026, 10, 2)
    while len(dates) < count:
        # This is a plumbing fixture, not a real Japanese exchange calendar.
        if cursor.weekday() < 5 and cursor.isoformat() not in ("2026-09-21", "2026-09-22", "2026-09-23"):
            dates.append(cursor)
        cursor -= timedelta(days=1)
    dates.reverse()
    ids = []
    for index, target in enumerate(dates):
        directory = root / "input" / str(index)
        fixture = create(directory, without_turnover=True)
        shift = (target - date.fromisoformat(fixture["target_session"])).days
        def shifted(value):
            return (date.fromisoformat(value[:10])+timedelta(days=shift)).isoformat()+value[10:]
        for path in directory.glob("*.json"):
            def change(value):
                if isinstance(value, dict): return {k:change(v) for k,v in value.items()}
                if isinstance(value, list): return [change(v) for v in value]
                if isinstance(value, str) and len(value)>=10:
                    try: date.fromisoformat(value[:10])
                    except ValueError: return value
                    return shifted(value)
                return value
            write_json(path, change(read_json(path)))
        next_session = shifted(fixture["next_session"])
        # Seal the next business session in the fixture so weekends display correctly.
        cursor = target + timedelta(days=1)
        while cursor.weekday() >= 5 or cursor.isoformat() in ("2026-09-21", "2026-09-22", "2026-09-23"):
            cursor += timedelta(days=1)
        next_session = cursor.isoformat()
        calendar = read_json(directory / "calendar.json")
        write_json(directory / "calendar.json", sorted(set(calendar + [next_session])))
        future = read_json(directory / "future.json")
        future["calendar"] = sorted(set(calendar + [next_session]))
        for row in future["bars"]:
            row["session_date"] = next_session
            if row["instrument_id"] in ("TSE:0001", "TSE:0002"):
                row["adj_high"] = 112 if index % 4 in (0, 1) else 104
            if index % 6 == 0 and row["instrument_id"] == "TSE:0003":
                row["bar_status"] = "missing"
        write_json(directory / "future.json", future)
        config = read_json(PROJECT / "configs/baseline.json")
        if index == 4:
            config["max_candidates_per_stage"] = 0
        config_path = root / "config.json"
        write_json(config_path, config)
        as_of = target.isoformat()+"T20:00:00+09:00"
        with patch("alpha_loop.pipeline.now_iso", return_value=as_of):
            manifest = run(root, directory, config_path, target.isoformat(), as_of)
        ids.append(manifest["run_id"])
        if index < count-1:
            with patch("alpha_loop.evaluation.now_iso", return_value=next_session+"T20:00:00+09:00"):
                evaluate_run(root, manifest["run_id"], directory, next_session)
    return build(root, run_ids=ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Use an isolated synthetic fixture, not daily market data")
    parser.add_argument("--output", type=Path, help="Static site directory (default: ignored outputs/dashboard/local or demo)")
    parser.add_argument("--public", action="store_true", help="Enforce publication rights policy before writing")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    destination = args.output or PROJECT / "outputs/dashboard" / ("demo" if args.demo else "local")
    destination = destination.resolve()
    # Real data must not accidentally be written into a tracked directory.
    if not args.demo and not args.public and not destination.is_relative_to((PROJECT / "outputs").resolve()):
        raise ValueError("private dashboard must remain under ignored outputs/")
    root = PROJECT / "data/dd" / uuid.uuid4().hex[:8] if args.demo else PROJECT
    document = demo(root) if args.demo else build(root, days=args.days)
    if args.public:
        local = PROJECT / "configs/dashboard_publication.local.json"
        policy = read_json(local if local.exists() else PROJECT / "configs/dashboard_publication.example.json")
        enforce_publication(document, policy)
    result = export_site(document, destination, PROJECT / "dashboard")
    print(json.dumps({**result, "synthetic": args.demo}, ensure_ascii=False))


if __name__ == "__main__":
    main()
