from __future__ import annotations

from decimal import Decimal
from statistics import mean
from .common import now_iso

OUTCOME_FIELDS = "decision_id source_run_id instrument_id horizon_sessions evaluation_version evaluated_at outcome_status discovery_return discovery_hit open_close_proxy_return roundtrip_cost_bps open_close_net_proxy_return execution_status forward_max_return forward_min_return price_basis data_grade".split()


def evaluate(snapshot: dict, decisions: list[dict], future: dict, config: dict, through: str) -> tuple[list[dict], dict]:
    calendar = future["calendar"]
    session = snapshot["target_session"]
    next_days = [d for d in calendar if d > session and d <= through]
    future_bars = {(b["instrument_id"], b["session_date"]): b for b in future["bars"]}
    prior = {(b["instrument_id"], b["session_date"]): b for b in snapshot["data"]["bars"]}
    rows = []
    for decision in decisions:
        iid = decision["instrument_id"]
        result = {key: None for key in OUTCOME_FIELDS}
        result.update(decision_id=decision["decision_id"], source_run_id=decision["run_id"], instrument_id=iid,
                      horizon_sessions=1, evaluation_version=config["evaluation_version"], evaluated_at=now_iso(),
                      price_basis="split_only", data_grade=snapshot["data_grade"], execution_status="unknown")
        if not next_days:
            result["outcome_status"] = "horizon_incomplete"
        else:
            day = next_days[0]
            bar = future_bars.get((iid, day))
            base = prior.get((iid, session))
            if bar is None or base is None or bar.get("bar_status") != "ok" or base.get("bar_status") != "ok":
                result["outcome_status"] = "price_unknown"
            elif bar.get("adjustment_basis") != "split_only" or base.get("adjustment_basis") != "split_only":
                result["outcome_status"] = "adjustment_unknown"
            elif not bar.get("adj_high") or not base.get("adj_close"):
                result["outcome_status"] = "price_unknown"
            else:
                factor = bar.get("prior_close_rebase_factor")
                if factor is None or factor <= 0:
                    result["outcome_status"] = "adjustment_unknown"
                    rows.append(result)
                    continue
                dec = lambda value: Decimal(str(value))
                comparison_close = dec(base["adj_close"]) * dec(factor)
                high_return_exact = dec(bar["adj_high"]) / comparison_close - 1
                high_return = float(high_return_exact)
                result.update(outcome_status="ok", discovery_return=high_return,
                              discovery_hit=high_return_exact >= dec(config["discovery_hit_threshold"]),
                              forward_max_return=high_return,
                              forward_min_return=float(dec(bar["adj_low"]) / comparison_close - 1))
                if bar.get("execution_status") in ("halted", "limit_up", "unfilled", "unknown"):
                    result["execution_status"] = bar["execution_status"]
                elif bar.get("adj_open") and bar.get("adj_close"):
                    result["execution_status"] = "proxy_only"
                    gross = bar["adj_close"] / bar["adj_open"] - 1
                    result["open_close_proxy_return"] = gross
                    cost = config["roundtrip_cost_bps"]
                    result["roundtrip_cost_bps"] = cost
                    if cost is None:
                        result["execution_status"] = "cost_unset"
                    else:
                        result["open_close_net_proxy_return"] = gross - cost / 10000
        rows.append(result)
    selected = [(d, o) for d, o in zip(decisions, rows) if d["selected"]]
    by_stage = {}
    for stage in ("EARLY", "PREMOVE", "WATCH"):
        group = [o for d, o in selected if d["stage"] == stage]
        valid = [o for o in group if o["discovery_hit"] is not None]
        hit = sum(bool(o["discovery_hit"]) for o in valid)
        n, unknown = len(valid), len(group) - len(valid)
        by_stage[stage] = {"selected": len(group), "valid": n, "unknown": unknown, "hits": hit,
                           "hit_rate": hit / n if n else None,
                           "unknown_as_failure": hit / len(group) if group else None,
                           "unknown_as_success": (hit + unknown) / len(group) if group else None}
    all_valid = [o for o in rows if o["discovery_hit"] is not None]
    universe_hits = sum(bool(o["discovery_hit"]) for o in all_valid)
    selected_valid = [o for _, o in selected if o["discovery_hit"] is not None]
    captured = sum(bool(o["discovery_hit"]) for o in selected_valid)
    universe_rate = universe_hits / len(all_valid) if all_valid else None
    selected_rate = captured / len(selected_valid) if selected_valid else None
    selected_gross = [o["open_close_proxy_return"] for _, o in selected if o["open_close_proxy_return"] is not None]
    selected_net = [o["open_close_net_proxy_return"] for _, o in selected if o["open_close_net_proxy_return"] is not None]
    metrics = {"by_stage": by_stage, "universe_valid": len(all_valid), "universe_unknown": len(rows)-len(all_valid),
               "universe_hits": universe_hits, "universe_hit_rate": universe_rate,
               "capture_rate": captured / universe_hits if universe_hits else None,
               "enrichment": selected_rate / universe_rate if selected_rate is not None and universe_rate else None,
               "false_positives": sum(o["discovery_hit"] is False for o in selected_valid),
               "trade_proxy_count": len(selected_gross), "mean_open_close_proxy_return": mean(selected_gross) if selected_gross else None,
               "net_proxy_count": len(selected_net), "mean_open_close_net_proxy_return": mean(selected_net) if selected_net else None,
               "data_grade": snapshot["data_grade"], "performance_claim_allowed": False}
    return rows, metrics
