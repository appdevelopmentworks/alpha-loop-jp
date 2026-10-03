"""Optional Yahoo Finance research adapter; no finance.yahoo.co.jp scraping."""
from __future__ import annotations

import io
import math
import os
import re
import json
import subprocess
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import JST, atomic_bytes, canonical, digest, now_iso, parse_time, read_json, stable_id, write_json
from .provenance import archive_code
from .provider import validate_input
from .processes import run_bounded

VERSION = "yfinance-research-v1"
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
MARKETS = {"スタンダード（内国株式）": "Standard", "グロース（内国株式）": "Growth", "プライム（内国株式）": "Prime"}


class MarketRateLimited(RuntimeError):
    pass


def _fetch_frame(params: dict):
    import pandas as pd
    result = run_bounded([sys.executable, "-m", "alpha_loop._market_fetch", params["ticker"], params["start"], params["end"]], timeout=60)
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        if "YFRateLimitError" in message:
            raise MarketRateLimited(message[-300:])
        raise RuntimeError(message[-300:])
    raw = result.stdout
    frame = pd.read_csv(io.BytesIO(raw), index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True)
    return frame, raw


def sessions(now: datetime, history_days: int = 365) -> list[str]:
    import exchange_calendars as xc
    start, end = now.date() - timedelta(days=history_days + 30), now.date() + timedelta(days=30)
    cal = xc.get_calendar("XTKS", start=start.isoformat(), end=end.isoformat())
    return [day.strftime("%Y-%m-%d") for day in cal.sessions]


def completed_session(calendar: list[str], now: datetime, ready_hour: int = 20) -> str:
    now = now.astimezone(JST)
    candidates = [day for day in calendar if day < now.date().isoformat() or
                  day == now.date().isoformat() and now.hour >= ready_hour]
    if not candidates:
        raise ValueError("completed exchange session unavailable")
    return candidates[-1]


def _raw(root: Path, raw: bytes) -> str:
    hash_ = digest(raw)
    path = root / "data" / "raw" / hash_
    if path.exists() and digest(path.read_bytes()) != hash_:
        raise RuntimeError("market raw hash mismatch")
    if not path.exists():
        atomic_bytes(path, raw)
    return hash_


def jpx_universe(root: Path, markets: list[str], refresh: bool = False) -> dict:
    path = root / "data" / "market" / "jpx_universe.json"
    if path.exists() and not refresh:
        result = read_json(path)
        if digest((root / "data" / "raw" / result["raw_hash"]).read_bytes()) != result["raw_hash"]:
            raise RuntimeError("JPX raw hash mismatch")
        # Refresh monthly; the published list itself can lag new listings.
        refresh = parse_time(result["fetched_at"]).strftime("%Y-%m") != parse_time(now_iso()).strftime("%Y-%m")
    if not path.exists() or refresh:
        import openpyxl
        request = urllib.request.Request(JPX_URL, headers={"User-Agent": "AlphaLoopJP/0.1 personal-research"})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(10_000_000)
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            iterator = book.active.iter_rows(values_only=True)
            headers = list(next(iterator))
            if not {"日付", "コード", "銘柄名", "市場・商品区分"}.issubset(headers):
                raise ValueError("JPX universe columns changed")
            instruments, dates, excluded = [], set(), []
            fetched = now_iso()
            for values in iterator:
                row = dict(zip(headers, values))
                if row["市場・商品区分"] not in MARKETS:
                    continue
                symbol = str(row["コード"]).strip()
                # JPX uses five digits for preferred / bond-type class shares.
                if re.fullmatch(r"[0-9A-Z]{5}", symbol):
                    excluded.append({"code": symbol, "name": str(row["銘柄名"]), "reason": "class_share"})
                    continue
                if not re.fullmatch(r"[0-9A-Z]{4}", symbol):
                    raise ValueError("unexpected JPX security code")
                effective = datetime.strptime(str(row["日付"]), "%Y%m%d").date().isoformat()
                dates.add(effective)
                instruments.append({"instrument_id": "TSE:" + symbol, "symbol": symbol, "raw_code": symbol,
                                    "name": str(row["銘柄名"]), "market": MARKETS[row["市場・商品区分"]],
                                    "security_type": "common", "listing_status": "listed", "effective_date": effective,
                                    "fetched_at": fetched})
            if not instruments or len(dates) != 1:
                raise ValueError("JPX universe has inconsistent effective dates")
            result = {"source_url": JPX_URL, "fetched_at": fetched, "effective_date": dates.pop(),
                      "raw_hash": _raw(root, raw), "instruments": instruments, "excluded": excluded,
                      "coverage": "latest_month_end_list; new listings and historical membership not guaranteed"}
            write_json(path, result)
        finally:
            book.close()
    selected = [i for i in result["instruments"] if i["market"] in markets]
    selected.sort(key=lambda i: (markets.index(i["market"]), i["symbol"]))
    return {**result, "instruments": selected}


