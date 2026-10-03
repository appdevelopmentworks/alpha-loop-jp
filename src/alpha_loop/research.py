"""M5: immutable preregistration, paired holdout comparison and adoption reports.

No model calls, provider downloads, strategy changes or orders occur here.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from .common import JST, atomic_bytes, canonical, digest, now_iso, parse_time, read_json, stable_id, write_csv, write_json
from .hypothesis_plans import CATALOG_VERSION, validate_plans
from .evaluation import evaluate
from .pipeline import _code_hash, _db, _verified_manifest, _verified_snapshot, config_load
from .provenance import archive_code, verify_code
from .research_metrics import comparison_metrics, decide

VERSION = "m5-paired-filter-v1"
DEFAULT_CRITERIA = {
    "stages": ["EARLY", "PREMOVE"], "k_per_stage": 20,
    "min_days": 30, "min_valid": 200, "min_unique_instruments": 50, "min_event_clusters": 50,
    "min_hit_rate_improvement": .03, "max_unknown_rate": .20,
    "max_capture_drop": .05, "min_candidate_retention": .50,
    "confidence": .95, "bootstrap_samples": 2000, "seed": 20260930,
    "max_trials": 10, "purge_sessions": 5, "cost_scenarios_bps": [0, 10, 30, 50],
}
SAMPLE_FIELDS = "split session instrument_id market regime liquidity source_run_id decision_id stage cluster_id event_groups A_selected B_selected condition_pass condition_reasons discovery_hit discovery_return outcome_status execution_status gross_proxy_return exclusion_reason".split()


def draft_from_qwen(root: Path, report_path: Path, calendar_path: Path, out: Path,
                    baseline_path: Path, tuning_days: int = 10, holdout_days: int = 30) -> dict:
    """Prepare explicit design defaults from existing finite Qwen plans; no adoption."""
    from .retrospective import _verify
    report = read_json(report_path)
    _, source, _ = _verify(root, report["study_id"])
    proposals = report["hypothesis_plans"]
    checked = validate_plans(json.dumps({"hypotheses": [{"condition_ids": p["condition_ids"], "reason_code": p["reason_code"]} for p in proposals]}),
                             report["study_id"], report["data_grade"])
    if any(p["hypothesis_id"] != q["hypothesis_id"] or p["conditions"] != q["conditions"] for p, q in zip(proposals, checked)):
        raise ValueError("Qwen plans differ from current finite catalog")
    if type(tuning_days) is not int or tuning_days < 1 or type(holdout_days) is not int or holdout_days < DEFAULT_CRITERIA["min_days"]:
        raise ValueError("positive tuning period and at least default holdout days required")
    calendar = read_json(calendar_path)
    cutoff = max(source["ranking_session"], parse_time(now_iso()).astimezone(JST).date().isoformat())
    future = [day for day in calendar if day > cutoff]
    if len(future) < tuning_days + holdout_days + 5:
        raise ValueError("calendar too short for tuning, holdout and purging")
    periods = {"discovery": {"start": source["feature_session"], "end": source["ranking_session"]},
               "tuning": {"start": future[0], "end": future[tuning_days - 1]},
               "holdout": {"start": future[tuning_days], "end": future[tuning_days + holdout_days - 1]}}
    paths = []
    for proposal in proposals:
        path = out / (proposal["hypothesis_id"] + ".json")
        value = {"schema_version": 1, "hypothesis_id": proposal["hypothesis_id"], "version": 1,
                 "family_id": "F-M5-" + report["study_id"], "mode": "research",
                 "periods": periods, "baseline_config_path": str(baseline_path.resolve()),
                 "calendar_path": str(calendar_path.resolve()), "proposal_ref": str(report_path.resolve()),
                 "condition_ids": proposal["condition_ids"], "reason_code": proposal["reason_code"],
                 "criteria": {**DEFAULT_CRITERIA, "k_per_stage": config_load(baseline_path)["max_candidates_per_stage"]}}
        if path.exists() and read_json(path) != value:
            raise ValueError("draft already exists with different content; choose a new output directory")
        write_json(path, value)
        paths.append(str(path))
    return {"state": "DRAFT", "plan_paths": paths, "periods": periods,
            "thresholds_are_design_defaults": True, "automatic_adoption": False}


def _id(value: str, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value):
        raise ValueError("invalid local identifier")
    if prefix and not value.startswith(prefix):
        raise ValueError("identifier prefix mismatch")
    return value


def _store(root: Path):
    con = _db(root)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS m5_experiments(
      experiment_id TEXT PRIMARY KEY, hypothesis_id TEXT, version INTEGER, family_id TEXT,
      registration_hash TEXT, consumed_hash TEXT, report_id TEXT,
      UNIQUE(hypothesis_id,version));
    CREATE TABLE IF NOT EXISTS m5_reports(
      report_id TEXT PRIMARY KEY, experiment_id TEXT, manifest_hash TEXT, completed_at TEXT);
    """)
    con.execute("INSERT OR IGNORE INTO schema_history VALUES(2,?)", (now_iso(),))
    con.commit()
    return con


