"""Weekly draft queue and explicit human review, separate from frozen M5 engines."""
from datetime import date
from pathlib import Path

from .common import atomic_bytes, canonical, digest, now_iso, read_json, stable_id, write_csv, write_json
from .hypothesis_plans import CATALOG_VERSION, validate_plans
from .pipeline import _verified_manifest
from .research import _load, _store


def weekly(root: Path, session: str, *, force_draft: bool = False) -> dict:
    from .runtime import exclusive
    with exclusive(root / "data/operations/.weekly.lock"):
        return _weekly(root, session, force_draft=force_draft)


def _weekly(root: Path, session: str, *, force_draft: bool = False) -> dict:
    # This is a hook of the existing weekday job, not a second schedule.
    day = date.fromisoformat(session)
    if day.weekday() != 4 and not force_draft:
        return {"status": "NOT_DUE", "next_policy": "Friday after daily completion", "automatic_registration": False}
    year, week, _ = day.isocalendar()
    week_id = f"{year}-W{week:02d}"
    directory = root / "outputs/weekly_research" / ("previews" if force_draft else "completed") / week_id
    if force_draft:
        directory = directory / session
    if (directory / "manifest.json").exists():
        saved = read_json(directory / "manifest.json")
        for name, hash_ in saved["artifacts"].items():
            if digest((directory / name).read_bytes()) != hash_:
                raise RuntimeError("weekly report artifact hash mismatch")
        return read_json(directory / "summary.json")
    experiments = [_load(root, p.stem) for p in (root / "data/research/experiments").glob("*.json")]
    registered = {tuple(e["condition_ids"]) for e in experiments}
    protected_periods = [e["periods"]["holdout"] for e in experiments if e.get("periods")]
    sessions = {}
    for path in (root / "outputs").glob("run-*/run_manifest.json"):
        m = _verified_manifest(path.parent)
        target = date.fromisoformat(m["target_session"])
        if m["run_kind"] != "baseline" or m["data_grade"] == "synthetic" or target.isocalendar()[:2] != (year, week) or target > day:
            continue
        existing = sessions.get(m["target_session"])
        if existing is None or (m["completed_at"], m["run_id"]) < (existing["completed_at"], existing["run_id"]):
            sessions[m["target_session"]] = m
    proposals, sources, attempted, protected_sessions = {}, [], set(), []
    jobs = [read_json(p) for p in sorted((root / "data/operations/service_jobs").glob("*.json"))]
    for target, m in sorted(sessions.items()):
        if any(period["start"] <= target <= period["end"] for period in protected_periods):
            protected_sessions.append(target)
            continue
        paths = {Path(j["qwen"]["hypotheses"]["report_path"]) for j in jobs if j.get("run_id") == m["run_id"] and j.get("qwen_status") == "SUCCEEDED" and j.get("qwen", {}).get("hypotheses", {}).get("report_path")}
        # One saved discovery response per canonical day; never count repeated jobs twice.
        for path in sorted(paths)[:1]:
            report = read_json(path)
            if not report.get("hypothesis_plans"):
                continue
            if report["data_grade"] != m["data_grade"] or not report.get("structure_validated"):
                raise ValueError("weekly source grade/validation mismatch")
            from .retrospective import _verify
            _, study, _ = _verify(root, report["study_id"])
            if study["ranking_session"] != target:
                raise ValueError("weekly discovery source session mismatch")
            if any(period["start"] <= study["feature_session"] <= period["end"] for period in protected_periods):
                protected_sessions.append(target)
                continue
            raw = {"hypotheses": [{"condition_ids": p["condition_ids"], "reason_code": p["reason_code"]} for p in report["hypothesis_plans"]]}
            validated = validate_plans(canonical(raw).decode(), report["study_id"], report["data_grade"])
            sources.append({"session": target, "report": str(path), "hash": digest(path.read_bytes()), "study_id": report["study_id"]})
            for plan in validated:
                key = tuple(plan["condition_ids"])
                attempted.add(key)
                if key in registered:
                    continue
                if key not in proposals:
                    proposals[key] = {**plan, "occurrences": 0, "source_reports": [], "periods": None,
                                      "registration_required": True, "quality_eligible": False}
                proposals[key]["occurrences"] += 1
                proposals[key]["source_reports"].append(sources[-1])
    drafts = sorted(proposals.values(), key=lambda p: (-p["occurrences"], p["condition_ids"]))[:2]
    summary = {"week_id": week_id, "status": "DRAFTS_READY" if drafts else "NO_NEW_HYPOTHESIS" if sources else "NO_HYPOTHESIS_EVIDENCE",
               "session": session, "source_days": len({s["session"] for s in sources}), "unique_conditions_tried": len(attempted),
               "new_drafts": len(drafts), "preview": force_draft, "catalog_version": CATALOG_VERSION, "automatic_registration": False, "automatic_adoption": False,
               "drafts_path": str(directory / "drafts.json"), "registered_duplicate_conditions": len(attempted & registered),
               "protected_holdout_sessions_skipped": sorted(set(protected_sessions)),
               "note": "saved discovery reports only; no holdout outcomes loaded; freeze new unused periods before comparison"}
    write_json(directory / "drafts.json", drafts)
    write_json(directory / "sources.json", sources)
    write_json(directory / "summary.json", summary)
    columns = ["hypothesis_id", "condition_ids", "reason_code", "occurrences", "registration_required"]
    write_csv(directory / "drafts.csv", columns, drafts)
    atomic_bytes(directory / "report.md", (f"# 週次仮説案 {week_id}\n\n状態: {summary['status']} / 参照日: {summary['source_days']} / 新規案: {len(drafts)}\n\n"
                 "仮説案は未検証。既存の登録条件は重複提案しない。新しい未使用期間と採否基準を登録し、人手確認後に採用する。\n").encode())
    write_json(directory / "manifest.json", {"completed_at": now_iso(), "artifacts": {name: digest((directory / name).read_bytes()) for name in ("drafts.json", "sources.json", "summary.json", "drafts.csv", "report.md")}})
    return summary


