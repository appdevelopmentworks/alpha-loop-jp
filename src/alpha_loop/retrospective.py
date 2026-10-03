"""Ranking-led research from local files; never a prospective prediction score."""
from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import uuid
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import median
from typing import Protocol
from urllib.parse import urlparse

from .common import JST, canonical, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .pipeline import _code_hash, _db, _usable_disclosures, _verified_manifest, config_load
from .provider import FileProvider, validate_input
from .screening import DECISION_FIELDS, screen
from .provenance import archive_code, verify_code

VERSION = "ranking-retrospective-v1"
RANKING_FIELDS = "instrument_id symbol rank listed_return source_url source_updated_at page".split()
FEATURE_FIELDS = "ret_1d ret_5d rel_volume_20d high_distance_20d ma25_gap turnover_median_20d_jpy".split()
STUDY_FIELDS = ["study_id", "ranking_session", "feature_session", "feature_cutoff", "in_ranking",
                "ranking_evidence", "availability_basis", "prediction_score_eligible"] + DECISION_FIELDS


class RankingProvider(Protocol):
    def load(self) -> tuple[dict, list[dict], dict[str, bytes]]: ...


class FileRankingProvider:
    """Manually recorded rankings or synthetic fixtures. No website requests."""

    def __init__(self, directory: Path):
        self.directory = directory

    def load(self) -> tuple[dict, list[dict], dict[str, bytes]]:
        raws = {name: (self.directory / name).read_bytes() for name in ("ranking.csv", "ranking_source.json")}
        import json
        meta = json.loads(raws["ranking_source.json"])
        captured = parse_time(meta["fetched_at"])
        if captured.astimezone(JST).date() < date.fromisoformat(meta["ranking_session"]):
            raise ValueError("ranking fetched before its session")
        if meta["schema_version"] != 1 or meta["acquisition_method"] not in ("manual", "synthetic", "derived_prices"):
            raise ValueError("only manual, synthetic or derived-price ranking input is supported")
        if meta["data_grade"] not in ("observed", "reconstructed", "synthetic"):
            raise ValueError("invalid ranking data_grade")
        if (meta["acquisition_method"] == "synthetic") != (meta["data_grade"] == "synthetic"):
            raise ValueError("synthetic acquisition and grade must agree")
        if meta["coverage"] not in ("partial", "declared_complete"):
            raise ValueError("ranking coverage required")
        if meta["data_grade"] != "synthetic" and captured > parse_time(now_iso()):
            raise ValueError("ranking fetched_at is in the future")
        reader = csv.DictReader(io.StringIO(raws["ranking.csv"].decode("utf-8-sig"), newline=""))
        if reader.fieldnames != RANKING_FIELDS:
            raise ValueError("ranking CSV columns do not match contract")
        rows, seen = [], {}
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("invalid ranking CSV row")
            row = {key: value.strip() for key, value in raw.items()}
            if not row["instrument_id"] or not row["symbol"]:
                raise ValueError("explicit instrument_id and string symbol required")
            row["rank"], row["page"] = int(row["rank"]), int(row["page"])
            if min(row["rank"], row["page"]) < 1:
                raise ValueError("positive ranking rank and page required")
            row["listed_return"] = float(row["listed_return"]) if row["listed_return"] else None
            if row["listed_return"] is not None and not math.isfinite(row["listed_return"]):
                raise ValueError("ranking return must be finite")
            url = urlparse(row["source_url"])
            local_derivation = meta["acquisition_method"] in ("synthetic", "derived_prices") and url.scheme == "file" and bool(url.path)
            if not local_derivation and (url.scheme != "https" or not url.hostname or url.username or url.password):
                raise ValueError("ranking source_url must be public HTTPS provenance")
            updated = parse_time(row["source_updated_at"])
            wrong_day = updated.astimezone(JST).date().isoformat() != meta["ranking_session"]
            if (wrong_day and meta["acquisition_method"] != "derived_prices") or updated > captured:
                raise ValueError("ranking date/time mismatch")
            key = (row["source_url"], row["page"], row["instrument_id"])
            if key in seen and seen[key] != row:
                raise ValueError("conflicting ranking duplicate")
            seen[key] = row
            rows.append(row)
        unique = sorted(seen.values(), key=lambda r: (r["instrument_id"], r["source_url"], r["page"], r["rank"]))
        return {**meta, "duplicate_rows": len(rows) - len(unique)}, unique, raws


