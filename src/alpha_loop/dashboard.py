"""Read-only, allowlisted dashboard exports. No market fetch, model call or scoring change."""
from __future__ import annotations

import re
from pathlib import Path

from .common import canonical, digest, read_json, write_json
from .pipeline import _verified_manifest, _verified_snapshot

VERSION = "dashboard-v1"
GRADES = {"synthetic", "reconstructed", "observed", "vendor_pit"}
RUN = re.compile(r"run-[0-9a-f]{20}\Z")


def _evaluation(directory: Path, run_id: str) -> tuple[dict | None, list[dict]]:
    available = []
    known = {}
    for path in sorted((directory / "evaluation").glob("*/evaluation_manifest.json")):
        manifest = read_json(path)
        if manifest["source_run_id"] != run_id or path.parent.name != manifest["evaluation_id"]:
            raise ValueError("evaluation identity mismatch")
        for name in ("outcomes.json", "outcomes.csv", "metrics.json"):
            if digest((path.parent / name).read_bytes()) != manifest["artifacts"][name]:
                raise ValueError("dashboard evaluation artifact hash mismatch")
        rows = read_json(path.parent / "outcomes.json")
        ids = [row["decision_id"] for row in rows]
        if len(ids) != len(set(ids)) or any(row["source_run_id"] != run_id for row in rows):
            raise ValueError("duplicate or foreign outcome")
        for row in rows:
            if row["outcome_status"] == "ok":
                if not isinstance(row["discovery_hit"], bool):
                    raise ValueError("known outcome needs boolean hit")
                fact = (row["discovery_hit"], row["discovery_return"])
                if row["decision_id"] in known and known[row["decision_id"]] != fact:
                    raise ValueError("conflicting archived evaluations; review required")
                known[row["decision_id"]] = fact
        available.append((manifest, rows))
    if not available:
        return None, []
    # Deterministic latest saved evaluation; never choose the highest hit rate.
    return max(available, key=lambda item: (item[0]["through"], item[0]["evaluation_id"]))


def summarize(candidates: list[dict]) -> dict:
    valid = [row for row in candidates if isinstance(row["hit"], bool)]
    hits = sum(row["hit"] for row in valid)
    return {"candidates": len(candidates), "evaluated": len(valid), "hits": hits,
            "misses": len(valid) - hits, "unknown": len(candidates) - len(valid),
            "hit_rate": hits / len(valid) if valid else None}


def build(root: Path, *, run_ids: list[str] | None = None, days: int = 90) -> dict:
    """Native mode reads service receipts only, excluding standalone experiments/replays."""
    if not 1 <= days <= 366:
        raise ValueError("dashboard retention must be 1..366 calendar days")
    receipts = {}
    if run_ids is None:
        for path in sorted((root / "data/operations/service_jobs").glob("*.json")):
            receipt = read_json(path)
            if receipt.get("status", "").startswith("SUCCEEDED"):
                receipts[receipt["run_id"]] = receipt
        run_ids = list(receipts)
    from datetime import date, timedelta
    selected = {}
    for run_id in sorted(set(run_ids)):
        if not RUN.fullmatch(run_id):
            raise ValueError("invalid dashboard run id")
        directory = root / "outputs" / run_id
        manifest = _verified_manifest(directory)
        if manifest is None or manifest["run_kind"] != "baseline":
            raise ValueError("dashboard requires a completed baseline run")
        if manifest["data_grade"] not in GRADES:
            raise ValueError("unknown data grade")
        key = (manifest["target_session"], manifest["data_grade"], manifest["strategy_id"],
               manifest["strategy_version"], manifest["config"]["discovery_hit_threshold"])
        # First sealed decision per session/cohort: retries must not double count.
        if key not in selected or (manifest["started_at"], run_id) < (selected[key]["started_at"], selected[key]["run_id"]):
            selected[key] = manifest
    manifests = list(selected.values())
    if manifests:
        cutoff = date.fromisoformat(max(m["target_session"] for m in manifests)) - timedelta(days=days - 1)
        manifests = [m for m in manifests if date.fromisoformat(m["target_session"]) >= cutoff]
    result = []
    proofs = {}
    for manifest in sorted(manifests, key=lambda m: (m["target_session"], m["run_id"])):
        run_id = manifest["run_id"]
        directory = root / "outputs" / run_id
        snapshot = _verified_snapshot(root, manifest["snapshot_id"])
        evaluation, outcomes = _evaluation(directory, run_id)
        if evaluation and evaluation["data_grade"] != manifest["data_grade"]:
            raise ValueError("evaluation data grade differs")
        outcome_map = {row["decision_id"]: row for row in outcomes}
        decisions = read_json(directory / "decisions.json")
        if len({d["decision_id"] for d in decisions}) != len(decisions):
            raise ValueError("duplicate decision")
        candidate_rows = [row for row in decisions if row["selected"]]
        if len(candidate_rows) != manifest["candidate_count"]:
            raise ValueError("candidate count mismatch")
        instruments = {row["instrument_id"]: row for row in snapshot["data"]["instruments"]}
        candidates = []
        for row in candidate_rows:
            outcome = outcome_map.get(row["decision_id"])
            if evaluation and outcome is None:
                raise ValueError("evaluation missing a selected candidate")
            if outcome and outcome["instrument_id"] != row["instrument_id"]:
                raise ValueError("outcome instrument differs")
            hit = outcome["discovery_hit"] if outcome and outcome["outcome_status"] == "ok" else None
            candidates.append({"symbol": row["symbol"], "name": row["name"], "stage": row["stage"],
                               "rank": row["rank"], "market": instruments[row["instrument_id"]]["market"],
                               "hit": hit, "outcome_status": outcome["outcome_status"] if outcome else "pending",
                               "high_return": outcome["discovery_return"] if hit is not None else None,
                               "execution_status": outcome["execution_status"] if outcome else "unknown"})
        next_days = [d for d in snapshot["data"]["calendar"] if d > manifest["target_session"]]
        receipt = receipts.get(run_id, {})
        result.append({"session": manifest["target_session"], "next_session": next_days[0] if next_days else None,
                       "as_of": manifest["as_of"], "completed_at": manifest["completed_at"],
                       "data_grade": manifest["data_grade"], "strategy_id": manifest["strategy_id"],
                       "strategy_version": manifest["strategy_version"],
                       "threshold": manifest["config"]["discovery_hit_threshold"],
                       "prediction_eligible": (not manifest["history_only"] and manifest["data_grade"] in ("observed", "vendor_pit")
                                               and (evaluation is None or evaluation["prediction_score_eligible"])),
                       "history_only": manifest["history_only"],
                       "batch_status": receipt.get("status", "SUCCEEDED"),
                       "missing_target_count": receipt.get("missing_target_count", 0),
                       "result_saved": evaluation is not None,
                       "evaluated_at": max((o["evaluated_at"] for o in outcomes), default=None),
                       "evaluated_through": evaluation["through"] if evaluation else None,
                       "stats": summarize(candidates), "candidates": candidates})
        proofs[run_id] = {"manifest_hash": digest((directory / "run_manifest.json").read_bytes()),
                          "evaluation_id": evaluation["evaluation_id"] if evaluation else None}
    # Public document includes only explicit fields above, never receipts, original paths or model text.
    document = {"schema_version": 1, "export_version": VERSION, "scope": "price_baseline",
                "updated_at": max([d["completed_at"] for d in result] +
                                  [d["evaluated_at"] for d in result if d["evaluated_at"]], default=None),
                "days": result}
    canonical(document)  # Reject non-finite values before replacement.
    write_json(root / "data/operations/dashboard/export_proof.json", {"export_hash": digest(canonical(document)),
                                                                     "runs": proofs})
    return document


