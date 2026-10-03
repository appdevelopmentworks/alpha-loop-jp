from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import mean, median

from .common import stable_id

CANDIDATE_FIELDS = "decision_id run_id target_session as_of instrument_id symbol name strategy_id strategy_version stage rank ret_1d ret_5d rel_volume_20d high_distance_20d ma25_gap turnover_median_20d_jpy semantic_status event_ids evidence_ids reason_codes data_grade turnover_status turnover_observed_sessions_20d liquidity_filter_status".split()
DECISION_FIELDS = CANDIDATE_FIELDS + "selected is_eligible rule_flags quality_status exclusion_codes".split()


def _feature(history: list[dict], calendar: list[str], session: str, min_history: int) -> tuple[dict, str]:
    index = calendar.index(session)
    window = calendar[max(0, index - min_history + 1): index + 1]
    by_date = {bar["session_date"]: bar for bar in history}
    if len(window) < min_history or any(day not in by_date for day in window):
        return {}, "history_missing"
    bars = [by_date[d] for d in window]
    if any(b["bar_status"] != "ok" for b in bars):
        return {}, "bar_unavailable"
    if bars[-1]["adj_volume"] <= 0:
        return {}, "zero_volume_target"
    if any(b["adj_volume"] <= 0 for b in bars[-21:-1]) or mean(b["adj_volume"] for b in bars[-21:-1]) <= 0:
        return {}, "volume_denominator_zero"
    if any(b["adjustment_basis"] != "split_only" for b in bars):
        return {}, "adjustment_unknown"
    dec = lambda value: Decimal(str(value))
    today = bars[-1]
    prev_high = max(dec(b["adj_high"]) for b in bars[-21:-1])
    exact = {
        "ret_1d": dec(today["adj_close"]) / dec(bars[-2]["adj_close"]) - 1,
        "ret_5d": dec(today["adj_close"]) / dec(bars[-6]["adj_close"]) - 1,
        "rel_volume_20d": dec(today["adj_volume"]) / mean(dec(b["adj_volume"]) for b in bars[-21:-1]),
        "high_distance_20d": dec(today["adj_close"]) / prev_high - 1,
        "ma25_gap": dec(today["adj_close"]) / mean(dec(b["adj_close"]) for b in bars[-25:]) - 1,
    }
    turnovers = [b.get("turnover_jpy") for b in bars[-21:-1] if b.get("turnover_jpy") is not None]
    result = {
        **{key: float(value) for key, value in exact.items()},
        "turnover_median_20d_jpy": median(turnovers) if len(turnovers) == 20 else None,
        "turnover_observed_sessions_20d": len(turnovers),
        "turnover_status": "observed_complete" if len(turnovers) == 20 else "observed_partial" if turnovers else "missing",
        "feature_bar_ids": [f'{today["instrument_id"]}:{b["session_date"]}' for b in bars],
        "_exact": exact,
    }
    return result, "ok"