def _previous_session(calendar: list[str], ranking_session: str) -> str:
    if any(date.fromisoformat(day).isoformat() != day for day in calendar):
        raise ValueError("ISO session dates required")
    if calendar != sorted(set(calendar)):
        raise ValueError("sorted unique calendar required")
    if ranking_session not in calendar:
        raise ValueError("ranking_session is not a known trading session")
    index = calendar.index(ranking_session)
    if index == 0:
        raise ValueError("previous trading session unavailable")
    return calendar[index - 1]


def _prior(root: Path, meta: dict, input_dir: Path | None, config_path: Path | None,
           prior_run_id: str | None, feature_as_of: str | None) -> tuple[dict, dict, dict[str, bytes], dict]:
    if (input_dir is None) == (prior_run_id is None):
        raise ValueError("choose exactly one prior-run-id or input")
    if prior_run_id:
        if not re.fullmatch(r"run-[a-f0-9]{20}", prior_run_id):
            raise ValueError("invalid prior run id")
        manifest = _verified_manifest(root / "outputs" / prior_run_id)
        if manifest is None:
            raise FileNotFoundError(prior_run_id)
        if manifest["code_hash"] != _code_hash():
            raise ValueError("prior run requires its original numerical code revision")
        path = root / "data" / "snapshots" / (manifest["snapshot_id"] + ".json")
        snapshot = read_json(path)
        if digest(canonical({k: v for k, v in snapshot.items() if k != "snapshot_id"})) != manifest["snapshot_id"]:
            raise RuntimeError("prior snapshot hash mismatch")
        previous = _previous_session(snapshot["data"]["calendar"], meta["ranking_session"])
        if snapshot["target_session"] != previous:
            raise ValueError("prior run must target the previous trading session")
        if feature_as_of and parse_time(feature_as_of) != parse_time(snapshot["as_of"]):
            raise ValueError("saved prior cutoff cannot be changed")
        if parse_time(snapshot["as_of"]).astimezone(JST).date().isoformat() != previous:
            raise ValueError("prior run cutoff must be on the previous trading session")
        for obj in snapshot["raw_objects"]:
            if digest((root / obj["stored_at"]).read_bytes()) != obj["content_hash"]:
                raise RuntimeError("prior raw object hash mismatch")
        return snapshot, manifest["config"], {"prior_snapshot.json": path.read_bytes()}, {
            "prior_run_id": prior_run_id, "availability_basis": "saved_cutoff",
            "collected_at": manifest["completed_at"], "after_cutoff_bars_removed": 0}
    if (input_dir / "import_manifest.json").exists():
        from .csv_input import verify_import
        verify_import(root, input_dir)
    data, raws = FileProvider(input_dir).load()
    disclosures, more = FileProvider(input_dir).load_disclosures()
    raws.update(more)
    previous = _previous_session(data["calendar"], meta["ranking_session"])
    cutoff = feature_as_of or previous + "T20:00:00+09:00"
    if parse_time(cutoff).astimezone(JST).date().isoformat() != previous:
        raise ValueError("feature cutoff must be on the previous trading session")
    if parse_time(meta["fetched_at"]) <= parse_time(cutoff):
        raise ValueError("ranking must be collected after the feature cutoff")
    grade = "synthetic" if data["source"]["data_grade"] == "synthetic" else "reconstructed"
    # Keep true retrieval timestamps; historical reconstruction never backdates them.
    collected = now_iso()
    removed = [b for b in data["bars"] if b["session_date"] > previous]
    data["bars"] = [b for b in data["bars"] if b["session_date"] <= previous]
    unavailable = []
    prior_keys = {(b["instrument_id"], b["session_date"]) for b in data["bars"]}
    for instrument in data["instruments"]:
        if (instrument["instrument_id"], previous) not in prior_keys:
            data["bars"].append({"instrument_id": instrument["instrument_id"], "session_date": previous,
                                 "bar_status": "missing", "fetched_at": data["source"]["fetched_at"]})
            unavailable.append(f"{instrument['instrument_id']}:{previous}")
    for bar in data["bars"]:
        if bar.get("available_at") and parse_time(bar["available_at"]) > parse_time(cutoff):
            bar["bar_status"] = "missing"
            unavailable.append(f"{bar['instrument_id']}:{bar['session_date']}")
    validate_input(data, disclosures, previous, collected)
    if grade == "synthetic":
        usable, rejected = _usable_disclosures(disclosures, cutoff)
    else:
        # Later retrieval is allowed only as explicitly limited publication-based reconstruction.
        known, rejected = [], []
        for item in disclosures:
            if not item.get("published_at") or parse_time(item["published_at"]) > parse_time(cutoff):
                rejected.append({**item, "reason": "publication_unknown_or_after_feature_cutoff"})
            else:
                known.append(item)
        usable, duplicates = _usable_disclosures(known, collected)
        rejected.extend(duplicates)
    snapshot = {"target_session": previous, "as_of": cutoff, "data_grade": grade,
                "data": data, "usable_disclosures": usable, "rejected_disclosures": rejected}
    return snapshot, config_load(config_path), raws, {
        "prior_run_id": None, "availability_basis": "synthetic_cutoff" if grade == "synthetic" else "published_only_reconstruction",
        "source_data_grade": data["source"]["data_grade"], "collected_at": collected,
        "after_cutoff_bars_removed": len(removed), "unavailable_feature_bars": unavailable}