def engine_hash() -> str:
    directory = Path(__file__).parent
    return digest(b"".join((directory / name).read_bytes() for name in
                           ("research.py", "research_metrics.py", "hypothesis_plans.py")))


def _criteria(value: dict, baseline: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CRITERIA):
        raise ValueError("all preregistration criteria must be explicit; unknown criteria rejected")
    canonical(value)
    integer_fields = ("k_per_stage", "min_days", "min_valid", "min_unique_instruments", "min_event_clusters",
                      "bootstrap_samples", "seed", "max_trials", "purge_sessions")
    for key in integer_fields:
        if type(value[key]) is not int or value[key] < (0 if key == "seed" else 1):
            raise ValueError("invalid integer criterion: " + key)
    if value["k_per_stage"] != baseline["max_candidates_per_stage"]:
        raise ValueError("candidate K must match the frozen baseline")
    if value["purge_sessions"] != 5 or not 200 <= value["bootstrap_samples"] <= 20000 or not 1 <= value["max_trials"] <= 100:
        raise ValueError("purging is fixed at five sessions; invalid bootstrap or trial bounds")
    if not isinstance(value["stages"], list) or not value["stages"] or len(set(value["stages"])) != len(value["stages"]) or any(s not in ("EARLY", "PREMOVE") for s in value["stages"]):
        raise ValueError("M5 v1 compares price stages EARLY/PREMOVE only")
    for key in ("min_hit_rate_improvement", "max_unknown_rate", "max_capture_drop", "min_candidate_retention", "confidence"):
        v = value[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1:
            raise ValueError("invalid finite ratio criterion: " + key)
    if not .8 <= value["confidence"] < 1 or value["min_hit_rate_improvement"] <= 0:
        raise ValueError("positive improvement and confidence in [.8,1) required")
    costs = value["cost_scenarios_bps"]
    if not isinstance(costs, list) or not costs or len(set(costs)) != len(costs) or any(type(c) not in (int, float) or not math.isfinite(c) or c < 0 for c in costs):
        raise ValueError("explicit finite nonnegative roundtrip cost scenarios required")
    return value


def _split(day: str, periods: dict) -> str | None:
    return next((name for name, period in periods.items() if period["start"] <= day <= period["end"]), None)


def register(root: Path, plan_path: Path) -> dict:
    value = read_json(plan_path)
    required = {"schema_version", "hypothesis_id", "version", "family_id", "mode", "periods",
                "baseline_config_path", "calendar_path", "condition_ids", "reason_code", "criteria"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"proposal_ref"}:
        raise ValueError("incomplete or unknown experiment fields")
    if value["schema_version"] != 1 or value["mode"] not in ("synthetic", "research", "prospective"):
        raise ValueError("invalid experiment mode/schema")
    _id(value["hypothesis_id"])
    _id(value["family_id"])
    if type(value["version"]) is not int or value["version"] < 1:
        raise ValueError("positive hypothesis version required")
    baseline = config_load(root / value["baseline_config_path"])
    criteria = _criteria(value["criteria"], baseline)
    calendar = read_json(root / value["calendar_path"])
    if not isinstance(calendar, list) or not calendar or calendar != sorted(set(calendar)):
        raise ValueError("explicit sorted exchange calendar required")
    for day in calendar:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("ISO calendar date required")
    periods = value["periods"]
    if not isinstance(periods, dict) or set(periods) != {"discovery", "tuning", "holdout"}:
        raise ValueError("three chronological periods required")
    for name in ("discovery", "tuning", "holdout"):
        period = periods[name]
        if not isinstance(period, dict) or set(period) != {"start", "end"} or any(d not in calendar for d in period.values()) or period["start"] > period["end"]:
            raise ValueError("ordered exchange-session boundaries required")
    if not periods["discovery"]["end"] < periods["tuning"]["start"] or not periods["tuning"]["end"] < periods["holdout"]["start"]:
        raise ValueError("periods overlap or are not chronological")
    end_index = calendar.index(periods["holdout"]["end"])
    if end_index + criteria["purge_sessions"] >= len(calendar):
        raise ValueError("calendar must include five sessions beyond holdout")
    if sum(periods["holdout"]["start"] <= d <= periods["holdout"]["end"] for d in calendar) < criteria["min_days"]:
        raise ValueError("planned holdout cannot satisfy minimum observation days")
    plan = validate_plans(json.dumps({"hypotheses": [{"condition_ids": value["condition_ids"],
                                                     "reason_code": value["reason_code"]}]}),
                          "m5-manual", value["mode"])[0]
    proposal_evidence = None
    if value.get("proposal_ref"):
        from .retrospective import _verify
        proposal_path = root / value["proposal_ref"]
        report = read_json(proposal_path)
        _, manifest, _ = _verify(root, report["study_id"])
        matching = [p for p in report["hypothesis_plans"] if p["hypothesis_id"] == value["hypothesis_id"]]
        if len(matching) != 1 or matching[0]["condition_ids"] != plan["condition_ids"] or matching[0]["reason_code"] != plan["reason_code"]:
            raise ValueError("proposal identity/conditions differ from the registered plan")
        if manifest["ranking_session"] > periods["discovery"]["end"]:
            raise ValueError("proposal contains information beyond discovery period")
        report_raw = proposal_path.read_bytes()
        report_hash = digest(report_raw)
        proposal_raw_ref = "data/raw/" + report_hash
        atomic_bytes(root / proposal_raw_ref, report_raw)
        proposal_evidence = {"study_id": report["study_id"], "report_hash": report_hash, "report_raw_ref": proposal_raw_ref,
                             "report_ref": str(proposal_path.resolve()), "study_manifest_hash": digest(canonical(manifest))}
    payload = {"schema_version": 1, "engine_version": VERSION, "engine_hash": engine_hash(),
               "numerical_code_hash": _code_hash(), "hypothesis_id": value["hypothesis_id"],
               "version": value["version"], "family_id": value["family_id"], "mode": value["mode"],
               "periods": periods, "calendar": calendar, "baseline_config": baseline,
               "baseline_config_hash": digest(canonical(baseline)), "criteria": criteria,
               "condition_ids": plan["condition_ids"], "conditions": plan["conditions"],
               "reason_code": plan["reason_code"], "thesis": plan["thesis"], "catalog_version": CATALOG_VERSION,
               "proposal_evidence": proposal_evidence,
               "primary_metric": "paired daily stage hit-rate delta (B minus A)",
               "comparison_kind": "price_filter_of_baseline_top_k_without_refill",
               "selection_policy": "first baseline completed per session / first complete one-session evaluation",
               "thresholds_are_design_defaults": True, "automatic_adoption": False}
    experiment_id = "exp-" + stable_id(payload)
    con = _store(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        prior = con.execute("SELECT experiment_id FROM m5_experiments WHERE hypothesis_id=? AND version=?",
                            (value["hypothesis_id"], value["version"])).fetchone()
        if prior:
            if prior[0] != experiment_id:
                raise ValueError("hypothesis version is immutable; register a new version and unused period")
            return _load(root, experiment_id, con)
        at = now_iso()
        if value["mode"] == "prospective" and periods["holdout"]["start"] <= parse_time(at).astimezone(JST).date().isoformat():
            raise ValueError("prospective holdout must start after actual registration date")
        members = con.execute("SELECT experiment_id,consumed_hash FROM m5_experiments WHERE family_id=?", (value["family_id"],)).fetchall()
        if len(members) >= criteria["max_trials"] or any(m[1] for m in members):
            raise ValueError("family trial budget exhausted or holdout already revealed")
        for revealed, in con.execute("SELECT experiment_id FROM m5_experiments WHERE consumed_hash IS NOT NULL"):
            previous = _load(root, revealed, con)
            a, b = previous["periods"]["holdout"], periods["holdout"]
            if a["start"] <= b["end"] and b["start"] <= a["end"]:
                raise ValueError("holdout overlaps a revealed period; use a new unused period")
        family_definition = {k: payload[k] for k in ("periods", "calendar", "criteria", "baseline_config_hash", "mode")}
        for member, _ in members:
            previous = _load(root, member, con)
            if any(previous[k] != v for k, v in family_definition.items()):
                raise ValueError("same family requires identical periods, baseline, mode and adoption criteria")
        legacy = con.execute("SELECT state FROM hypotheses WHERE hypothesis_id=? AND version=?", (value["hypothesis_id"], value["version"])).fetchone()
        if legacy:
            raise ValueError("existing hypothesis ledger version occupied; use a new version")
        registration = {"experiment_id": experiment_id, "registered_at": at,
                        "code_bundle_id": archive_code(root), **payload}
        path = root / "data" / "research" / "experiments" / (experiment_id + ".json")
        write_json(path, registration)
        con.execute("INSERT INTO m5_experiments VALUES(?,?,?,?,?,?,?)", (experiment_id, value["hypothesis_id"], value["version"], value["family_id"], digest(path.read_bytes()), None, None))
        con.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?)",
                    (value["hypothesis_id"], value["version"], plan["thesis"], "sealed baseline decisions and complete universe outcomes",
                     payload["comparison_kind"], periods["holdout"]["start"], periods["holdout"]["end"], "REGISTERED", at))
        con.execute("INSERT INTO hypothesis_events VALUES(?,?,?,?,?)", (value["hypothesis_id"], value["version"], at, "PREREGISTER", experiment_id))
        con.commit()
        return registration
    finally:
        con.close()