def screen(snapshot: dict, config: dict, run_id: str) -> tuple[list[dict], list[dict]]:
    data = snapshot["data"]
    calendar = data["calendar"]
    session = snapshot["target_session"]
    bars_by_id = defaultdict(list)
    for bar in data["bars"]:
        bars_by_id[bar["instrument_id"]].append(bar)
    events = defaultdict(list)
    for item in snapshot["usable_disclosures"]:
        events[item["instrument_id"]].append(item["disclosure_id"] + ":" + item["revision_id"])
    rows = []
    features = []
    liquidity_required = config.get("turnover_filter_mode", "required" if config.get("turnover_median_min_jpy") is not None else "disabled") == "required"
    for instrument in sorted(data["instruments"], key=lambda x: x["instrument_id"]):
        iid = instrument["instrument_id"]
        feat, quality = _feature(bars_by_id[iid], calendar, session, config["min_history_sessions"])
        exact = feat.pop("_exact", {})
        flags = {"overheated": False, "early": False, "premove": False, "watch": bool(events[iid])}
        eligible = instrument["market"] in ("Prime", "Standard", "Growth") and instrument["security_type"] == "common" and instrument["listing_status"] == "listed"
        reasons = []
        turnover = feat.get("turnover_median_20d_jpy")
        liquidity_status = "not_evaluated" if quality != "ok" else "missing" if liquidity_required and turnover is None else "enabled" if liquidity_required else "disabled"
        if quality == "ok" and not liquidity_required:
            reasons.append("turnover_filter_disabled")
            if turnover is None:
                reasons.append("turnover_unavailable")
        if quality != "ok":
            stage = "DATA_HOLD"
            reasons.append(quality)
        elif not eligible:
            stage = "INELIGIBLE"
            reasons.append("outside_universe")
        else:
            ret5, rv, hd, gap = (exact[x] for x in ("ret_5d", "rel_volume_20d", "high_distance_20d", "ma25_gap"))
            flags["overheated"] = ret5 > Decimal("0.20") or gap > Decimal("0.15")
            flags["early"] = hd > 0 and rv >= 2 and ret5 <= Decimal("0.12")
            flags["premove"] = Decimal("-0.03") <= ret5 <= Decimal("0.05") and rv >= Decimal("1.5") and Decimal("-0.05") <= hd <= 0
            if liquidity_required and turnover is None:
                stage = "DATA_HOLD"
                reasons.append("turnover_required_missing")
            elif liquidity_required and turnover < config["turnover_median_min_jpy"]:
                stage = "INELIGIBLE"
                reasons.append("low_liquidity")
            elif flags["overheated"]:
                stage = "OVERHEATED"
                reasons.append("overheated")
            elif flags["early"]:
                stage = "EARLY"
            elif flags["premove"]:
                stage = "PREMOVE"
            elif flags["watch"]:
                stage = "WATCH"
            else:
                stage = "NONE"
                reasons.append("no_rule_match")
        if stage in ("EARLY", "PREMOVE", "WATCH"):
            reasons.append("selected_rule")
        row = {
            "decision_id": stable_id(run_id, iid, config["strategy_version"]), "run_id": run_id,
            "target_session": session, "as_of": snapshot["as_of"], "instrument_id": iid,
            "symbol": instrument["symbol"], "name": instrument["name"],
            "strategy_id": config["strategy_id"], "strategy_version": config["strategy_version"],
            "stage": stage, "rank": None, "semantic_status": "unavailable",
            "event_ids": sorted(events[iid]), "evidence_ids": [], "reason_codes": reasons,
            "turnover_status": feat.get("turnover_status", "not_evaluated"),
            "turnover_observed_sessions_20d": feat.get("turnover_observed_sessions_20d"),
            "liquidity_filter_status": liquidity_status,
            "data_grade": snapshot["data_grade"], "selected": False, "is_eligible": eligible,
            "rule_flags": flags, "quality_status": quality, "exclusion_codes": [] if stage in ("EARLY", "PREMOVE", "WATCH") else reasons,
        }
        row.update({k: feat.get(k) for k in ("ret_1d", "ret_5d", "rel_volume_20d", "high_distance_20d", "ma25_gap", "turnover_median_20d_jpy")})
        rows.append(row)
        features.append({"decision_id": row["decision_id"], "instrument_id": iid, "quality_status": quality, **feat})
    for stage in ("EARLY", "PREMOVE", "WATCH"):
        group = [r for r in rows if r["stage"] == stage]
        group.sort(key=lambda r: (-(r["rel_volume_20d"] or 0), -(r["turnover_median_20d_jpy"] or 0) if liquidity_required else 0, r["instrument_id"]))
        for rank, row in enumerate(group[:config["max_candidates_per_stage"]], 1):
            row["rank"] = rank
            row["selected"] = True
        for row in group[config["max_candidates_per_stage"]:]:
            row["reason_codes"].append("rank_limit")
            row["exclusion_codes"] = ["rank_limit"]
    candidates = sorted((r for r in rows if r["selected"]), key=lambda r: ({"EARLY": 0, "PREMOVE": 1, "WATCH": 2}[r["stage"]], r["rank"]))
    return rows, features
