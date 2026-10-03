"""Versioned numerical hypothesis vocabulary. Model selects IDs; Python defines meaning."""
from __future__ import annotations

import json

from .common import stable_id

CATALOG_VERSION = "hypothesis-condition-catalog-v1"
CATALOG = {
    "volume_ge_1_5": {"feature": "rel_volume_20d", "operator": ">=", "value": 1.5, "unit": "multiple", "label": "相対出来高が1.5倍以上"},
    "volume_ge_2": {"feature": "rel_volume_20d", "operator": ">=", "value": 2.0, "unit": "multiple", "label": "相対出来高が2倍以上"},
    "near_high_ge_minus_0_05": {"feature": "high_distance_20d", "operator": ">=", "value": -.05, "unit": "ratio", "label": "20日高値からの距離が-5%以上"},
    "not_breakout_le_0": {"feature": "high_distance_20d", "operator": "<=", "value": 0.0, "unit": "ratio", "label": "20日高値をまだ上回っていない"},
    "ret5_le_0_05": {"feature": "ret_5d", "operator": "<=", "value": .05, "unit": "ratio", "label": "5日騰落率が5%以下"},
    "ret5_le_0_12": {"feature": "ret_5d", "operator": "<=", "value": .12, "unit": "ratio", "label": "5日騰落率が12%以下"},
    "ma25_gap_le_0_15": {"feature": "ma25_gap", "operator": "<=", "value": .15, "unit": "ratio", "label": "25日移動平均からの乖離率が15%以下"},
}
REASONS = {"volume_expansion": "上昇前の出来高増加を調べる",
           "near_previous_high": "上昇前の高値への接近を調べる",
           "avoid_overheat": "過熱を避ける条件の効果を調べる"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate hypothesis JSON key")
        result[key] = value
    return result


def validate_plans(content: str, study_id: str, data_grade: str) -> list[dict]:
    try:
        value = json.loads(content, object_pairs_hook=_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite hypothesis JSON")))
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("hypothesis response must be JSON") from error
    if not isinstance(value, dict) or set(value) != {"hypotheses"} or not isinstance(value["hypotheses"], list) or not 1 <= len(value["hypotheses"]) <= 2:
        raise ValueError("one or two structured hypotheses required")
    plans, seen = [], set()
    for proposal in value["hypotheses"]:
        if not isinstance(proposal, dict) or set(proposal) != {"condition_ids", "reason_code"}:
            raise ValueError("free text or unknown hypothesis fields are not allowed")
        ids = proposal["condition_ids"]
        reason = proposal["reason_code"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or any(not isinstance(key, str) or key not in CATALOG for key in ids):
            raise ValueError("unknown or invalid condition IDs")
        if len(set(ids)) != len(ids) or not isinstance(reason, str) or reason not in REASONS:
            raise ValueError("duplicate condition or unknown reason")
        ids = sorted(ids)
        # Multiple bounds on the same feature are permitted, redundant same-direction bounds are not.
        pairs = [(CATALOG[key]["feature"], CATALOG[key]["operator"]) for key in ids]
        if len(pairs) != len(set(pairs)) or tuple(ids) in seen:
            raise ValueError("redundant or duplicated hypothesis")
        reason_features = {"volume_expansion": {"rel_volume_20d"}, "near_previous_high": {"high_distance_20d"},
                           "avoid_overheat": {"ret_5d", "ma25_gap"}}
        if not {feature for feature, _ in pairs}.intersection(reason_features[reason]):
            raise ValueError("reason does not match selected condition features")
        seen.add(tuple(ids))
        plans.append({"hypothesis_id": "H-AI-" + stable_id(study_id, CATALOG_VERSION, ids), "state": "DRAFT",
                      "condition_ids": ids, "conditions": [{"condition_id": key, **CATALOG[key]} for key in ids],
                      "combine": "all", "reason_code": reason, "reason": REASONS[reason],
                      "thesis": " AND ".join(CATALOG[key]["label"] for key in ids),
                      "comparison": "同じ未使用期間の全対象銘柄で、固定した価格基準Aとこの条件のAND選別を比較する。",
                      "metrics": ["discovery_hit_rate", "discovery_capture_rate", "candidate_count", "unknown_rate"],
                      "horizon_sessions": 1, "label": "翌営業日の調整後高値/前営業日終値-1 >= 0.10（基準設定の初期値）",
                      "required_data": "全母集団の日足・分割情報・開示時点・翌日価格。売買評価には費用と約定可否も必要。",
                      "catalog_version": CATALOG_VERSION, "data_grade": data_grade, "basis_study_id": study_id,
                      "thresholds_are_design_defaults": True, "automatic_adoption": False,
                      "test_period": None, "sample_size_and_adoption_criteria": None})
    return sorted(plans, key=lambda p: p["hypothesis_id"])


def render_plans(plans: list[dict]) -> str:
    pieces = []
    for index, plan in enumerate(plans, 1):
        pieces.append(f"### 仮説{index}（未検証・設計初期値）\n\n{plan['thesis']}\n\n"
                      f"目的: {plan['reason']}。\n\n比較: {plan['comparison']}\n\n"
                      "主指標: 翌1営業日の急騰的中率・捕捉率。候補数・未知率を併記。\n\n"
                      "未使用の検証期間、必要件数、採否基準を事前登録してから評価する。\n")
    return "\n".join(pieces)