def record_review(root: Path, experiment_id: str, decision: str, reviewer: str, reason: str, human_reviewed: bool) -> dict:
    if decision not in ("HOLD", "REJECT", "APPROVE_SHADOW") or human_reviewed is not True or not reviewer.strip() or not reason.strip():
        raise ValueError("explicit human review, reviewer and reason required")
    spec = _load(root, experiment_id)
    con = _store(root)
    try:
        row = con.execute("SELECT report_id,manifest_hash FROM m5_reports WHERE experiment_id=?", (experiment_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("completed comparison required for review")
    directory = root / "outputs/research" / experiment_id / row[0]
    manifest = read_json(directory / "manifest.json")
    if digest((directory / "manifest.json").read_bytes()) != row[1]:
        raise RuntimeError("review comparison manifest hash mismatch")
    for name, hash_ in manifest["artifacts"].items():
        if digest((directory / name).read_bytes()) != hash_:
            raise RuntimeError("review comparison artifact hash mismatch")
    summary = read_json(directory / "summary.json")
    if decision == "APPROVE_SHADOW" and (summary["recommendation"] != "ADOPTION_CANDIDATE" or spec["mode"] != "prospective"):
        raise ValueError("synthetic/research/HOLD results cannot approve a production shadow release")
    receipt = {"experiment_id": experiment_id, "report_id": row[0], "report_manifest_hash": row[1],
               "decision": decision, "reviewer": reviewer, "human_reviewed": True, "reason": reason,
               "baseline_config_hash": spec["baseline_config_hash"], "conditions": spec["conditions"],
               "automatic_activation": False, "production_config_changed": False}
    review_id = "review-" + stable_id(receipt)
    path = root / "data/research/reviews" / (review_id + ".json")
    if path.exists():
        previous = read_json(path)
        if previous["checksum"] != digest(canonical({k: v for k, v in previous.items() if k != "checksum"})):
            raise RuntimeError("human review receipt hash mismatch")
        return previous
    receipt.update(review_id=review_id, reviewed_at=now_iso())
    if decision == "APPROVE_SHADOW":
        release = {"release_id": "release-" + stable_id(experiment_id, row[0], review_id), "state": "APPROVED_SHADOW",
                   "baseline_config": spec["baseline_config"], "conditions": spec["conditions"], "combine": "all",
                   "comparison_report": str(directory), "review_id": review_id, "live_enabled": False}
        release_path = root / "data/research/releases" / (release["release_id"] + ".json")
        write_json(release_path, release)
        receipt["release_path"] = str(release_path)
    receipt["checksum"] = digest(canonical(receipt))
    write_json(path, receipt)
    con = _store(root)
    try:
        con.execute("INSERT INTO hypothesis_events VALUES(?,?,?,?,?)", (spec["hypothesis_id"], spec["version"], receipt["reviewed_at"], "HUMAN_REVIEW", canonical({"review_id": review_id, "decision": decision, "reason": reason}).decode()))
        con.commit()
    finally:
        con.close()
    return receipt
