"""Saved-response B1 filter / B2 ranking previews, never active rules."""
from .common import digest, read_json, stable_id, write_csv, write_json
from .pipeline import _verified_manifest

VERSION = "material-b1-b2-preview-v1"


def preview(root, run_id, documents_path):
    manifest = _verified_manifest(root / "outputs" / run_id)
    material_manifest = read_json(documents_path.parent / "manifest.json")
    for name, hash_ in material_manifest["artifacts"].items():
        if digest((documents_path.parent / name).read_bytes()) != hash_:
            raise RuntimeError("material preview input hash mismatch")
    docs = read_json(documents_path)
    decisions = read_json(root / "outputs" / run_id / "decisions.json")
    rows = []
    for d in decisions:
        available = [v for v in docs if v["instrument_id"] == d["instrument_id"] and v["semantic_status"] == "ok"]
        accepted = []
        for v in available:
            a = v["answers"]
            if a["material_type"]["choice"] == "contract" and a["contract_stage"]["choice"] == "formal_contract" and a["dilution"]["probability_yes"] < .5:
                accepted.append(sum(a[k]["probability_yes"] for k in ("core_business", "guidance_timing", "explicit_amount")) / 3)
        row = {"instrument_id": d["instrument_id"], "stage": d["stage"], "A_selected": d["selected"],
               "material_status": "known" if available else "unknown", "material_pass": bool(accepted) if available else None,
               "material_score": max(accepted) if accepted else None, "B1_selected": bool(d["selected"] and accepted), "B2_selected": False,
               "reason": "formal_contract_without_dilution" if accepted else "material_condition_failed" if available else "missing_or_unvalidated_response"}
        rows.append(row)
    k = manifest["config"]["max_candidates_per_stage"] if "config" in manifest else None
    if k is None:
        from .pipeline import _verified_snapshot
        k = _verified_snapshot(root, manifest["snapshot_id"])["config"]["max_candidates_per_stage"]
    for stage in ("EARLY", "PREMOVE"):
        pool = sorted([r for r in rows if r["stage"] == stage and r["material_pass"]], key=lambda r: (-r["material_score"], r["instrument_id"]))
        for r in pool[:k]:
            r["B2_selected"] = True
    output = root / "outputs" / run_id / "material_experiments" / stable_id(VERSION, digest(documents_path.read_bytes()))
    if (output / "manifest.json").exists():
        saved = read_json(output / "manifest.json")
        for name, hash_ in saved["artifacts"].items():
            if digest((output / name).read_bytes()) != hash_:
                raise RuntimeError("material preview artifact hash mismatch")
        return read_json(output / "summary.json")
    write_csv(output / "selections.csv", list(rows[0]) if rows else ["instrument_id"], rows)
    result = {"status": "PREVIEW_ONLY", "version": VERSION, "run_id": run_id, "data_grade": manifest["data_grade"], "quality_gate": "NOT_VALIDATED",
              "A_count": sum(r["A_selected"] for r in rows), "B1_count": sum(r["B1_selected"] for r in rows), "B2_count": sum(r["B2_selected"] for r in rows),
              "selections_csv": str(output / "selections.csv"), "active": False, "score_is_surge_probability": False,
              "note": "design defaults; freeze prospective periods/criteria and human labels before adoption; B2 only existing price-qualified stages"}
    write_json(output / "summary.json", result)
    write_json(output / "manifest.json", {"artifacts": {n: digest((output / n).read_bytes()) for n in ("selections.csv", "summary.json")}})
    return result