def _analysis(snapshot: dict, ranking: list[dict], config: dict, study_id: str, meta: dict, provenance: dict):
    decisions, features = screen(snapshot, config, study_id)
    by_id = {r["instrument_id"]: r for r in decisions}
    evidence, unmatched = {}, []
    for entry in ranking:
        prior = by_id.get(entry["instrument_id"])
        if prior is None:
            unmatched.append({**entry, "reason": "absent_from_prior_universe"})
        elif entry["symbol"] != prior["symbol"]:
            raise ValueError("ranking symbol conflicts with prior instrument_id")
        else:
            evidence.setdefault(entry["instrument_id"], []).append(entry)
    rows = [{"study_id": study_id, "ranking_session": meta["ranking_session"],
             "feature_session": snapshot["target_session"], "feature_cutoff": snapshot["as_of"],
             "in_ranking": row["instrument_id"] in evidence,
             "ranking_evidence": evidence.get(row["instrument_id"], []),
             "availability_basis": provenance["availability_basis"], "prediction_score_eligible": False,
             **row} for row in decisions]
    groups = {}
    for label, listed in (("listed", True), ("not_listed", False)):
        group = [r for r in rows if r["in_ranking"] == listed]
        valid = [r for r in group if r["quality_status"] == "ok" and r["stage"] != "INELIGIBLE"]
        groups[label] = {"count": len(group), "feature_valid_eligible_count": len(valid),
                         "selected_count": sum(r["selected"] for r in group),
                         "stages": dict(Counter(r["stage"] for r in group)),
                         "feature_medians": {key: median(values) if (values := [r[key] for r in valid if r[key] is not None]) else None for key in FEATURE_FIELDS},
                         "feature_observed_counts": {key: sum(r[key] is not None for r in valid) for key in FEATURE_FIELDS}}
    comparison = {"comparison_kind": "ranking_membership_descriptive_only", "prediction_score_eligible": False,
                  "ranking_coverage": meta["coverage"], "ranking_count": len({r["instrument_id"] for r in ranking}),
                  "matched_count": len(evidence), "unmatched_count": len({r["instrument_id"] for r in unmatched}),
                  "groups": groups, "limitations": ["非掲載を非急騰と扱いません。ランキングだけから精度・勝率を算出しません。",
                  "後から急騰銘柄を選んだ事例研究です。因果関係や前日の予測成功を示しません。",
                  "売買代金は任意です。20営業日分が揃わない中央値は不明。既定では流動性の足切り・順位付けをしません。"]}
    drafts = []
    if groups["listed"]["feature_valid_eligible_count"]:
        drafts.append({"hypothesis_id": "H-RANK-" + study_id.removeprefix("study-"), "state": "DRAFT",
                       "data_grade": snapshot["data_grade"],
                       "thesis": "前営業日の出来高増加と20日高値への接近は翌営業日の急騰捕捉に役立つか。",
                       "required_data": "当時の全母集団、分割調整日足、開示時点、翌日高値と始値・終値",
                       "comparison": "価格基準Aと、新しい版として固定した条件を同じ未使用期間で比較する。",
                       "discovery_through": meta["ranking_session"], "period_start": None, "period_end": None,
                       "basis_study_id": study_id, "automatic_adoption": False,
                       "note": "Pythonの定型下書き。学習・検証の独立件数と採否基準は未登録。"})
    return rows, features, unmatched, comparison, drafts


