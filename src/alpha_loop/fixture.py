"""Synthetic observations for plumbing tests; never market evidence."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from .common import write_json


def create(directory: Path, without_turnover: bool = False) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    sessions = []
    day = date(2026, 6, 25)
    while len(sessions) < 62:
        if day.weekday() < 5 and day != date(2026, 9, 21):
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    target, next_day = sessions[-2:]
    history = sessions[:-1]
    as_of = target + "T20:00:00+09:00"
    fetched = target + "T19:30:00+09:00"
    instruments = [
        ("TSE:0001", "0001", "合成未上昇", "common"),
        ("TSE:0002", "0002", "合成初動", "common"),
        ("TSE:0003", "0003", "合成材料監視", "common"),
        ("TSE:0004", "0004", "合成過熱", "common"),
        ("TSE:0005", "0005", "合成停止", "common"),
        ("TSE:0006", "0006", "合成ETF", "etf"),
    ]
    instrument_rows = [{"instrument_id": iid, "symbol": code, "raw_code": code + "0", "name": name,
                        "market": "Prime", "security_type": typ, "listing_status": "listed",
                        "effective_date": history[0], "fetched_at": fetched} for iid, code, name, typ in instruments]
    bars = []
    for iid, _, _, _ in instruments:
        for index, session in enumerate(history):
            close = 100.0
            volume = 1000000
            if index == len(history)-1:
                if iid == "TSE:0001":
                    close, volume = 99.0, 2000000
                elif iid == "TSE:0002":
                    close, volume = 102.0, 2500000
                elif iid == "TSE:0004":
                    close, volume = 130.0, 3000000
            status = "halted" if iid == "TSE:0005" and index == len(history)-1 else "ok"
            bar = {"instrument_id": iid, "session_date": session, "bar_status": status,
                   "open": close, "high": max(101.0, close), "low": min(98.0, close), "close": close,
                   "volume": volume, "turnover_jpy": close*volume*2,
                   "adj_open": close, "adj_high": max(101.0, close), "adj_low": min(98.0, close),
                   "adj_close": close, "adj_volume": volume, "adjustment_basis": "split_only", "fetched_at": fetched}
            if status != "ok":
                for key in ("open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close"):
                    bar[key] = None
                bar["volume"] = bar["adj_volume"] = bar["turnover_jpy"] = 0
            bars.append(bar)
    disclosures = [{"disclosure_id": "event-watch", "revision_id": "1", "instrument_id": "TSE:0003",
                    "published_at": target + "T16:00:00+09:00", "first_seen_at": target + "T16:02:00+09:00",
                    "fetched_at": fetched, "content_hash": "synthetic-watch", "event_group_id": "group-watch"},
                   {"disclosure_id": "event-late", "revision_id": "1", "instrument_id": "TSE:0001",
                    "published_at": target + "T21:00:00+09:00", "first_seen_at": target + "T21:01:00+09:00",
                    "fetched_at": target + "T21:02:00+09:00", "content_hash": "synthetic-late", "event_group_id": "group-late"}]
    write_json(directory / "calendar.json", history)
    write_json(directory / "instruments.json", instrument_rows)
    if without_turnover:
        for bar in bars:
            bar["turnover_jpy"] = None
    write_json(directory / "bars.json", bars)
    write_json(directory / "source.json", {"data_grade": "synthetic", "fetched_at": fetched, "available_at": fetched})
    write_json(directory / "disclosures.json", disclosures)
    future_bars = []
    for iid, _, _, _ in instruments:
        o, h, l, c, execution = 100.0, 104.0, 98.0, 101.0, "proxy_only"
        if iid == "TSE:0001":
            o, h, l, c = 102.0, 112.0, 99.0, 105.0
        elif iid == "TSE:0002":
            o, h, l, c, execution = 110.0, 115.0, 106.0, 111.0, "limit_up"
        elif iid == "TSE:0005":
            execution = "halted"
        future_bars.append({"instrument_id": iid, "session_date": next_day,
                            "bar_status": "halted" if execution == "halted" else "ok",
                            "adj_open": o, "adj_high": h, "adj_low": l, "adj_close": c,
                            "adjustment_basis": "split_only", "prior_close_rebase_factor": 1.0,
                            "execution_status": execution})
    write_json(directory / "future.json", {"calendar": sessions, "bars": future_bars, "data_grade": "synthetic"})
    return {"target_session": target, "next_session": next_day, "as_of": as_of, "input_dir": str(directory)}