def enforce_publication(document: dict, policy: dict) -> None:
    validate_document(document)
    grades = {day["data_grade"] for day in document["days"]}
    if policy.get("schema_version") != 1 or not grades <= set(policy.get("allowed_data_grades", [])):
        raise ValueError("data grade not allowed for publication")
    if grades - {"synthetic"} and (policy.get("rights_confirmed") is not True or not policy.get("rights_note", "").strip()):
        raise ValueError("real data publication requires documented rights confirmation")


def validate_document(document: dict) -> None:
    """Reject accidentally attached private fields at every publication boundary."""
    day_keys = {"session", "next_session", "as_of", "completed_at", "data_grade", "strategy_id",
                "strategy_version", "threshold", "prediction_eligible", "history_only", "batch_status",
                "missing_target_count", "result_saved", "evaluated_at", "evaluated_through", "stats", "candidates"}
    row_keys = {"symbol", "name", "stage", "rank", "market", "hit", "outcome_status", "high_return", "execution_status"}
    if set(document) != {"schema_version", "export_version", "scope", "updated_at", "days"} or document["schema_version"] != 1 or document["scope"] != "price_baseline":
        raise ValueError("unexpected dashboard schema or fields")
    for day in document["days"]:
        if set(day) != day_keys or day["data_grade"] not in GRADES:
            raise ValueError("unexpected dashboard day fields")
        for row in day["candidates"]:
            if set(row) != row_keys or row["hit"] is not None and not isinstance(row["hit"], bool):
                raise ValueError("unexpected dashboard candidate fields")
        if day["stats"] != summarize(day["candidates"]):
            raise ValueError("dashboard aggregate differs from candidates")
    canonical(document)


def export_site(document: dict, destination: Path, assets: Path) -> dict:
    from .common import atomic_bytes
    validate_document(document)
    for name in ("index.html", "style.css", "app.js", ".nojekyll"):
        atomic_bytes(destination / name, (assets / name).read_bytes())
    write_json(destination / "data/dashboard.json", document)
    return {"status": "SAVED", "directory": str(destination), "days": len(document["days"]),
            "export_hash": digest(canonical(document)), "scope": "price_baseline", "network_used": False}


def after_daily(root: Path) -> dict:
    """Called by the Hermes wrapper; private export always, remote push explicitly opt-in."""
    from .dashboard_publish import publish
    document = build(root)
    local = export_site(document, root / "outputs/dashboard/local", root / "dashboard")
    policy_path = root / "configs/dashboard_publication.local.json"
    if not policy_path.exists():
        return {**local, "publication": "DISABLED", "reason": "local publication settings not supplied"}
    policy = read_json(policy_path)
    if policy.get("auto_publish") is not True:
        return {**local, "publication": "DISABLED"}
    enforce_publication(document, policy)
    site = root / "outputs/dashboard/public"
    export_site(document, site, root / "dashboard")
    return {**local, "publication": publish(site, root, policy, push=True)}
