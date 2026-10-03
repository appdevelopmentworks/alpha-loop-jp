from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, time
from pathlib import Path

from .common import JST, canonical, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .evaluation import OUTCOME_FIELDS, evaluate
from .provider import FileProvider, validate_input
from .screening import CANDIDATE_FIELDS, DECISION_FIELDS, screen
from .provenance import archive_code


def config_load(path: Path) -> dict:
    config = read_json(path)
    if config["cloud_enabled"] or config["orders_enabled"]:
        raise ValueError("external AI and orders are outside M0-M2")
    cost = config["roundtrip_cost_bps"]
    if cost is not None and (not isinstance(cost, (int, float)) or cost < 0):
        raise ValueError("invalid roundtrip_cost_bps")
    mode = config.get("turnover_filter_mode", "required" if config.get("turnover_median_min_jpy") is not None else "disabled")
    threshold = config.get("turnover_median_min_jpy")
    if mode not in ("disabled", "required"):
        raise ValueError("invalid turnover_filter_mode")
    if mode == "required" and (isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or threshold < 0):
        raise ValueError("required turnover filter needs a finite nonnegative threshold")
    return config


def _db(root: Path):
    path = root / "data" / "state.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS schema_history(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, run_kind TEXT, target_session TEXT,
      snapshot_id TEXT, strategy_version TEXT, config_hash TEXT, status TEXT, started_at TEXT, completed_at TEXT,
      as_of TEXT, replay_of TEXT);
    CREATE TABLE IF NOT EXISTS hypotheses(hypothesis_id TEXT, version INTEGER, thesis TEXT, required_data TEXT,
      comparison TEXT, period_start TEXT, period_end TEXT, state TEXT, created_at TEXT,
      PRIMARY KEY(hypothesis_id,version));
    CREATE TABLE IF NOT EXISTS hypothesis_events(hypothesis_id TEXT, version INTEGER, at TEXT, action TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS run_attempts(attempt_id TEXT PRIMARY KEY, target_session TEXT, input_ref TEXT,
      started_at TEXT, finished_at TEXT, status TEXT, detail TEXT, run_id TEXT);
    """)
    con.execute("INSERT OR IGNORE INTO schema_history VALUES(1,?)", (now_iso(),))
    con.commit()
    return con


def _usable_disclosures(disclosures: list[dict], as_of: str) -> tuple[list[dict], list[dict]]:
    usable, rejected = [], []
    cutoff = parse_time(as_of)
    for item in disclosures:
        reason = None
        if not item.get("published_at") or not item.get("first_seen_at"):
            reason = "time_unknown"
        elif parse_time(item["published_at"]) > cutoff:
            reason = "published_after_as_of"
        elif parse_time(item["first_seen_at"]) > cutoff:
            reason = "first_seen_after_as_of"
        elif item.get("fetched_at") and parse_time(item["fetched_at"]) > cutoff:
            reason = "fetched_after_as_of"
        if reason:
            rejected.append({**item, "reason": reason})
        else:
            usable.append(item)
    latest = {}
    for item in usable:
        key = item["disclosure_id"]
        if key not in latest or item["first_seen_at"] > latest[key]["first_seen_at"]:
            latest[key] = item
    distinct = []
    event_keys = set()
    for item in sorted(latest.values(), key=lambda x: (x["first_seen_at"], x["disclosure_id"])):
        event_key = (item.get("event_group_id", item["disclosure_id"]), item.get("content_hash"))
        if event_key in event_keys:
            rejected.append({**item, "reason": "duplicate_event_content"})
        else:
            event_keys.add(event_key)
            distinct.append(item)
    for item in usable:
        if latest[item["disclosure_id"]] is not item:
            rejected.append({**item, "reason": "superseded_revision"})
    return distinct, rejected


def _verified_manifest(output: Path) -> dict | None:
    path = output / "run_manifest.json"
    if not path.exists():
        return None
    manifest = read_json(path)
    if manifest["status"] != "SUCCEEDED":
        raise RuntimeError("run manifest is not complete")
    for name, hash_ in manifest["artifacts"].items():
        target = output / name
        if not target.is_file() or digest(target.read_bytes()) != hash_:
            raise RuntimeError(f"run artifact failed verification: {name}")
    return manifest


def _verified_snapshot(root: Path, snapshot_id: str) -> dict:
    snapshot = read_json(root / "data" / "snapshots" / (snapshot_id + ".json"))
    body = {key: value for key, value in snapshot.items() if key != "snapshot_id"}
    if snapshot.get("snapshot_id") != snapshot_id or digest(canonical(body)) != snapshot_id:
        raise RuntimeError("decision snapshot hash mismatch")
    for obj in snapshot["raw_objects"] + snapshot["data"]["source"].get("original_objects", []):
        if digest((root / obj["stored_at"]).read_bytes()) != obj["content_hash"]:
            raise RuntimeError("decision raw object hash mismatch")
    return snapshot


def _seal(root: Path, data: dict, disclosures: list[dict], raws: dict[str, bytes], session: str, as_of: str,
          raw_fetched: dict[str, str] | None = None) -> dict:
    usable, rejected = _usable_disclosures(disclosures, as_of)
    refs = []
    for name, raw in sorted(raws.items()):
        hash_ = digest(raw)
        target = root / "data" / "raw" / hash_
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        refs.append({"name": name, "content_hash": hash_, "provider": "file", "source_ref": name,
                     "fetched_at": (raw_fetched or {}).get(name, data["source"]["fetched_at"]),
                     "media_type": "text/plain" if name.endswith(".txt") else "application/json", "stored_at": f"data/raw/{hash_}"})
    body = {"target_session": session, "as_of": as_of, "data_grade": data["source"]["data_grade"],
            "data": data, "usable_disclosures": usable, "rejected_disclosures": rejected,
            "raw_objects": refs}
    snapshot_id = digest(canonical(body))
    snapshot = {"snapshot_id": snapshot_id, **body}
    path = root / "data" / "snapshots" / f"{snapshot_id}.json"
    if not path.exists():
        write_json(path, snapshot)
    return snapshot


def _run_id(key: str) -> str:
    return "run-" + key[:20]


def _code_hash() -> str:
    source = Path(__file__).parent
    return digest(b"".join((source / name).read_bytes() for name in
                           ("common.py", "provider.py", "screening.py", "evaluation.py", "pipeline.py")))


def run(root: Path, input_dir: Path, config_path: Path, session: str, as_of: str, replay_of: str | None = None) -> dict:
    attempt_id = uuid.uuid4().hex
    con = _db(root)
    con.execute("INSERT INTO run_attempts VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, session, str(input_dir), now_iso(), None, "COLLECTING", None, None))
    con.commit()
    try:
        config = config_load(config_path)
        provider = FileProvider(input_dir)
        data, raws = provider.load()
        disclosures, more = provider.load_disclosures()
        raws.update(more)
        if data["source"].get("data_grade") == "observed":
            as_of = now_iso()
        raw_fetched = {}
        manual_dir = root / "data" / "manual_disclosures"
        if manual_dir.exists():
            for path in sorted(manual_dir.glob("*.json")):
                document = read_json(path)
                if document["data_grade"] != data["source"]["data_grade"] or document["published_at"][:10] != session:
                    continue
                if parse_time(document["first_seen_at"]) > parse_time(as_of):
                    continue
                existing = next((item for item in disclosures if
                                 item["disclosure_id"] == document["disclosure_id"] and
                                 item["revision_id"] == document["revision_id"]), None)
                if existing is not None:
                    if existing.get("content_hash") != document["content_hash"] and data["source"]["data_grade"] != "synthetic":
                        raise ValueError("manual disclosure conflicts with provider revision")
                    continue
                disclosures.append({key: document[key] for key in
                                    ("disclosure_id", "revision_id", "instrument_id", "published_at", "first_seen_at", "fetched_at", "content_hash")})
                for name, raw in ((f"manual/{document['document_id']}.json", path.read_bytes()),
                                  (f"manual/{document['document_id']}.txt", (root / document["raw_ref"]).read_bytes())):
                    raws[name] = raw
                    raw_fetched[name] = document["fetched_at"]
        validate_input(data, disclosures, session, as_of)
        snapshot = _seal(root, data, disclosures, raws, session, as_of, raw_fetched)
        result = run_snapshot(root, snapshot, config, replay_of)
        con.execute("UPDATE run_attempts SET finished_at=?,status=?,run_id=? WHERE attempt_id=?",
                    (now_iso(), "SUCCEEDED", result["run_id"], attempt_id))
        con.commit()
        return result
    except Exception as error:
        status = "SKIPPED_NON_TRADING_DAY" if str(error) == "SKIPPED_NON_TRADING_DAY" else "BLOCKED_DATA" if isinstance(error, (ValueError, FileNotFoundError)) else "FAILED_RUNTIME"
        con.execute("UPDATE run_attempts SET finished_at=?,status=?,detail=? WHERE attempt_id=?",
                    (now_iso(), status, str(error), attempt_id))
        con.commit()
        raise
    finally:
        con.close()


def run_snapshot(root: Path, snapshot: dict, config: dict, replay_of: str | None = None) -> dict:
    config_hash = digest(canonical(config))
    code_hash = _code_hash()
    kind = "replay" if replay_of else "baseline"
    key = stable_id(kind, snapshot["target_session"], snapshot["snapshot_id"], config["strategy_version"], config_hash, code_hash, replay_of)
    run_id = _run_id(key)
    output = root / "outputs" / run_id
    con = _db(root)
    stage_dir = root / "outputs" / (".staging-" + run_id)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = _verified_manifest(output)
        if existing:
            return existing
        if stage_dir.exists():
            resolved = stage_dir.resolve()
            if resolved.parent != (root / "outputs").resolve() or not resolved.name.startswith(".staging-run-"):
                raise RuntimeError("unsafe staging directory")
            shutil.rmtree(resolved)
        stage_dir.mkdir(parents=True)
        started = now_iso()
        con.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, key, kind, snapshot["target_session"], snapshot["snapshot_id"], config["strategy_version"],
                     config_hash, "COMPUTING", started, None, snapshot["as_of"], replay_of))
        decisions, features = screen(snapshot, config, run_id)
        candidates = [r for r in decisions if r["selected"]]
        candidates.sort(key=lambda r: ({"EARLY": 0, "PREMOVE": 1, "WATCH": 2}[r["stage"]], r["rank"]))
        write_csv(stage_dir / "candidates.csv", CANDIDATE_FIELDS, candidates)
        write_csv(stage_dir / "decisions.csv", DECISION_FIELDS, decisions)
        write_json(stage_dir / "decisions.json", decisions)
        write_json(stage_dir / "features.json", features)
        write_json(stage_dir / "disclosure_exclusions.json", snapshot["rejected_disclosures"])
        next_dates = [d for d in snapshot["data"]["calendar"] if d > snapshot["target_session"]]
        completion = now_iso()
        next_open = datetime.combine(datetime.fromisoformat(next_dates[0]).date(), time(9), JST) if next_dates else None
        history_only = next_open is None or parse_time(snapshot["as_of"]) >= next_open or parse_time(completion) >= next_open
        counts = dict(Counter(r["stage"] for r in decisions))
        warnings = ["合成データは実市場での性能検証ではありません。"] if snapshot["data_grade"] == "synthetic" else []
        if history_only:
            warnings.append("翌営業日開始後の完成のため、事前予測成績には算入しません。")
        if any(row["liquidity_filter_status"] == "disabled" for row in decisions):
            warnings.append("売買代金による流動性フィルターは無効です。流動性・約定可能性は未確認です。")
        missing_turnover = sum(row["turnover_status"] in ("missing", "observed_partial") for row in decisions)
        if missing_turnover:
            warnings.append(f"売買代金の20営業日履歴が不足: {missing_turnover}銘柄。中央値は不明として保存します。")
        (stage_dir / "report.md").write_text(
            f"# 実行報告\n\n対象日: {snapshot['target_session']} / 判定時点: {snapshot['as_of']}\n\n"
            f"全銘柄: {len(decisions)} / 候補: {len(candidates)} / 区分: {counts}\n\n"
            + "\n".join(warnings) + "\n", encoding="utf-8")
        manifest = {"schema_version": 1, "run_id": run_id, "run_kind": kind, "status": "SUCCEEDED",
                    "code_bundle_id": archive_code(root),
                    "target_session": snapshot["target_session"], "snapshot_id": snapshot["snapshot_id"],
                    "as_of": snapshot["as_of"], "started_at": started, "completed_at": completion,
                    "strategy_id": config["strategy_id"], "strategy_version": config["strategy_version"],
                    "config_hash": config_hash, "code_hash": code_hash, "config": config, "replay_of": replay_of,
                    "data_grade": snapshot["data_grade"], "history_only": history_only,
                    "universe_count": len(decisions), "candidate_count": len(candidates), "stage_counts": counts,
                    "raw_objects": snapshot["raw_objects"], "warnings": warnings,
                    "artifacts": {p.name: digest(p.read_bytes()) for p in stage_dir.iterdir() if p.is_file()}}
        write_json(stage_dir / "run_manifest.json", manifest)
        os.replace(stage_dir, output)
        con.execute("UPDATE runs SET status=?, completed_at=? WHERE run_id=?", ("SUCCEEDED", completion, run_id))
        con.commit()
        return manifest
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def replay(root: Path, run_id: str) -> dict:
    original = _verified_manifest(root / "outputs" / run_id)
    if original is None:
        raise FileNotFoundError(run_id)
    if original.get("code_hash") != _code_hash():
        raise RuntimeError("original code hash differs; use the original code revision for exact replay")
    snapshot = read_json(root / "data" / "snapshots" / (original["snapshot_id"] + ".json"))
    result = run_snapshot(root, snapshot, original["config"], run_id)
    prior = read_json(root / "outputs" / run_id / "decisions.json")
    current = read_json(root / "outputs" / result["run_id"] / "decisions.json")
    ignore = {"run_id", "decision_id"}
    def normalized(rows):
        return [{k:v for k,v in row.items() if k not in ignore} for row in rows]
    if normalized(prior) != normalized(current):
        raise AssertionError("replay decisions differ")
    result["replay_verified"] = True
    write_json(root / "outputs" / result["run_id"] / "replay_verification.json", {"source_run_id": run_id, "decisions_match": True})
    return result


def evaluate_run(root: Path, run_id: str, future_dir: Path, through: str) -> dict:
    manifest = _verified_manifest(root / "outputs" / run_id)
    if manifest is None:
        raise FileNotFoundError(run_id)
    if manifest.get("code_hash") != _code_hash():
        raise RuntimeError("run code hash differs; evaluate with the original code revision")
    snapshot = _verified_snapshot(root, manifest["snapshot_id"])
    decisions = read_json(root / "outputs" / run_id / "decisions.json")
    future_path = future_dir / "future.json"
    future_raw = future_path.read_bytes()
    future = json.loads(future_raw)
    if future.get("data_grade") != snapshot["data_grade"]:
        raise ValueError("future data_grade differs from decision snapshot")
    if not future["calendar"] == sorted(set(future["calendar"])):
        raise ValueError("invalid future calendar")
    if any(b["session_date"] <= snapshot["target_session"] for b in future["bars"]):
        raise ValueError("future input contains decision-date bars")
    future_keys = [(b["instrument_id"], b["session_date"]) for b in future["bars"]]
    if len(future_keys) != len(set(future_keys)):
        raise ValueError("duplicate future bar")
    config = manifest["config"]
    rows, metrics = evaluate(snapshot, decisions, future, config, through)
    evaluation_id = stable_id(run_id, digest(future_raw), config["evaluation_version"], through)
    destination = root / "outputs" / run_id / "evaluation" / evaluation_id
    if (destination / "evaluation_manifest.json").exists():
        prior = read_json(destination / "evaluation_manifest.json")
        for name, hash_ in prior["artifacts"].items():
            if digest((destination / name).read_bytes()) != hash_:
                raise RuntimeError(f"evaluation artifact failed verification: {name}")
        return {"evaluation_id": evaluation_id, "path": str(destination), "metrics": read_json(destination / "metrics.json")}
    destination.mkdir(parents=True, exist_ok=True)
    write_csv(destination / "outcomes.csv", OUTCOME_FIELDS, rows)
    write_json(destination / "outcomes.json", rows)
    write_json(destination / "metrics.json", metrics)
    write_json(destination / "evaluation_manifest.json", {"evaluation_id": evaluation_id, "source_run_id": run_id,
               "future_hash": digest(future_raw), "through": through, "evaluation_version": config["evaluation_version"],
               "data_grade": snapshot["data_grade"], "history_only": manifest["history_only"],
               "prediction_score_eligible": not manifest["history_only"] and snapshot["data_grade"] in ("observed", "vendor_pit"),
               "artifacts": {name: digest((destination / name).read_bytes()) for name in
                             ("outcomes.csv", "outcomes.json", "metrics.json")}})
    return {"evaluation_id": evaluation_id, "path": str(destination), "metrics": metrics}


def add_hypothesis(root: Path, hypothesis_id: str, thesis: str, required_data: str, comparison: str,
                   period_start: str, period_end: str) -> None:
    if not all((thesis, required_data, comparison, period_start, period_end)) or period_start > period_end:
        raise ValueError("complete hypothesis fields and ordered period required")
    con = _db(root)
    try:
        con.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?)",
                    (hypothesis_id, 1, thesis, required_data, comparison, period_start, period_end, "DRAFT", now_iso()))
        con.execute("INSERT INTO hypothesis_events VALUES(?,?,?,?,?)", (hypothesis_id, 1, now_iso(), "CREATE", "initial hypothesis"))
        con.commit()
    finally:
        con.close()


def list_hypotheses(root: Path) -> list[dict]:
    con = _db(root)
    try:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute("SELECT * FROM hypotheses ORDER BY hypothesis_id,version")]
    finally:
        con.close()