def _finite(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


def normalize_history(frame, instrument: dict, session: str, fetched: str) -> list[dict]:
    required = {"Open", "High", "Low", "Close", "Volume", "Stock Splits", "Dividends"}
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError("empty or incompatible yfinance history")
    bars = []
    for stamp, values in frame.iterrows():
        day = (stamp.tz_convert("Asia/Tokyo") if stamp.tzinfo else stamp).date().isoformat()
        if day > session:
            continue
        prices = {field: _finite(values[field.title()]) for field in ("open", "high", "low", "close")}
        volume, split, dividend = (_finite(values[name]) for name in ("Volume", "Stock Splits", "Dividends"))
        capital_gain = _finite(values.get("Capital Gains", 0))
        valid = all(value is not None and value > 0 for value in prices.values()) and volume is not None and volume >= 0
        valid = valid and split is not None and split >= 0 and dividend is not None and capital_gain == 0
        if valid:
            o, h, l, c = (prices[field] for field in ("open", "high", "low", "close"))
            valid = l <= min(o, c) <= max(o, c) <= h
        if not valid:
            prices = {key: None for key in prices}
        bars.append({"instrument_id": instrument["instrument_id"], "session_date": day,
                     **prices, **{"adj_" + key: value for key, value in prices.items()},
                     "volume": volume if valid else 0, "adj_volume": volume if valid else 0,
                     "turnover_jpy": None, "adjustment_basis": "split_only", "bar_status": "ok" if valid else "missing",
                     "stock_split_event": split, "dividend_event": dividend, "fetched_at": fetched,
                     "source_price_basis": "Yahoo native OHLCV with auto_adjust=False; provider split adjustments retained"})
    if len({b["session_date"] for b in bars}) != len(bars):
        raise ValueError("duplicate yfinance date")
    return bars


def _history(root: Path, instrument: dict, session: str, start: str, refresh: bool) -> tuple[list[dict], dict]:
    import yfinance as yf
    params = {"ticker": instrument["symbol"] + ".T", "start": start,
              "end": (date.fromisoformat(session) + timedelta(days=1)).isoformat(),
              "interval": "1d", "auto_adjust": False, "actions": True, "repair": False, "keepna": True}
    key = stable_id(params, VERSION, yf.__version__)
    path = root / "data" / "market" / "histories" / session / (key + ".json")
    if path.exists() and not refresh:
        result = read_json(path)
        if digest((root / "data" / "raw" / result["raw_hash"]).read_bytes()) != result["raw_hash"] or digest(canonical(result["bars"])) != result["bars_hash"]:
            raise RuntimeError("market history cache hash mismatch")
        return result["bars"], {key: value for key, value in result.items() if key != "bars"}
    cooldown = root / "data" / "market" / "cooldown.json"
    if cooldown.exists() and parse_time(read_json(cooldown)["until"]) > parse_time(now_iso()):
        raise MarketRateLimited("provider cooldown active; cached data retained")
    for attempt in range(2):
        try:
            frame, raw = _fetch_frame(params)
            fetched = now_iso()
            bars = normalize_history(frame, instrument, session, fetched)
            if not bars:
                raise ValueError("no history before target session")
            # Preserve the exact library output; this is not the underlying HTTP body.
            result = {"params": params, "yfinance_version": yf.__version__, "fetched_at": fetched,
                      "raw_hash": _raw(root, raw), "raw_kind": "yfinance_returned_dataframe_csv",
                      "bars": bars, "bars_hash": digest(canonical(bars))}
            write_json(path, result)
            return bars, {key: value for key, value in result.items() if key != "bars"}
        except MarketRateLimited:
            write_json(cooldown, {"until": (parse_time(now_iso()) + timedelta(hours=1)).isoformat(), "reason": "provider_rate_limit"})
            raise
        except Exception:
            if attempt:
                raise
            time.sleep(2)


def collect(root: Path, config: dict, session: str | None = None, limit: int | None = None, refresh: bool = False,
            on_progress=None) -> dict:
    now = parse_time(now_iso())
    calendar = sessions(now, config["history_calendar_days"])
    target = session or completed_session(calendar, now, config["session_ready_hour_jst"])
    if target not in calendar or target > completed_session(calendar, now, config["session_ready_hour_jst"]):
        raise ValueError("target session is not completed")
    universe = jpx_universe(root, config["markets"], refresh)
    instruments = universe["instruments"][:limit] if limit else universe["instruments"]
    if not instruments or any(i["effective_date"] > target for i in instruments):
        raise ValueError("universe snapshot unavailable for target session")
    start = (date.fromisoformat(target) - timedelta(days=config["history_calendar_days"])).isoformat()
    request_id = stable_id(target, start, config["markets"], [i["symbol"] for i in instruments], VERSION)
    progress_path = root / "data" / "market" / "progress" / (request_id + ".json")
    previous_progress = read_json(progress_path) if progress_path.exists() else None
    progress = {"request_id": request_id, "target_session": target,
                "started_at": previous_progress["started_at"] if previous_progress else now_iso(),
                "resumed_at": now_iso() if previous_progress else None, "updated_at": now_iso(), "expected": len(instruments),
                "completed": 0, "status": "COLLECTING", "failures": []}
    write_json(progress_path, progress)
    if on_progress:
        on_progress(progress)
    bars, originals, failures = [], [], []
    last_progress = time.monotonic()
    with ThreadPoolExecutor(max_workers=config["download_workers"]) as pool:
        futures = {pool.submit(_history, root, instrument, target, start, refresh): instrument for instrument in instruments}
        for future in as_completed(futures):
            instrument = futures[future]
            try:
                history, provenance = future.result()
                bars.extend(history)
                originals.append(provenance)
            except Exception as error:
                failures.append({"instrument_id": instrument["instrument_id"], "error": str(error)[:300]})
            progress.update(completed=progress["completed"] + 1, failures=failures, updated_at=now_iso())
            if time.monotonic() - last_progress >= 1 or progress["completed"] == progress["expected"]:
                write_json(progress_path, progress)
                last_progress = time.monotonic()
    by_key = {(b["instrument_id"], b["session_date"]): b for b in bars}
    originals.sort(key=lambda obj: obj["params"]["ticker"])
    fetched = max([universe["fetched_at"]] + [obj["fetched_at"] for obj in originals])
    missing = []
    for item in instruments:
        if (item["instrument_id"], target) not in by_key:
            missing.append(item["instrument_id"])
            bars.append({"instrument_id": item["instrument_id"], "session_date": target, "bar_status": "missing", "fetched_at": fetched})
    unavailable = len([b for b in bars if b["session_date"] == target and b["bar_status"] != "ok"])
    if unavailable == len(instruments):
        progress.update(status="FAILED", reason="all target prices unavailable")
        write_json(progress_path, progress)
        raise RuntimeError("BLOCKED_DATA: all target prices unavailable")
    data = {"calendar": calendar, "instruments": instruments, "bars": sorted(bars, key=lambda b: (b["instrument_id"], b["session_date"])),
            "source": {"data_grade": "reconstructed", "provider_id": "yfinance", "source_url": "https://finance.yahoo.com/", "fetched_at": fetched, "available_at": fetched,
                       "universe_coverage": "partial", "universe_basis": config["universe_basis"], "universe_effective_date": universe["effective_date"],
                       "purpose": config["purpose"], "native_adjustments_preserved": True, "turnover_available": False,
                       "raw_objects": originals, "universe_raw_hash": universe["raw_hash"], "missing_target_count": unavailable},
            "disclosures": []}
    data["source"]["original_objects"] = [{"name": obj["params"]["ticker"] + ".csv", "content_hash": obj["raw_hash"],
                                            "stored_at": "data/raw/" + obj["raw_hash"]} for obj in originals]
    data["source"]["original_objects"].append({"name": "jpx_universe.xlsx", "content_hash": universe["raw_hash"],
                                              "stored_at": "data/raw/" + universe["raw_hash"]})
    validate_input(data, [], target, fetched)
    dataset_id = "market-" + stable_id(data, VERSION)
    directory = root / "data" / "market" / "datasets" / dataset_id
    if not (directory / "manifest.json").exists():
        stage = directory.with_name(".staging-" + uuid.uuid4().hex)
        stage.mkdir(parents=True)
        for name, value in data.items():
            write_json(stage / (name + ".json"), value)
        manifest = {"dataset_id": dataset_id, "input_dir": str(directory), "session": target, "grade": "reconstructed",
                    "expected": len(instruments), "missing_target_count": unavailable, "request_id": request_id,
                    "code_bundle_id": archive_code(root), "failures": failures,
                    "artifacts": {p.name: digest(p.read_bytes()) for p in stage.iterdir()}}
        write_json(stage / "manifest.json", manifest)
        os.replace(stage, directory)
    else:
        manifest = read_json(directory / "manifest.json")
        for name, hash_ in manifest["artifacts"].items():
            if digest((directory / name).read_bytes()) != hash_:
                raise RuntimeError("market dataset artifact hash mismatch")
    progress.update(status="SUCCEEDED_WITH_GAPS" if unavailable else "SUCCEEDED", completed_at=now_iso(), dataset_id=dataset_id)
    write_json(progress_path, progress)
    return manifest
