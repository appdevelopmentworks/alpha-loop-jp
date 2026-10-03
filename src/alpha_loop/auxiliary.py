"""Three/five-session descriptive price outcomes; never claimed trade returns."""
import math
from pathlib import Path

from .common import digest, read_json, stable_id, write_csv, write_json
from .pipeline import _verified_manifest, _verified_snapshot
from .provider import FileProvider

VERSION = "auxiliary-price-v1"


def completed(root: Path, run_id: str) -> bool:
    for path in sorted((root / "outputs" / run_id / "auxiliary").glob("*/manifest.json")):
        saved = read_json(path)
        for name, hash_ in saved["artifacts"].items():
            if digest((path.parent / name).read_bytes()) != hash_:
                raise RuntimeError("auxiliary artifact hash mismatch")
        if read_json(path.parent / "summary.json").get("horizons_complete"):
            return True
    return False


def evaluate_auxiliary(root: Path, run_id: str, input_dir: Path, through: str) -> dict:
    from .service import _future
    manifest = _verified_manifest(root / "outputs" / run_id)
    snapshot = _verified_snapshot(root, manifest["snapshot_id"])
    data, _ = FileProvider(input_dir).load()
    if data["source"]["data_grade"] != snapshot["data_grade"]:
        raise ValueError("auxiliary input grade mismatch")
    calendar = data["calendar"]
    index = calendar.index(manifest["target_session"])
    if through not in calendar or calendar.index(through) < index:
        raise ValueError("invalid auxiliary through session")
    key = stable_id(VERSION, run_id, through, {n: digest((input_dir / (n + ".json")).read_bytes()) for n in ("calendar", "bars", "source", "instruments", "disclosures")})
    output = root / "outputs" / run_id / "auxiliary" / key
    if (output / "manifest.json").exists():
        saved = read_json(output / "manifest.json")
        for name, hash_ in saved["artifacts"].items():
            if digest((output / name).read_bytes()) != hash_:
                raise RuntimeError("auxiliary artifact hash mismatch")
        return read_json(output / "summary.json")
    future = _future(root, run_id, data, through, output / "inputs")
    by_key = {(b["instrument_id"], b["session_date"]): b for b in read_json(future / "future.json")["bars"]}
    prices = {b["instrument_id"]: b for b in snapshot["data"]["bars"] if b["session_date"] == manifest["target_session"]}
    rows = []
    for decision in read_json(root / "outputs" / run_id / "decisions.json"):
        iid = decision["instrument_id"]
        for horizon in (3, 5):
            days = calendar[index + 1:index + horizon + 1]
            row = {"instrument_id": iid, "stage": decision["stage"], "selected": decision["selected"], "horizon_sessions": horizon,
                   "outcome_status": "horizon_incomplete", "high_return": None, "close_return": None,
                   "execution_status": "unknown", "trade_return": None, "prediction_eligible": False}
            if len(days) == horizon and days[-1] <= through:
                row["outcome_status"] = "unknown"
                base = prices.get(iid, {}).get("adj_close")
                bars = [by_key.get((iid, day)) for day in days]
                usable = base and all(b and b["bar_status"] == "ok" and b.get("adjustment_basis") == "split_only" and b.get("prior_close_rebase_factor") and all(isinstance(b.get(k), (int, float)) and math.isfinite(b[k]) and b[k] > 0 for k in ("adj_high", "adj_close")) for b in bars)
                if usable:
                    row.update(outcome_status="observed_price_only", high_return=max(b["adj_high"] / (base * b["prior_close_rebase_factor"]) - 1 for b in bars),
                               close_return=bars[-1]["adj_close"] / (base * bars[-1]["prior_close_rebase_factor"]) - 1)
            rows.append(row)
    write_csv(output / "outcomes.csv", list(rows[0]) if rows else ["instrument_id", "horizon_sessions", "outcome_status"], rows)
    summary = {"status": "SAVED", "version": VERSION, "run_id": run_id, "through": through, "data_grade": manifest["data_grade"],
               "horizons_complete": len(calendar) > index + 5 and calendar[index + 5] <= through,
               "outcomes_csv": str(output / "outcomes.csv"), "rows": len(rows), "known_rows": sum(r["outcome_status"] == "observed_price_only" for r in rows),
               "note": "descriptive auxiliary only; no independent significance claim; not trade revenue; primary M5 remains next session"}
    write_json(output / "summary.json", summary)
    write_json(output / "manifest.json", {"artifacts": {name: digest((output / name).read_bytes()) for name in ("outcomes.csv", "summary.json", "inputs/future.json")}})
    return summary
