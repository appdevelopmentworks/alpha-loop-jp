"""Local OpenJev transport/evidence smoke on synthetic text; not real accuracy."""
from pathlib import Path
import json
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from alpha_loop.common import now_iso, read_json, write_json
from alpha_loop.materials import infer, verified_document
from alpha_loop.model_session import local_model
from alpha_loop.models import model_status
from alpha_loop.semantic import OpenJevProvider


def main():
    acceptance = read_json(PROJECT / "data/operations/remaining_acceptance.json")
    root = Path(acceptance["root"])
    doc = next(verified_document(root, p) for p in (root / "data/materials/documents").glob("*.json") if read_json(p)["disclosure_id"] == "fixture-positive")
    runtime = read_json(PROJECT / "configs/openjev_runtime_rtx5090_v1.json")
    provider = OpenJevProvider("http://127.0.0.1:8080", "openjev-0.1", runtime["model_revision"], runtime["base_image_digest"] + "+topk50", "NVFP4-marlin", runtime, 60)
    before = model_status()
    began = time.monotonic()
    try:
        with local_model(root, "openjev", 240):
            result = infer(root, doc, provider, PROJECT / "configs/questions_v1.json")
        status = result["semantic_status"]
        receipt = {"verified_at": now_iso(), "root": str(root), "data_grade": "synthetic", "status": status,
                   "before": before, "after": model_status(), "elapsed_seconds": time.monotonic() - began,
                   "response": result, "real_material_accuracy_validated": False}
        receipt["container_states_restored"] = receipt["before"]["containers"] == receipt["after"]["containers"]
        write_json(PROJECT / "data/operations/remaining_gpu_acceptance.json", receipt)
        print(json.dumps({k: receipt[k] for k in ("status", "elapsed_seconds", "container_states_restored", "real_material_accuracy_validated")}, ensure_ascii=False))
        if status not in ("ok", "missing_evidence") or not receipt["container_states_restored"]:
            raise SystemExit(2)
    except Exception as error:
        write_json(PROJECT / "data/operations/remaining_gpu_acceptance.json", {"status": "FAILED", "before": before,
                   "error": str(error), "elapsed_seconds": time.monotonic() - began, "data_grade": "synthetic", "real_material_accuracy_validated": False})
        raise


if __name__ == "__main__":
    main()
