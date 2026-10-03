"""Local, explicitly mapped price CSV imports and rankings derived from those prices."""
from __future__ import annotations

import csv
import io
import math
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from .common import JST, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .pipeline import _db
from .provider import FileProvider, validate_input
from .retrospective import RANKING_FIELDS, _previous_session
from .provenance import archive_code, verify_code

VERSION = "local-price-csv-v2"


def _local(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("CSV reference escapes import directory")
    return path


def _number(value: str, field: str) -> float:
    number = float(value.strip().replace(",", ""))
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid finite nonnegative {field}")
    return number


def _date(value: str) -> str:
    parts = value.strip().replace("/", "-").split("-")
    return date(*(int(p) for p in parts)).isoformat()


def _store(root: Path, name: str, raw: bytes) -> dict:
    hash_ = digest(raw)
    path = root / "data" / "raw" / hash_
    if path.exists() and digest(path.read_bytes()) != hash_:
        raise RuntimeError("import raw hash mismatch")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return {"name": name, "content_hash": hash_, "stored_at": f"data/raw/{hash_}"}


def _import(root: Path, spec_path: Path, session: str) -> dict:
    spec = read_json(spec_path)
    if spec["schema_version"] != 1 or spec["data_grade"] not in ("synthetic", "reconstructed"):
        raise ValueError("CSV imports must be synthetic or reconstructed")
    if spec["acquisition_method"] not in ("synthetic", "official_csv_export", "licensed_export"):
        raise ValueError("unsupported price acquisition method")
    if (spec["data_grade"] == "synthetic") != (spec["acquisition_method"] == "synthetic"):
        raise ValueError("synthetic price acquisition/grade mismatch")
    if not spec.get("usage_basis") or not spec.get("adjustment_basis_id"):
        raise ValueError("usage basis and shared split adjustment basis required")
    if spec["universe_coverage"] not in ("partial", "declared_complete") or not spec["files"]:
        raise ValueError("explicit universe coverage and nonempty CSV list required")
    if spec["corporate_action_scope"] != "split_and_consolidation_only":
        raise ValueError("rights issues or unknown corporate actions need a separate adjustment adapter")
    fetched, available = parse_time(spec["fetched_at"]), parse_time(spec["available_at"])
    if spec["data_grade"] != "synthetic" and max(fetched, available) > parse_time(now_iso()):
        raise ValueError("price import timestamps are in the future")
    url = urlparse(spec["source_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("public HTTPS source provenance required")
    base = spec_path.parent
    raws = {"import_spec.json": spec_path.read_bytes()}
    data = {}
    for kind in ("calendar", "instruments"):
        path = _local(base, spec[kind + "_file"])
        data[kind] = read_json(path)
        raws[kind + ".json"] = path.read_bytes()
    if session not in data["calendar"]:
        raise ValueError("import session is not in the explicit calendar")
    instruments = {item["instrument_id"]: item for item in data["instruments"]}
    bars = []
    for item in spec["files"]:
        iid = item["instrument_id"]
        if iid not in instruments:
            raise ValueError("CSV instrument absent from explicit universe")
        path = _local(base, item["file"])
        raw = path.read_bytes()
        raws[item["file"]] = raw
        mapping = item["columns"]
        required = {"session_date", "open", "high", "low", "close", "volume", "split_factor"}
        if not required.issubset(mapping):
            raise ValueError("CSV mapping requires OHLCV and explicit split_factor; do not infer from adjusted close")
        reader = csv.DictReader(io.StringIO(raw.decode(item.get("encoding", "utf-8-sig")), newline=""))
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or not set(mapping.values()).issubset(reader.fieldnames):
            raise ValueError("mapped columns missing or duplicated in price CSV")
        for entry in reader:
            if None in entry or any(value is None for value in entry.values()):
                raise ValueError("malformed price CSV row")
            day = _date(entry[mapping["session_date"]])
            if day > session:
                continue
            status = entry[mapping["bar_status"]].strip() if "bar_status" in mapping else "ok"
            row = {"instrument_id": iid, "session_date": day, "bar_status": status,
                   "fetched_at": spec["fetched_at"], "adjustment_basis": "split_only"}
            if status == "ok":
                for field in ("open", "high", "low", "close", "volume"):
                    row[field] = _number(entry[mapping[field]], field)
                turnover = entry[mapping["turnover_jpy"]].strip() if "turnover_jpy" in mapping else ""
                row["turnover_jpy"] = _number(turnover, "turnover_jpy") if turnover else None
                factor = _number(entry[mapping["split_factor"]], "split_factor")
                if factor <= 0:
                    raise ValueError("positive explicit split_factor required")
                for field in ("open", "high", "low", "close"):
                    row["adj_" + field] = row[field] * factor
                row["adj_volume"] = row["volume"] / factor
                row["split_factor"] = factor
                if "adj_close" in mapping and abs(_number(entry[mapping["adj_close"]], "adj_close") - row["adj_close"]) > .010000001:
                    raise ValueError("explicit split factor conflicts with supplied adjusted close")
            elif status in ("halted", "no_trade", "missing"):
                row.update({field: None for field in ("open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close")})
                row.update(volume=0, adj_volume=0, turnover_jpy=None)
            else:
                raise ValueError("unknown bar_status")
            bars.append(row)
    refs = [_store(root, name, raw) for name, raw in sorted(raws.items())]
    data["bars"] = sorted(bars, key=lambda b: (b["instrument_id"], b["session_date"]))
    data["source"] = {"data_grade": spec["data_grade"], "fetched_at": spec["fetched_at"], "available_at": spec["available_at"],
                      "provider_id": spec["provider_id"], "source_url": spec["source_url"], "usage_basis": spec["usage_basis"],
                      "universe_coverage": spec["universe_coverage"], "adjustment_basis_id": spec["adjustment_basis_id"],
                      "original_objects": refs, "import_version": VERSION}
    validate_input(data, [], session, max(fetched, available).isoformat())
    import_id = "import-" + stable_id(refs, spec, session, digest(Path(__file__).read_bytes()))
    output = root / "data" / "imports" / import_id
    con = _db(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        if (output / "import_manifest.json").exists():
            manifest = read_json(output / "import_manifest.json")
            verify_code(root, manifest["code_bundle_id"])
            for name, hash_ in manifest["artifacts"].items():
                if digest((output / name).read_bytes()) != hash_:
                    raise RuntimeError("normalized import hash mismatch")
            return manifest
        stage = output.with_name(".staging-" + uuid.uuid4().hex)
        stage.mkdir(parents=True)
        for name, value in data.items():
            write_json(stage / (name + ".json"), value)
        write_json(stage / "disclosures.json", [])
        manifest = {"import_id": import_id, "status": "SUCCEEDED", "target_session": session,
                    "code_bundle_id": archive_code(root),
                    "data_grade": spec["data_grade"], "input_dir": str(output), "bar_count": len(bars),
                    "universe_count": len(instruments), "original_objects": refs, "version": VERSION,
                    "artifacts": {p.name: digest(p.read_bytes()) for p in stage.iterdir()}}
        write_json(stage / "import_manifest.json", manifest)
        os.replace(stage, output)
        con.commit()
        return manifest
    finally:
        con.close()


def verify_import(root: Path, directory: Path) -> None:
    manifest = read_json(directory / "import_manifest.json")
    verify_code(root, manifest["code_bundle_id"])
    for name, hash_ in manifest["artifacts"].items():
        if digest((directory / name).read_bytes()) != hash_:
            raise RuntimeError("normalized import hash mismatch")
    for obj in manifest["original_objects"]:
        if digest((root / obj["stored_at"]).read_bytes()) != obj["content_hash"]:
            raise RuntimeError("import raw hash mismatch")


def import_csv(root: Path, spec_path: Path, session: str) -> dict:
    attempt = {"attempt_id": uuid.uuid4().hex, "started_at": now_iso(), "spec": str(spec_path), "status": "RUNNING"}
    path = root / "data" / "operations" / "import_attempts" / (attempt["attempt_id"] + ".json")
    write_json(path, attempt)
    try:
        result = _import(root, spec_path, session)
        attempt.update(status="SUCCEEDED", import_id=result["import_id"])
        return result
    except Exception as error:
        attempt.update(status="FAILED", error=str(error), error_type=type(error).__name__)
        raise
    finally:
        attempt["completed_at"] = now_iso()
        write_json(path, attempt)


def _derive_ranking(root: Path, input_dir: Path, session: str, min_return: float = .10, limit: int = 50) -> dict:
    if not math.isfinite(min_return) or min_return < 0 or limit < 1:
        raise ValueError("finite nonnegative minimum return and positive ranking limit required")
    if (input_dir / "import_manifest.json").exists():
        verify_import(root, input_dir)
    data, raws = FileProvider(input_dir).load()
    source = data["source"]
    if source["data_grade"] != "synthetic" and date.fromisoformat(session) > parse_time(now_iso()).astimezone(JST).date():
        raise ValueError("cannot derive a real ranking for a future session")
    validate_input(data, [], session, max(parse_time(source["fetched_at"]), parse_time(source["available_at"])).isoformat())
    previous = _previous_session(data["calendar"], session)
    by_key = {(b["instrument_id"], b["session_date"]): b for b in data["bars"]}
    values, unknown = [], []
    for instrument in data["instruments"]:
        if instrument["security_type"] != "common" or instrument["listing_status"] != "listed":
            continue
        iid = instrument["instrument_id"]
        before, after = (by_key.get((iid, d)) for d in (previous, session))
        if not before or not after or before["bar_status"] != "ok" or after["bar_status"] != "ok":
            unknown.append({"instrument_id": iid, "reason": "prior_or_current_price_unknown"})
            continue
        value = Decimal(str(after["adj_close"])) / Decimal(str(before["adj_close"])) - 1
        if value >= Decimal(str(min_return)):
            values.append({"instrument_id": iid, "symbol": instrument["symbol"], "listed_return": float(value)})
    values.sort(key=lambda r: (-r["listed_return"], r["instrument_id"]))
    key = stable_id({name: digest(raw) for name, raw in raws.items()}, session, min_return, limit, VERSION, digest(Path(__file__).read_bytes()))
    output = root / "data" / "derived_rankings" / ("ranking-" + key)
    if (output / "derivation_manifest.json").exists():
        saved = read_json(output / "derivation_manifest.json")
        for name, hash_ in saved["artifacts"].items():
            if digest((output / name).read_bytes()) != hash_:
                raise RuntimeError("derived ranking hash mismatch")
        return saved
    synthetic = source["data_grade"] == "synthetic"
    captured = session + "T20:00:00+09:00" if synthetic else now_iso()
    rows = [{**row, "rank": rank, "source_url": source.get("source_url", "file:" + str(input_dir.resolve())), "source_updated_at": captured, "page": 1}
            for rank, row in enumerate(values[:limit], 1)]
    write_csv(output / "ranking.csv", RANKING_FIELDS, rows)
    write_json(output / "ranking_source.json", {"schema_version": 1, "ranking_session": session, "fetched_at": captured,
               "data_grade": "synthetic" if synthetic else "reconstructed",
               "acquisition_method": "synthetic" if synthetic else "derived_prices",
               "coverage": "declared_complete" if source.get("universe_coverage") == "declared_complete" and not unknown and len(values) <= limit else "partial",
               "ranking_method": "split_adjusted_close_to_close", "return_min": min_return,
               "source_price_hashes": {name: digest(raw) for name, raw in raws.items()}})
    write_json(output / "unknown_prices.json", unknown)
    result = {"ranking_id": "ranking-" + key, "ranking_input": str(output), "matched_price_count": len(values),
              "ranking_count": len(rows), "unknown_count": len(unknown), "ranking_method": "split_adjusted_close_to_close",
              "artifacts": {name: digest((output / name).read_bytes()) for name in ("ranking.csv", "ranking_source.json", "unknown_prices.json")}}
    write_json(output / "derivation_manifest.json", result)
    return result


def derive_ranking(root: Path, input_dir: Path, session: str, min_return: float = .10, limit: int = 50) -> dict:
    con = _db(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        result = _derive_ranking(root, input_dir, session, min_return, limit)
        con.commit()
        return result
    finally:
        con.close()


def create_csv_fixture(directory: Path, without_turnover: bool = False) -> dict:
    from .fixture import create
    fixture = create(directory)
    future = read_json(directory / "future.json")
    write_json(directory / "calendar.json", future["calendar"])
    fetched = fixture["next_session"] + "T19:30:00+09:00"
    instruments = read_json(directory / "instruments.json")
    for item in instruments:
        item["fetched_at"] = fetched
    write_json(directory / "instruments.json", instruments)
    bars = read_json(directory / "bars.json")
    for bar in future["bars"]:
        prices = {key: bar["adj_" + key] for key in ("open", "high", "low", "close")}
        if bar["instrument_id"] == "TSE:0001":
            prices["close"] = 110.0
        elif bar["instrument_id"] == "TSE:0002":
            prices["close"] = 115.0
        bars.append({**bar, **prices, "volume": 1_000_000, "turnover_jpy": prices["close"] * 1_000_000})
    fields = {"session_date": "日付", "open": "始値", "high": "高値", "low": "安値", "close": "終値",
              "volume": "出来高", "turnover_jpy": "売買代金", "split_factor": "分割調整係数", "bar_status": "状態"}
    if without_turnover:
        fields.pop("turnover_jpy")
    files = []
    for instrument in instruments:
        rows = [{fields[key]: (1 if key == "split_factor" else bar[key]) for key in fields}
                for bar in bars if bar["instrument_id"] == instrument["instrument_id"]]
        name = instrument["symbol"] + ".csv"
        write_csv(directory / name, list(fields.values()), rows)
        files.append({"instrument_id": instrument["instrument_id"], "file": name, "columns": fields})
    spec = {"schema_version": 1, "data_grade": "synthetic", "provider_id": "synthetic_csv",
            "acquisition_method": "synthetic", "usage_basis": "合成テスト用。実サイトのCSVではない。",
            "source_url": "https://example.invalid/synthetic-prices", "adjustment_basis_id": "synthetic-common-basis-v1",
            "corporate_action_scope": "split_and_consolidation_only", "universe_coverage": "partial",
            "fetched_at": fetched, "available_at": fetched,
            "calendar_file": "calendar.json", "instruments_file": "instruments.json", "files": files}
    write_json(directory / "import.json", spec)
    return {**fixture, "spec_path": str(directory / "import.json")}
