"""Deterministic paired statistics for frozen M5 experiments (standard library)."""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from statistics import mean


def rate(numerator, denominator):
    return numerator / denominator if denominator else None


def arm_metrics(rows: list[dict], arm: str, days: list[str], costs: list[float]) -> dict:
    selected = [r for r in rows if r[arm + "_selected"]]
    valid = [r for r in selected if r["discovery_hit"] is not None]
    hits = sum(r["discovery_hit"] for r in valid)
    known = [r for r in rows if r["discovery_hit"] is not None]
    universe_hits = sum(r["discovery_hit"] for r in known)
    daily = []
    for day in days:
        group = [r for r in selected if r["session"] == day]
        labels = [r for r in group if r["discovery_hit"] is not None]
        daily.append({"session": day, "selected": len(group), "valid": len(labels),
                      "hits": sum(r["discovery_hit"] for r in labels),
                      "hit_rate": rate(sum(r["discovery_hit"] for r in labels), len(labels))})
    gross = [r["gross_proxy_return"] for r in selected if r["gross_proxy_return"] is not None]
    unknown = len(selected) - len(valid)
    selected_rate, universe_rate = rate(hits, len(valid)), rate(universe_hits, len(known))
    return {"selected": len(selected), "valid": len(valid), "unknown": unknown, "hits": hits,
            "hit_rate": selected_rate, "unknown_rate": rate(unknown, len(selected)),
            "unknown_as_failure": rate(hits, len(selected)),
            "unknown_as_success": rate(hits + unknown, len(selected)),
            "daily_mean_hit_rate": mean([d["hit_rate"] for d in daily if d["hit_rate"] is not None])
                if any(d["hit_rate"] is not None for d in daily) else None,
            "empty_days": sum(d["selected"] == 0 for d in daily), "daily": daily,
            "unique_instruments": len({r["instrument_id"] for r in valid}),
            "event_clusters": len({r["cluster_id"] for r in valid}),
            "false_positives": len(valid) - hits,
            "universe_count": len(rows), "universe_valid": len(known),
            "universe_unknown": len(rows) - len(known), "universe_hits": universe_hits,
            "universe_hit_rate": universe_rate, "capture_rate": rate(hits, universe_hits),
            "enrichment": selected_rate / universe_rate if selected_rate is not None and universe_rate else None,
            "trade_proxy_count": len(gross), "mean_gross_proxy_return": mean(gross) if gross else None,
            "cost_sensitivity": [{"roundtrip_cost_bps": c, "net_proxy_count": len(gross),
                                   "mean_net_proxy_return": mean(gross) - c / 10000 if gross else None}
                                  for c in costs],
            "execution_counts": dict(sorted(Counter(r["execution_status"] for r in selected).items())),
            "actual_trading_return": None}


def _quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * p
    lower = int(position)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)


def paired_interval(rows: list[dict], days: list[str], criteria: dict, seed: int) -> dict:
    # Resample days and conservative event/instrument clusters independently and use
    # their product as weights. Both arms always share the same resampled observations.
    cells = defaultdict(lambda: [0, 0, 0, 0])
    clusters = sorted({r["cluster_id"] for r in rows if r["A_selected"] or r["B_selected"]})
    for r in rows:
        if r["discovery_hit"] is None or not (r["A_selected"] or r["B_selected"]):
            continue
        cell = cells[(r["session"], r["cluster_id"])]
        for offset, arm in ((0, "A"), (2, "B")):
            if r[arm + "_selected"]:
                cell[offset] += 1
                cell[offset + 1] += int(r["discovery_hit"])

    def statistic(day_weights, cluster_weights):
        totals = defaultdict(lambda: [0, 0, 0, 0])
        for (day, cluster), values in cells.items():
            weight = cluster_weights.get(cluster, 0)
            if not weight or not day_weights.get(day, 0):
                continue
            for index, value in enumerate(values):
                totals[day][index] += value * weight
        deltas, weights = [], []
        for day, (an, ah, bn, bh) in sorted(totals.items()):
            if an and bn:
                deltas.append(bh / bn - ah / an)
                weights.append(day_weights[day])
        return (sum(d * w for d, w in zip(deltas, weights)) / sum(weights) if weights else None,
                len(deltas))

    point, paired_days = statistic(dict.fromkeys(days, 1), dict.fromkeys(clusters, 1))
    samples = []
    rng = random.Random(seed)
    if days and clusters and point is not None:
        for _ in range(criteria["bootstrap_samples"]):
            weights_day = Counter(rng.choices(days, k=len(days)))
            weights_cluster = Counter(rng.choices(clusters, k=len(clusters)))
            value, _ = statistic(weights_day, weights_cluster)
            if value is not None:
                samples.append(value)
    tail = (1 - criteria["confidence"]) / (2 * criteria["max_trials"] * len(criteria["stages"]))
    return {"method": "paired_day_x_event_cluster_product_bootstrap_v1", "seed": seed,
            "confidence_familywise": criteria["confidence"], "max_trials": criteria["max_trials"],
            "requested_resamples": criteria["bootstrap_samples"], "valid_resamples": len(samples),
            "paired_days": paired_days, "delta_daily_mean_hit_rate": point,
            "lower": _quantile(samples, tail), "upper": _quantile(samples, 1 - tail)}