def _verify(root: Path, study_id: str) -> tuple[Path, dict, dict]:
    if not re.fullmatch(r"study-[a-f0-9]{20}", study_id):
        raise ValueError("invalid study id")
    output = root / "outputs" / "retrospectives" / study_id
    manifest = read_json(output / "study_manifest.json")
    if manifest.get("code_bundle_id"):
        verify_code(root, manifest["code_bundle_id"])
    for name, hash_ in manifest["artifacts"].items():
        if digest((output / name).read_bytes()) != hash_:
            raise RuntimeError(f"study artifact failed verification: {name}")
    saved = read_json(root / "data" / "retrospectives" / (manifest["snapshot_hash"] + ".json"))
    if digest(canonical(saved)) != manifest["snapshot_hash"]:
        raise RuntimeError("study snapshot hash mismatch")
    for obj in saved["raw_objects"] + saved["prior"].get("raw_objects", []) + saved["prior"]["data"]["source"].get("original_objects", []):
        if digest((root / obj["stored_at"]).read_bytes()) != obj["content_hash"]:
            raise RuntimeError("study raw object hash mismatch")
    return output, manifest, saved


def _retrospective(root: Path, ranking_dir: Path, input_dir: Path | None = None,
                  config_path: Path | None = None, prior_run_id: str | None = None,
                  feature_as_of: str | None = None) -> dict:
    meta, ranking, raws = FileRankingProvider(ranking_dir).load()
    prior, config, source_raws, provenance = _prior(root, meta, input_dir, config_path, prior_run_id, feature_as_of)
    if (meta["data_grade"] == "synthetic") != (prior["data_grade"] == "synthetic"):
        raise ValueError("ranking and feature synthetic grades must agree")
    raws.update({"market/" + name: raw for name, raw in source_raws.items()})
    refs = []
    for name, raw in sorted(raws.items()):
        hash_ = digest(raw)
        target = root / "data" / "raw" / hash_
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        elif digest(target.read_bytes()) != hash_:
            raise RuntimeError("existing raw object hash mismatch")
        refs.append({"name": name, "content_hash": hash_, "stored_at": f"data/raw/{hash_}"})
    # Collection time is operational metadata, not a source of new numerical identity.
    provenance.pop("collected_at", None)
    saved = {"version": VERSION, "prior": prior, "config": config, "ranking_meta": meta,
             "ranking": ranking, "provenance": provenance, "raw_objects": refs}
    snapshot_hash = digest(canonical(saved))
    code_hash = digest(Path(__file__).read_bytes() + _code_hash().encode())
    study_id = "study-" + stable_id(snapshot_hash, code_hash)
    output = root / "outputs" / "retrospectives" / study_id
    con = _db(root)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS ranking_studies(study_id TEXT PRIMARY KEY, snapshot_hash TEXT, status TEXT, completed_at TEXT)")
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        if (output / "study_manifest.json").exists():
            return _verify(root, study_id)[1]
        stage = output.with_name(".staging-" + study_id)
        if stage.exists():
            resolved = stage.resolve()
            if resolved.parent != output.parent.resolve() or not resolved.name.startswith(".staging-study-"):
                raise RuntimeError("unsafe study staging directory")
            shutil.rmtree(resolved)
        stage.mkdir(parents=True)
        rows, features, unmatched, comparison, drafts = _analysis(prior, ranking, config, study_id, meta, provenance)
        write_csv(stage / "retrospective.csv", STUDY_FIELDS, rows)
        write_csv(stage / "ranked_prior.csv", STUDY_FIELDS, [r for r in rows if r["in_ranking"]])
        write_csv(stage / "unmatched.csv", RANKING_FIELDS + ["reason"], unmatched)
        for name, value in (("decisions.json", rows), ("features.json", features), ("comparison.json", comparison),
                            ("hypothesis_drafts.json", drafts), ("disclosure_exclusions.json", prior["rejected_disclosures"])):
            write_json(stage / name, value)
        (stage / "report.md").write_text(
            f"# 急騰前の振り返り\n\nランキング日: {meta['ranking_session']} / 特徴日: {prior['target_session']}\n\n"
            f"情報締切: {prior['as_of']} / grade: {prior['data_grade']} / 入力: {provenance['availability_basis']}\n\n"
            f"全母集団: {len(rows)} / ランキング一致: {comparison['matched_count']} / 不一致: {comparison['unmatched_count']}\n\n"
            "本出力は事例研究であり、事前予測の成績・売買利益ではありません。非掲載を非急騰と扱いません。\n"
            "合成データは市場検証ではありません。再構成では訂正・取得遅延・モデルの後年知識に限界があります。\n",
            encoding="utf-8")
        write_json(root / "data" / "retrospectives" / (snapshot_hash + ".json"), saved)
        manifest = {"schema_version": 1, "study_id": study_id, "status": "SUCCEEDED", "version": VERSION,
                    "code_bundle_id": archive_code(root),
                    "snapshot_hash": snapshot_hash, "code_hash": code_hash, "config_hash": digest(canonical(config)),
                    "completed_at": now_iso(), "ranking_session": meta["ranking_session"],
                    "feature_session": prior["target_session"], "feature_cutoff": prior["as_of"],
                    "data_grade": prior["data_grade"], "prediction_score_eligible": False,
                    "provenance": provenance, "output_path": str(output),
                    "matched_count": comparison["matched_count"], "unmatched_count": comparison["unmatched_count"],
                    "artifacts": {p.name: digest(p.read_bytes()) for p in stage.iterdir()}}
        write_json(stage / "study_manifest.json", manifest)
        os.replace(stage, output)
        con.execute("INSERT OR REPLACE INTO ranking_studies VALUES(?,?,?,?)", (study_id, snapshot_hash, "SUCCEEDED", manifest["completed_at"]))
        con.commit()
        return manifest
    finally:
        con.close()