def _load(root: Path, experiment_id: str, con=None) -> dict:
    _id(experiment_id, "exp-")
    own = con is None
    con = con or _store(root)
    try:
        expected = con.execute("SELECT registration_hash FROM m5_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        path = root / "data" / "research" / "experiments" / (experiment_id + ".json")
        if not expected or digest(path.read_bytes()) != expected[0]:
            raise RuntimeError("registered experiment hash mismatch or missing registry entry")
        value = read_json(path)
        verify_code(root, value["code_bundle_id"])
        if value["proposal_evidence"]:
            proposal = value["proposal_evidence"]
            if digest((root / proposal["report_raw_ref"]).read_bytes()) != proposal["report_hash"]:
                raise RuntimeError("registered proposal raw object hash mismatch")
        return value
    finally:
        if own:
            con.close()


def _ready(spec: dict) -> bool:
    day = spec["calendar"][spec["calendar"].index(spec["periods"]["holdout"]["end"]) + 1]
    return parse_time(now_iso()) >= parse_time(day + "T20:00:00+09:00")


def _status(root: Path, spec: dict, reason: str) -> dict:
    result = {"experiment_id": spec["experiment_id"], "status": "WAITING", "recommendation": "HOLD",
              "reason_codes": [reason], "automatic_adoption": False,
              "holdout_end": spec["periods"]["holdout"]["end"], "metrics_revealed": False}
    write_json(root / "outputs" / "research" / spec["experiment_id"] / "status.json", result)
    return result


def _read_entry(root: Path, entry: dict, spec: dict):
    if not isinstance(entry, dict) or not {"run_id", "evaluation_id"} <= set(entry) or set(entry) - {"run_id", "evaluation_id", "future_ref"}:
        raise ValueError("batch entries require run_id, evaluation_id and optional future_ref")
    run_id, evaluation_id = _id(entry["run_id"], "run-"), _id(entry["evaluation_id"])
    output = root / "outputs" / run_id
    manifest = _verified_manifest(output)
    if not manifest or manifest["run_kind"] != "baseline" or manifest["code_hash"] != spec["numerical_code_hash"] or manifest["config_hash"] != spec["baseline_config_hash"]:
        raise ValueError("comparison baseline code/config/kind differs from preregistration")
    snapshot = _verified_snapshot(root, manifest["snapshot_id"])
    directory = output / "evaluation" / evaluation_id
    evaluated = read_json(directory / "evaluation_manifest.json")
    if evaluated["source_run_id"] != run_id or evaluated["evaluation_id"] != evaluation_id or evaluated["evaluation_version"] != spec["baseline_config"]["evaluation_version"] or evaluated["data_grade"] != snapshot["data_grade"]:
        raise ValueError("evaluation identity/version/grade mismatch")
    for name, hash_ in evaluated["artifacts"].items():
        if Path(name).name != name or digest((directory / name).read_bytes()) != hash_:
            raise RuntimeError("evaluation artifact hash mismatch")
    if not {"outcomes.json", "outcomes.csv", "metrics.json"} <= set(evaluated["artifacts"]):
        raise ValueError("evaluation is incomplete")
    decisions, outcomes = read_json(output / "decisions.json"), read_json(directory / "outcomes.json")
    instruments = {i["instrument_id"]: i for i in snapshot["data"]["instruments"]}
    if len({r["instrument_id"] for r in decisions}) != len(decisions) or set(instruments) != {r["instrument_id"] for r in decisions}:
        raise ValueError("decision universe is incomplete/duplicated")
    if len({o["instrument_id"] for o in outcomes}) != len(outcomes) or set(instruments) != {o["instrument_id"] for o in outcomes}:
        raise ValueError("outcomes must cover the SAME complete universe, including unknowns")
    by_id = {o["instrument_id"]: o for o in outcomes}
    for decision in decisions:
        outcome = by_id[decision["instrument_id"]]
        if outcome["decision_id"] != decision["decision_id"] or outcome["source_run_id"] != run_id or outcome["horizon_sessions"] != 1:
            raise ValueError("decision/outcome alignment or horizon mismatch")
        hit = outcome["discovery_hit"]
        if hit is not None and type(hit) is not bool or (hit is not None) != (outcome["outcome_status"] == "ok"):
            raise ValueError("unknown outcomes must not become negative labels")
        if hit is not None:
            ret = outcome["discovery_return"]
            if type(ret) not in (int, float) or not math.isfinite(ret):
                raise ValueError("nonfinite discovery return")
            # Saved labels use Decimal before serialization; float returns can lose
            # a last bit at the boundary. The immutable evaluation owns the label.
        if decision["run_id"] != run_id or decision["target_session"] != snapshot["target_session"]:
            raise ValueError("decision provenance mismatch")
    day = manifest["target_session"]
    if day not in spec["calendar"]:
        raise ValueError("source session outside frozen calendar")
    next_day = spec["calendar"][spec["calendar"].index(day) + 1]
    source_next = next((d for d in snapshot["data"]["calendar"] if d > day), None)
    if source_next != next_day:
        raise ValueError("frozen calendar and saved run disagree on next session")
    if evaluated["through"] < next_day or any(o["outcome_status"] == "horizon_incomplete" for o in outcomes):
        raise ValueError("one-session horizon incomplete")
    raw_verified = False
    if entry.get("future_ref"):
        raw = (root / entry["future_ref"]).read_bytes()
        if digest(raw) != evaluated["future_hash"]:
            raise RuntimeError("future evaluation input hash mismatch")
        future = json.loads(raw)
        if future["data_grade"] != snapshot["data_grade"] or next((d for d in future["calendar"] if d > day), None) != next_day:
            raise ValueError("evaluation input grade/next session differs from frozen calendar")
        recalculated, _ = evaluate(snapshot, decisions, future, spec["baseline_config"], evaluated["through"])
        normalized = lambda values: [{k: v for k, v in row.items() if k != "evaluated_at"} for row in values]
        if normalized(recalculated) != normalized(outcomes):
            raise RuntimeError("saved outcomes differ from immutable evaluation input")
        raw_verified = True
    events = defaultdict(set)
    for event in snapshot["usable_disclosures"]:
        events[event["instrument_id"]].add(event.get("event_group_id") or event["disclosure_id"])
    evidence = {"run_id": run_id, "evaluation_id": evaluation_id, "session": day,
                "run_manifest_hash": digest((output / "run_manifest.json").read_bytes()),
                "snapshot_id": manifest["snapshot_id"], "evaluation_manifest_hash": digest((directory / "evaluation_manifest.json").read_bytes()),
                "future_hash": evaluated["future_hash"], "data_grade": snapshot["data_grade"],
                "prediction_score_eligible": evaluated["prediction_score_eligible"],
                "evaluation_raw_verified": raw_verified,
                "as_of": manifest["as_of"], "completed_at": manifest["completed_at"],
                "artifacts": {"decisions": manifest["artifacts"]["decisions.json"], "outcomes": evaluated["artifacts"]["outcomes.json"]}}
    return manifest, decisions, by_id, instruments, events, evidence


def _rows(root: Path, batch: dict, spec: dict):
    if not isinstance(batch, dict) or set(batch) != {"schema_version", "entries"} or batch["schema_version"] != 1 or not isinstance(batch["entries"], list):
        raise ValueError("invalid comparison batch")
    sessions, samples, evidence, groups = set(), [], [], defaultdict(set)
    for entry in sorted(batch["entries"], key=lambda e: e.get("run_id", "")):
        manifest, decisions, outcomes, instruments, events, receipt = _read_entry(root, entry, spec)
        day = manifest["target_session"]
        split = _split(day, spec["periods"])
        if split is None or day in sessions:
            raise ValueError("source day outside periods or duplicate daily run")
        sessions.add(day)
        evidence.append(receipt)
        for row in decisions:
            iid = row["instrument_id"]
            outcome = outcomes[iid]
            tests, reasons = [], []
            for condition in spec["conditions"]:
                value = row.get(condition["feature"])
                if type(value) not in (int, float) or not math.isfinite(value):
                    tests.append(False)
                    reasons.append(condition["condition_id"] + ":feature_unknown")
                else:
                    a, b = Decimal(str(value)), Decimal(str(condition["value"]))
                    matched = a >= b if condition["operator"] == ">=" else a <= b
                    tests.append(matched)
                    if not matched:
                        reasons.append(condition["condition_id"] + ":condition_not_met")
            passes = all(tests)
            selected = bool(row["selected"] and row["stage"] in spec["criteria"]["stages"])
            gross = outcome["open_close_proxy_return"] if outcome["execution_status"] in ("proxy_only", "cost_unset") else None
            if gross is not None and (type(gross) not in (int, float) or not math.isfinite(gross)):
                raise ValueError("invalid trading proxy")
            samples.append({"split": split, "session": day, "instrument_id": iid,
                            "source_run_id": manifest["run_id"], "decision_id": row["decision_id"],
                            "market": instruments[iid]["market"], "regime": "unknown",
                            "liquidity": row["turnover_status"], "stage": row["stage"],
                            "event_groups": sorted(events[iid]), "cluster_id": iid,
                            "A_selected": selected, "B_selected": selected and passes,
                            "condition_pass": passes, "condition_reasons": reasons,
                            "discovery_hit": outcome["discovery_hit"], "discovery_return": outcome["discovery_return"],
                            "outcome_status": outcome["outcome_status"], "execution_status": outcome["execution_status"],
                            "gross_proxy_return": gross, "exclusion_reason": "outside_universe" if not row["is_eligible"] else None})
            groups[split].update(events[iid])
    # Preserve audit rows for every universe member and partition. Exclude common
    # material events across partitions and five-session overlap from BOTH arms.
    calendar = spec["calendar"]
    purge_log = []
    earlier = set()
    for split, next_split in (("discovery", "tuning"), ("tuning", "holdout"), ("holdout", None)):
        boundary = spec["periods"][next_split]["start"] if next_split else None
        for row in [r for r in samples if r["split"] == split]:
            reason = None
            if earlier.intersection(row["event_groups"]):
                reason = "event_group_crosses_partition"
            if boundary:
                index = calendar.index(row["session"]) + spec["criteria"]["purge_sessions"]
                if index >= len(calendar) or calendar[index] >= boundary:
                    reason = "five_session_horizon_crosses_partition"
            if reason:
                row["exclusion_reason"] = reason
                purge_log.append({"session": row["session"], "instrument_id": row["instrument_id"], "split": split, "reason": reason})
        earlier.update(groups[split])
    # Conservative union: repeating an instrument or a shared event is never
    # counted as a new independent observation simply because the day changed.
    parent = {r["instrument_id"]: r["instrument_id"] for r in samples}
    def find(iid):
        while parent[iid] != iid:
            parent[iid] = parent[parent[iid]]
            iid = parent[iid]
        return iid
    event_owner = {}
    for row in samples:
        for event in row["event_groups"]:
            if event in event_owner:
                a, b = find(row["instrument_id"]), find(event_owner[event])
                parent[max(a, b)] = min(a, b)
            else:
                event_owner[event] = row["instrument_id"]
    for row in samples:
        row["cluster_id"] = find(row["instrument_id"])
    return sorted(samples, key=lambda r: (r["session"], r["instrument_id"])), sorted(evidence, key=lambda e: e["session"]), purge_log


def _verify_report(path: Path, con) -> dict:
    manifest = read_json(path / "manifest.json")
    expected = con.execute("SELECT manifest_hash FROM m5_reports WHERE report_id=?", (manifest["report_id"],)).fetchone()
    if not expected or digest((path / "manifest.json").read_bytes()) != expected[0]:
        raise RuntimeError("comparison report manifest hash mismatch")
    for name, hash_ in manifest["artifacts"].items():
        if Path(name).name != name or digest((path / name).read_bytes()) != hash_:
            raise RuntimeError("comparison report artifact hash mismatch")
    return read_json(path / "summary.json")


def _render(spec: dict, summary: dict, metrics: dict) -> str:
    lines = ["# M5 比較・採否報告", "", f"仮説: {spec['hypothesis_id']} / 版: {spec['version']}",
             f"条件: {spec['thesis']}", f"最終期間: {spec['periods']['holdout']['start']}〜{spec['periods']['holdout']['end']}",
             f"採否: **{summary['recommendation']}** / 統計判定: {summary['statistical_decision']}",
             "", "A: 固定した価格基準の上位K。B: Aに登録済みAND条件を適用（補充なし）。",
             "", "| 区分 | A選抜/有効/的中 | B選抜/有効/的中 | 日平均的中率差 B−A | 対応区間 |", "|---|---|---|---|---|"]
    for stage, group in metrics["by_stage"].items():
        a, b, paired = group["A"], group["B"], group["paired"]
        lines.append(f"| {stage} | {a['selected']}/{a['valid']}/{a['hits']} | {b['selected']}/{b['valid']}/{b['hits']} | {paired['delta_daily_mean_hit_rate']} | [{paired['lower']}, {paired['upper']}] |")
    lines.extend(["", "## 判定理由", ""] + ["- " + r for r in summary["reason_codes"]])
    lines.extend(["", f"登録済み試行数: {summary['registered_trials']} / 事前上限: {spec['criteria']['max_trials']}",
                  "再標本化は日と保守的なイベント/銘柄群の積重み。同じ標本でA/Bを対応比較し、事前試行上限と区分数で区間を補正する。",
                  "欠損はunknown。空日は的中率null。市場別・流動性別・未知の市場局面をmetrics.jsonへ保存。",
                  "材料イベント不明時は銘柄単位の保守的クラスタを使う。独立性を保証するものではない。",
                  "", "## 費用と売買", "", "始値→終値は仮想売買proxy。費用感度はcost_sensitivity.csv。未約定・停止・約定不明は収益に算入しない。実売買収益は算出しない。",
                  "", "## リリース", "", "採用候補でも人の証跡確認が必要。本番条件・費用・モデル・発注設定は変更していない。",
                  "合成データと後日再構成データは本番採用・予測精度の証拠にならない。"])
    return "\n".join(lines) + "\n"


def compare(root: Path, experiment_id: str, batch_path: Path) -> dict:
    spec = _load(root, experiment_id)
    if not _ready(spec):
        return _status(root, spec, "holdout_not_finished_no_interim_peeking")
    if spec["engine_hash"] != engine_hash() or spec["numerical_code_hash"] != _code_hash():
        raise RuntimeError("frozen comparison code differs; use the archived source bundle")
    batch = read_json(batch_path)
    samples, evidence, purging = _rows(root, batch, spec)
    # Manual batches must obey the same frozen selection policy as daily batches.
    canonical_batch = read_json(prepare_batch(root, experiment_id))
    expected_entries = {(e["run_id"], e["evaluation_id"]) for e in canonical_batch["entries"]}
    if any((e["run_id"], e["evaluation_id"]) not in expected_entries for e in batch["entries"]):
        raise ValueError("batch violates frozen first-run/first-evaluation selection policy")
    present = {e["session"] for e in evidence}
    final_days = [d for d in spec["calendar"] if _split(d, spec["periods"]) == "holdout"]
    if any(d not in present for d in final_days):
        return _status(root, spec, "holdout_sessions_missing_no_partial_metrics")
    input_hash = digest(canonical({"evidence": evidence, "engine_hash": spec["engine_hash"]}))
    report_id = "cmp-" + stable_id(experiment_id, input_hash)
    directory = root / "outputs" / "research" / experiment_id / report_id
    con = _store(root)
    try:
        con.execute("BEGIN IMMEDIATE")
        consumed, prior_id = con.execute("SELECT consumed_hash,report_id FROM m5_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        if consumed:
            if consumed != input_hash or prior_id != report_id:
                raise ValueError("holdout already consumed; changed input requires a new unused period")
            return _verify_report(directory, con)
        final = [r for r in samples if r["split"] == "holdout" and not r["exclusion_reason"]]
        metrics = comparison_metrics(final, final_days, spec["criteria"])
        quality = []
        grades = sorted({e["data_grade"] for e in evidence if _split(e["session"], spec["periods"]) == "holdout"})
        if len(grades) != 1:
            raise ValueError("mixed data grades cannot be compared as one experiment")
        if spec["mode"] != "prospective" or grades[0] not in ("observed", "vendor_pit"):
            quality.append("synthetic_or_reconstructed_research_only")
        if not all(e["prediction_score_eligible"] for e in evidence if _split(e["session"], spec["periods"]) == "holdout"):
            quality.append("prediction_timing_or_grade_gate_failed")
        if not all(e["evaluation_raw_verified"] for e in evidence):
            quality.append("evaluation_input_raw_missing_labels_not_recomputed")
        if any(d not in present for d in spec["calendar"] if _split(d, spec["periods"]) in ("discovery", "tuning")):
            quality.append("split_context_missing_event_leakage_not_verified")
        denominator = metrics["overall"]["A"]["universe_count"]
        if not denominator or metrics["overall"]["A"]["universe_unknown"] / denominator > spec["criteria"]["max_unknown_rate"]:
            quality.append("universe_unknown_rate_exceeded_or_empty")
        trials = con.execute("SELECT COUNT(*) FROM m5_experiments WHERE family_id=?", (spec["family_id"],)).fetchone()[0]
        summary = {"experiment_id": experiment_id, "report_id": report_id, "status": "COMPLETE",
                   "registered_trials": trials, "data_grades": grades, "mode": spec["mode"],
                   "purged_rows": len(purging), "final_rows": len(final), "final_days": len(final_days),
                   "input_hash": input_hash, **decide(metrics, spec["criteria"], quality),
                   "report_path": str(directory / "report.md"), "sample_csv": str(directory / "samples.csv"),
                   "metrics_path": str(directory / "metrics.json")}
        staging = directory.with_name(".staging-" + report_id)
        if staging.exists():
            if staging.resolve().parent != directory.parent.resolve():
                raise RuntimeError("unsafe comparison staging directory")
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        write_json(staging / "summary.json", summary)
        write_json(staging / "metrics.json", metrics)
        write_json(staging / "inputs.json", {"evidence": evidence, "purging": purging})
        write_json(staging / "registration.json", spec)
        write_csv(staging / "samples.csv", SAMPLE_FIELDS, samples)
        write_json(staging / "samples.json", samples)
        daily_rows = []
        cost_rows = []
        for stage, group in metrics["by_stage"].items():
            for arm in ("A", "B"):
                daily_rows.extend({"stage": stage, "arm": arm, **d} for d in group[arm]["daily"])
                cost_rows.extend({"stage": stage, "arm": arm, **c} for c in group[arm]["cost_sensitivity"])
        write_csv(staging / "daily_metrics.csv", ["stage", "arm", "session", "selected", "valid", "hits", "hit_rate"], daily_rows)
        write_csv(staging / "cost_sensitivity.csv", ["stage", "arm", "roundtrip_cost_bps", "net_proxy_count", "mean_net_proxy_return"], cost_rows)
        (staging / "report.md").write_text(_render(spec, summary, metrics), encoding="utf-8")
        manifest = {"report_id": report_id, "experiment_id": experiment_id, "status": "COMPLETE", "input_hash": input_hash,
                    "engine_hash": spec["engine_hash"], "completed_at": now_iso(),
                    "artifacts": {p.name: digest(p.read_bytes()) for p in staging.iterdir() if p.is_file()}}
        write_json(staging / "manifest.json", manifest)
        # A directory without a completed manifest is never returned as a result.
        if directory.exists():
            # Recover a crash between publishing immutable files and committing
            # the ledger, only after comparing freshly recalculated artifact hashes.
            previous = read_json(directory / "manifest.json")
            for key in ("report_id", "experiment_id", "input_hash", "engine_hash", "artifacts"):
                if previous[key] != manifest[key]:
                    raise RuntimeError("uncommitted comparison differs from recalculation; preserve for inspection")
            for name, hash_ in previous["artifacts"].items():
                if digest((directory / name).read_bytes()) != hash_:
                    raise RuntimeError("uncommitted comparison artifact hash mismatch")
            shutil.rmtree(staging.resolve())
            manifest = previous
        else:
            staging.rename(directory)
        con.execute("INSERT INTO m5_reports VALUES(?,?,?,?)", (report_id, experiment_id, digest((directory / "manifest.json").read_bytes()), manifest["completed_at"]))
        con.execute("UPDATE m5_experiments SET consumed_hash=?,report_id=? WHERE experiment_id=?", (input_hash, report_id, experiment_id))
        con.execute("UPDATE hypotheses SET state=? WHERE hypothesis_id=? AND version=?", (summary["recommendation"], spec["hypothesis_id"], spec["version"]))
        con.execute("INSERT INTO hypothesis_events VALUES(?,?,?,?,?)", (spec["hypothesis_id"], spec["version"], now_iso(), "COMPARE", json.dumps({"report_id": report_id, "recommendation": summary["recommendation"], "reasons": summary["reason_codes"]})))
        con.commit()
        _verify_report(directory, con)
        write_json(root / "outputs" / "research" / experiment_id / "status.json", summary)
        return summary
    finally:
        con.close()


def prepare_batch(root: Path, experiment_id: str) -> Path:
    """Select immutable first runs/evaluations using the policy frozen at registration."""
    spec = _load(root, experiment_id)
    choices = defaultdict(list)
    for path in (root / "outputs").glob("run-*/run_manifest.json"):
        m = read_json(path)
        if m["run_kind"] != "baseline" or _split(m["target_session"], spec["periods"]) is None or m["config_hash"] != spec["baseline_config_hash"] or m["code_hash"] != spec["numerical_code_hash"]:
            continue
        if spec["mode"] == "synthetic" and m["data_grade"] != "synthetic":
            continue
        if spec["mode"] != "synthetic" and m["data_grade"] == "synthetic":
            continue
        choices[m["target_session"]].append((m["completed_at"], m["run_id"], path.parent))
    entries = []
    raw_index = {}
    for directory in (root / "input", root / "data" / "operations" / "evaluation_inputs"):
        for path in sorted(directory.rglob("future.json")):
            raw_index.setdefault(digest(path.read_bytes()), str(path.relative_to(root)))
    for day, options in sorted(choices.items()):
        # Do not choose a later revision simply because its evaluation is present.
        _, run_id, output = min(options)
        evaluations = []
        for path in (output / "evaluation").glob("*/evaluation_manifest.json"):
            m = read_json(path)
            next_day = spec["calendar"][spec["calendar"].index(day) + 1]
            if m["through"] < next_day:
                continue
            rows = read_json(path.parent / "outcomes.json")
            if rows and not any(r["outcome_status"] == "horizon_incomplete" for r in rows):
                evaluations.append((min(r["evaluated_at"] for r in rows), m["evaluation_id"], m["future_hash"]))
        if evaluations:
            _, evaluation_id, future_hash = min(evaluations)
            entry = {"run_id": run_id, "evaluation_id": evaluation_id}
            if future_hash in raw_index:
                entry["future_ref"] = raw_index[future_hash]
            entries.append(entry)
    path = root / "data" / "research" / "batches" / (experiment_id + ".json")
    write_json(path, {"schema_version": 1, "entries": entries})
    return path


def refresh(root: Path) -> dict:
    con = _store(root)
    try:
        ids = [r[0] for r in con.execute("SELECT experiment_id FROM m5_experiments ORDER BY experiment_id")]
    finally:
        con.close()
    results = []
    for experiment_id in ids:
        try:
            spec = _load(root, experiment_id)
            if not _ready(spec):
                results.append(_status(root, spec, "holdout_not_finished_no_interim_peeking"))
            else:
                results.append(compare(root, experiment_id, prepare_batch(root, experiment_id)))
        except Exception as error:
            results.append({"experiment_id": experiment_id, "status": "FAILED", "error": str(error)[:500], "automatic_adoption": False})
    return {"status": "FAILED" if any(r["status"] == "FAILED" for r in results) else "OK" if ids else "NOT_REGISTERED", "experiments": results}