def comparison_metrics(rows: list[dict], days: list[str], criteria: dict) -> dict:
    overall = {arm: arm_metrics(rows, arm, days, criteria["cost_scenarios_bps"]) for arm in ("A", "B")}
    stages = {}
    for index, stage in enumerate(criteria["stages"]):
        # Include the SAME entire daily universe in each stage denominator so capture
        # and enrichment do not mistake a non-selected stage for a negative label.
        stage_rows = [{**r, "A_selected": r["A_selected"] and r["stage"] == stage,
                       "B_selected": r["B_selected"] and r["stage"] == stage} for r in rows]
        stages[stage] = {arm: arm_metrics(stage_rows, arm, days, criteria["cost_scenarios_bps"])
                         for arm in ("A", "B")}
        stages[stage]["paired"] = paired_interval(stage_rows, days, criteria, criteria["seed"] + index)
    breakdown = []
    for field in ("market", "regime", "liquidity"):
        for value in sorted({r[field] for r in rows}):
            group = [r for r in rows if r[field] == value]
            breakdown.append({"dimension": field, "value": value,
                              **{arm: arm_metrics(group, arm, days, criteria["cost_scenarios_bps"])
                                 for arm in ("A", "B")}})
    return {"overall": overall, "by_stage": stages, "breakdown": breakdown,
            "day_count": len(days), "cluster_policy": "same instrument linked across days; shared disclosure event groups also linked",
            "regime_policy": "unknown (no contemporaneous market-regime source)",
            "profit_is_proxy_only": True, "actual_trading_return": None}


def decide(metrics: dict, criteria: dict, quality_reasons: list[str]) -> dict:
    insufficient, rejects, improvements = [], [], []
    for stage, group in metrics["by_stage"].items():
        a, b, interval = group["A"], group["B"], group["paired"]
        if interval["paired_days"] < criteria["min_days"]:
            insufficient.append(stage + ":insufficient_paired_days")
        if interval["valid_resamples"] < .9 * criteria["bootstrap_samples"]:
            insufficient.append(stage + ":insufficient_bootstrap_resamples")
        for arm, values in (("A", a), ("B", b)):
            for key, threshold in (("valid", "min_valid"), ("unique_instruments", "min_unique_instruments"),
                                   ("event_clusters", "min_event_clusters")):
                if values[key] < criteria[threshold]:
                    insufficient.append(stage + ":" + arm + ":insufficient_" + key)
            if values["unknown_rate"] is None or values["unknown_rate"] > criteria["max_unknown_rate"]:
                insufficient.append(stage + ":" + arm + ":unknown_rate_exceeded_or_empty")
        delta = interval["delta_daily_mean_hit_rate"]
        improvements.append(delta is not None and delta >= criteria["min_hit_rate_improvement"]
                            and interval["lower"] is not None and interval["lower"] > 0)
        if interval["upper"] is not None and interval["upper"] < criteria["min_hit_rate_improvement"]:
            rejects.append(stage + ":improvement_below_frozen_minimum")
    a, b = metrics["overall"]["A"], metrics["overall"]["B"]
    if a["capture_rate"] is None or b["capture_rate"] is None:
        insufficient.append("capture_rate_unavailable")
    elif b["capture_rate"] < a["capture_rate"] - criteria["max_capture_drop"]:
        rejects.append("capture_rate_deteriorated")
    retention = rate(b["selected"], a["selected"])
    if retention is None:
        insufficient.append("no_baseline_candidates")
    elif retention < criteria["min_candidate_retention"]:
        rejects.append("candidate_retention_below_minimum")
    statistical = "HOLD" if insufficient else "REJECT" if rejects else "ADOPTION_CANDIDATE" if all(improvements) else "HOLD"
    reasons = sorted(set(quality_reasons + insufficient + rejects))
    if statistical == "HOLD" and not reasons:
        reasons.append("improvement_uncertain")
    return {"statistical_decision": statistical,
            "recommendation": "HOLD" if quality_reasons else statistical,
            "reason_codes": reasons, "candidate_retention": retention,
            "release_state": "HUMAN_REVIEW_REQUIRED" if not quality_reasons and statistical == "ADOPTION_CANDIDATE" else "NOT_APPROVED",
            "automatic_adoption": False, "performance_claim_allowed": False}
