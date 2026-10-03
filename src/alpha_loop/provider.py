from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Protocol

from .common import digest, parse_time, read_json


class MarketDataProvider(Protocol):
    def load(self) -> tuple[dict, dict[str, bytes]]: ...


class DisclosureProvider(Protocol):
    def load_disclosures(self) -> tuple[list[dict], dict[str, bytes]]: ...


class FileProvider:
    """Normalized file contract. API adapters can implement the two protocols."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _read(self, name: str):
        raw = (self.directory / name).read_bytes()
        return json.loads(raw), raw

    def load(self) -> tuple[dict, dict[str, bytes]]:
        result = {}
        raws = {}
        for name in ("calendar.json", "instruments.json", "bars.json", "source.json"):
            result[name.removesuffix(".json")], raws[name] = self._read(name)
        return result, raws

    def load_disclosures(self) -> tuple[list[dict], dict[str, bytes]]:
        path = self.directory / "disclosures.json"
        if not path.exists():
            return [], {}
        data, raw = self._read("disclosures.json")
        return data, {"disclosures.json": raw}


def validate_input(data: dict, disclosures: list[dict], session: str, as_of: str) -> None:
    cutoff = parse_time(as_of)
    calendar = data["calendar"]
    if len(calendar) != len(set(calendar)) or calendar != sorted(calendar):
        raise ValueError("calendar must be sorted unique trading sessions")
    if session not in calendar:
        raise RuntimeError("SKIPPED_NON_TRADING_DAY")
    source = data["source"]
    if source["data_grade"] not in ("synthetic", "observed", "vendor_pit", "reconstructed"):
        raise ValueError("invalid data_grade")
    if parse_time(source["fetched_at"]) > cutoff:
        raise ValueError("market data fetched after as_of")
    if source["available_at"] is None or parse_time(source["available_at"]) > cutoff:
        raise ValueError("market data unavailable at as_of")
    instruments = data["instruments"]
    ids = [row["instrument_id"] for row in instruments]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate instrument_id")
    bars = data["bars"]
    keys = [(b["instrument_id"], b["session_date"]) for b in bars]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate daily_bar")
    universe_ids, calendar_days = set(ids), set(calendar)
    if any(key[0] not in universe_ids or key[1] not in calendar_days for key in keys):
        raise ValueError("bar outside universe or calendar")
    bar_keys = set(keys)
    for instrument in instruments:
        if instrument["effective_date"] > session or parse_time(instrument["fetched_at"]) > cutoff:
            raise ValueError("future instrument attribute")
        if (instrument["instrument_id"], session) not in bar_keys:
            raise ValueError("BLOCKED_DATA: missing target bar")
    for bar in bars:
        if bar["session_date"] > session:
            raise ValueError("future bar in screening input")
        if parse_time(bar["fetched_at"]) > cutoff:
            raise ValueError("bar fetched after as_of")
        if bar["bar_status"] == "ok":
            for group in (("open", "high", "low", "close"), ("adj_open", "adj_high", "adj_low", "adj_close")):
                o, h, l, c = (bar[k] for k in group)
                if any(x is None or x <= 0 for x in (o, h, l, c)) or l > min(o, c) or h < max(o, c):
                    raise ValueError("invalid OHLC")
            for field in ("volume", "adj_volume"):
                value = bar[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid finite nonnegative volume")
            turnover = bar.get("turnover_jpy")
            if turnover is not None and (isinstance(turnover, bool) or not isinstance(turnover, (int, float)) or not math.isfinite(turnover) or turnover < 0):
                raise ValueError("invalid finite nonnegative turnover")
            if bar["adjustment_basis"] != "split_only":
                raise ValueError("unsupported adjustment_basis")
        elif bar["bar_status"] not in ("halted", "no_trade", "missing"):
            raise ValueError("invalid bar_status")
    seen = set()
    for item in disclosures:
        key = (item["disclosure_id"], item["revision_id"])
        if key in seen:
            raise ValueError("duplicate disclosure revision")
        seen.add(key)
        for field in ("published_at", "first_seen_at"):
            if item.get(field) is not None:
                parse_time(item[field])
