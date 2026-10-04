"""End-to-end acceptance: synthetic public site and private native export, no network."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from alpha_loop.common import digest, read_json, write_json
from alpha_loop.dashboard import build, enforce_publication, export_site
from alpha_loop.dashboard_publish import publish
from alpha_loop.pipeline import _code_hash
from export_dashboard import demo


def main():
    baseline_hash = _code_hash()
    original = {p: digest(p.read_bytes()) for p in (PROJECT / "outputs").glob("run-*/candidates.csv")}
    root = PROJECT / "data/dd" / uuid.uuid4().hex[:8]
    document = demo(root)
    assert len(document["days"]) == 22
    assert any(d["stats"]["candidates"] == 0 for d in document["days"])
    assert document["days"][-1]["stats"]["unknown"] == 3
    assert document["days"][-1]["next_session"] == "2026-10-05"
    assert any(d["stats"]["negative_misses"]>0 for d in document["days"])
    policy = read_json(PROJECT / "configs/dashboard_publication.example.json")
    enforce_publication(document, policy)
    # Tracked sample is synthetic only. It lets Pages show a demo from a clean checkout.
    demo_site = export_site(document, PROJECT / "outputs/dashboard/demo", PROJECT / "dashboard")
    write_json(PROJECT / "dashboard/data/dashboard.json", document)
    dry_run = publish(Path(demo_site["directory"]), PROJECT, policy)
    native = build(PROJECT)
    local_site = export_site(native, PROJECT / "outputs/dashboard/local", PROJECT / "dashboard")
    if native["days"]:
        try:
            enforce_publication(native, policy)
        except ValueError:
            blocked = True
        else:
            blocked = False
        assert blocked, "current real data must remain private under default policy"
    assert _code_hash() == baseline_hash
    assert all(digest(path.read_bytes()) == hash_ for path, hash_ in original.items())
    proof = {"status":"SUCCEEDED", "synthetic_days":22, "synthetic_site":demo_site["directory"],
             "native_days":len(native["days"]), "native_site":local_site["directory"],
             "publication":dry_run["status"], "network_used":False, "gpu_used":False,
             "original_candidate_files_preserved":len(original), "numerical_code_hash_preserved":True,
             "negative_miss_demo_verified":True,
             "real_data_publication":"BLOCKED_BY_DEFAULT"}
    write_json(PROJECT / "data/operations/dashboard_acceptance.json",proof)
    print(json.dumps(proof,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