def retrospective(root: Path, ranking_dir: Path, input_dir: Path | None = None,
                  config_path: Path | None = None, prior_run_id: str | None = None,
                  feature_as_of: str | None = None) -> dict:
    attempt = {"attempt_id": uuid.uuid4().hex, "started_at": now_iso(), "status": "RUNNING",
               "ranking_input": str(ranking_dir), "market_input": str(input_dir) if input_dir else None,
               "prior_run_id": prior_run_id}
    path = root / "data" / "operations" / "retrospective_attempts" / (attempt["attempt_id"] + ".json")
    write_json(path, attempt)
    try:
        result = _retrospective(root, ranking_dir, input_dir, config_path, prior_run_id, feature_as_of)
        attempt.update(status="SUCCEEDED", study_id=result["study_id"])
        return result
    except Exception as error:
        attempt.update(status="FAILED", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        attempt["completed_at"] = now_iso()
        write_json(path, attempt)


def replay_retrospective(root: Path, study_id: str) -> dict:
    output, manifest, saved = _verify(root, study_id)
    if manifest["code_hash"] != digest(Path(__file__).read_bytes() + _code_hash().encode()):
        raise RuntimeError("study requires its original code revision")
    rows, features, unmatched, comparison, drafts = _analysis(saved["prior"], saved["ranking"], saved["config"],
                                                            study_id, saved["ranking_meta"], saved["provenance"])
    for name, value in (("decisions.json", rows), ("features.json", features), ("comparison.json", comparison),
                        ("hypothesis_drafts.json", drafts)):
        if value != read_json(output / name):
            raise AssertionError(f"retrospective replay differs: {name}")
    return {**manifest, "replay_verified": True}


def create_ranking_fixture(directory: Path) -> dict:
    from .fixture import create
    fixture = create(directory)
    write_json(directory / "calendar.json", read_json(directory / "future.json")["calendar"])
    session = fixture["next_session"]
    rows = [{"instrument_id": "TSE:" + symbol, "symbol": symbol, "rank": rank, "listed_return": value,
             "source_url": "https://finance.yahoo.co.jp/stocks/ranking/up?market=all&term=daily&page=1",
             "source_updated_at": session + "T18:00:00+09:00", "page": 1}
            for rank, (symbol, value) in enumerate((("0001", .12), ("0004", .15), ("0999", .20)), 1)]
    write_csv(directory / "ranking.csv", RANKING_FIELDS, rows)
    write_json(directory / "ranking_source.json", {"schema_version": 1, "ranking_session": session,
               "fetched_at": session + "T20:00:00+09:00", "data_grade": "synthetic",
               "acquisition_method": "synthetic", "coverage": "partial"})
    return {**fixture, "ranking_session": session}


def register_hypothesis(root: Path, study_id: str, period_start: str, period_end: str) -> dict:
    output, manifest, _ = _verify(root, study_id)
    if date.fromisoformat(period_start) <= date.fromisoformat(manifest["ranking_session"]) or date.fromisoformat(period_end) < date.fromisoformat(period_start):
        raise ValueError("holdout period must follow discovery and be ordered")
    drafts = read_json(output / "hypothesis_drafts.json")
    if not drafts:
        raise ValueError("no hypothesis draft: insufficient matched features")
    draft = drafts[0]
    requirements = f"data_grade={manifest['data_grade']}; source={study_id}; " + draft["required_data"]
    values = (draft["hypothesis_id"], 1, draft["thesis"], requirements, draft["comparison"],
              period_start, period_end, "DRAFT")
    con = _db(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        prior = con.execute("SELECT hypothesis_id,version,thesis,required_data,comparison,period_start,period_end,state FROM hypotheses WHERE hypothesis_id=? AND version=1", (draft["hypothesis_id"],)).fetchone()
        if prior is not None and prior != values:
            raise ValueError("hypothesis already registered with different period or content")
        if prior is None:
            at = now_iso()
            con.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?)", (*values, at))
            con.execute("INSERT INTO hypothesis_events VALUES(?,?,?,?,?)", (draft["hypothesis_id"], 1, at, "CREATE", study_id))
        con.commit()
        return {"hypothesis_id": draft["hypothesis_id"], "state": "DRAFT", "period_start": period_start,
                "period_end": period_end, "data_grade": manifest["data_grade"], "adoption_criteria_registered": False}
    finally:
        con.close()
