"""Small complete multi-session M5 demo. All observations are synthetic."""
from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from .common import now_iso, write_json
from .pipeline import config_load, evaluate_run, run
from .research import DEFAULT_CRITERIA


def create_research_fixture(root: Path, baseline_path: Path) -> dict:
    calendar, day = [], date(2026, 5, 1)
    while day <= date(2026, 9, 30):
        if day.weekday() < 5 and day.isoformat() not in ("2026-09-21", "2026-09-22", "2026-09-23"):
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    periods = {"discovery": {"start": "2026-08-24", "end": "2026-08-24"},
               "tuning": {"start": "2026-09-03", "end": "2026-09-03"},
               "holdout": {"start": "2026-09-14", "end": "2026-09-18"}}
    criteria = copy.deepcopy(DEFAULT_CRITERIA)
    criteria.update(min_days=5, min_valid=10, min_unique_instruments=5, min_event_clusters=5,
                    bootstrap_samples=400, max_trials=2, confidence=.90)
    config = config_load(baseline_path)
    config["roundtrip_cost_bps"] = None
    write_json(root / "baseline.json", config)
    write_json(root / "calendar.json", calendar)
    write_json(root / "plan.json", {"schema_version": 1, "hypothesis_id": "H-M5-SYNTHETIC", "version": 1,
               "family_id": "M5-SYNTHETIC-FAMILY", "mode": "synthetic", "periods": periods,
               "baseline_config_path": "baseline.json", "calendar_path": "calendar.json",
               "condition_ids": ["ret5_le_0_05", "volume_ge_2"], "reason_code": "volume_expansion", "criteria": criteria})
    entries = []
    targets = [periods["discovery"]["start"], periods["tuning"]["start"]] + [d for d in calendar if periods["holdout"]["start"] <= d <= periods["holdout"]["end"]]
    for target in targets:
        directory = root / "input" / target
        fetched = target + "T19:30:00+09:00"
        instruments = [{"instrument_id": f"TSE:{i:04d}", "symbol": f"{i:04d}", "raw_code": f"{i:04d}",
                        "name": f"M5合成{i}", "market": "Standard" if i <= 10 else "Growth",
                        "security_type": "common", "listing_status": "listed", "effective_date": calendar[0], "fetched_at": fetched}
                       for i in range(1, 23)]
        bars = []
        for instrument in instruments:
            index = int(instrument["symbol"])
            for session in [d for d in calendar if d <= target]:
                close, volume = 100., 1000000
                if session == target:
                    if index <= 5:
                        close, volume = 102., 3000000
                    elif index <= 10:
                        close, volume = 108., 2500000
                    elif index <= 15:
                        close, volume = 99., 2000000
                    elif index <= 20:
                        close, volume = 101., 1500000
                missing = index == 22 and session == target
                bars.append({"instrument_id": instrument["instrument_id"], "session_date": session,
                             "bar_status": "missing" if missing else "ok", "open": None if missing else close,
                             "high": None if missing else max(101., close), "low": None if missing else min(98., close),
                             "close": None if missing else close, "volume": volume, "turnover_jpy": None,
                             "adj_open": None if missing else close, "adj_high": None if missing else max(101., close),
                             "adj_low": None if missing else min(98., close), "adj_close": None if missing else close,
                             "adj_volume": volume, "adjustment_basis": "split_only", "fetched_at": fetched})
        for name, value in (("calendar", calendar), ("instruments", instruments), ("bars", bars),
                            ("source", {"data_grade": "synthetic", "fetched_at": fetched, "available_at": fetched}), ("disclosures", [])):
            write_json(directory / (name + ".json"), value)
        following = calendar[calendar.index(target) + 1]
        future = []
        for instrument in instruments:
            index = int(instrument["symbol"])
            hit = index <= 5 or 11 <= index <= 15
            base = next(b["adj_close"] for b in bars if b["instrument_id"] == instrument["instrument_id"] and b["session_date"] == target) or 100.
            future.append({"instrument_id": instrument["instrument_id"], "session_date": following, "bar_status": "ok",
                           "adj_open": base, "adj_high": base * (1.12 if hit else 1.04), "adj_low": base * .98,
                           "adj_close": base * 1.01, "adjustment_basis": "split_only", "prior_close_rebase_factor": 1.,
                           "execution_status": "unfilled" if index == 1 else "unknown" if index == 2 else "proxy_only"})
        write_json(directory / "future.json", {"calendar": calendar, "bars": future, "data_grade": "synthetic"})
        # Only this synthetic builder simulates completion before the next opening.
        with patch("alpha_loop.pipeline.now_iso", return_value=target + "T20:00:00+09:00"):
            manifest = run(root, directory, root / "baseline.json", target, target + "T20:00:00+09:00")
        evaluated = evaluate_run(root, manifest["run_id"], directory, following)
        entries.append({"run_id": manifest["run_id"], "evaluation_id": evaluated["evaluation_id"],
                        "future_ref": str((directory / "future.json").relative_to(root))})
    write_json(root / "batch.json", {"schema_version": 1, "entries": entries})
    return {"data_grade": "synthetic", "plan_path": str(root / "plan.json"), "batch_path": str(root / "batch.json"),
            "days": len(targets), "created_at": now_iso(), "performance_claim_allowed": False}
