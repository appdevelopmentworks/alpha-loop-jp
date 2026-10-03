"""Human-label comparison for material analysis, never stock-return validation."""
from collections import Counter
from pathlib import Path

from .common import canonical, digest, now_iso, read_json, stable_id, write_csv, write_json
from .materials import verified_document

CASE_TAGS = {"positive", "negative", "ambiguous", "multiple_issuers", "revision", "duplicate", "missing", "injection"}
DEFAULT_CRITERIA = {"min_documents": 100, "min_known_per_question": 50, "min_case_count": 5,
                    "min_accuracy": .9, "min_evidence_match": .9, "max_invalid_rate": .05}


def register_labels(root: Path, path: Path, question_path: Path) -> dict:
    value, questions = read_json(path), read_json(question_path)["questions"]
    if value.get("schema_version") != 1 or value.get("label_origin") not in ("human", "synthetic_fixture") or not value.get("labels"):
        raise ValueError("human or explicit synthetic gold labels required; AI labels are not ground truth")
    seen, docs = set(), []
    for row in value["labels"]:
        if row["document_id"] in seen or not row.get("annotator") or row.get("permission_confirmed") is not True:
            raise ValueError("duplicate label/annotator/permission invalid")
        seen.add(row["document_id"])
        doc = verified_document(root, root / "data/materials/documents" / (row["document_id"] + ".json"))
        if row.get("content_hash") != doc["content_hash"] or set(row["answers"]) != set(questions):
            raise ValueError("gold source hash/question set mismatch")
        if not isinstance(row.get("case_tags"), list) or not row["case_tags"] or not set(row["case_tags"]) <= CASE_TAGS:
            raise ValueError("explicit quality case tags required")
        if (doc["data_grade"] == "synthetic") != (value["label_origin"] == "synthetic_fixture"):
            raise ValueError("synthetic labels cannot validate real material quality")
        if set(row.get("evidence_ids", {})) != set(questions):
            raise ValueError("per-question gold evidence required")
        valid_ids = {p["id"] for p in doc["paragraphs"]}
        for key, answer in row["answers"].items():
            if questions[key]["type"] == "noul":
                if answer not in (True, False, "unknown") or isinstance(answer, (int, float)) and not isinstance(answer, bool):
                    raise ValueError("gold binary answer must be boolean or unknown")
            elif answer != "unknown" and answer not in questions[key]["criteria"]:
                raise ValueError("invalid gold choice")
            ids = row["evidence_ids"][key]
            if not isinstance(ids, list) or not set(ids) <= valid_ids:
                raise ValueError("invalid gold evidence paragraph")
            if answer != "unknown" and doc["status"] == "ready" and not ids:
                raise ValueError("known gold label requires source evidence")
        docs.append(doc)
    spec = {"label_set": value, "question_set": read_json(question_path), "question_hash": digest(question_path.read_bytes()),
            "criteria": DEFAULT_CRITERIA, "registered_at": now_iso(),
            "document_record_hashes": {d["document_id"]: d["record_hash"] for d in docs},
            "data_grade": "synthetic" if value["label_origin"] == "synthetic_fixture" else "real_materials",
            "quality_claim": "human annotations asserted by input; adjudication and independence require review"}
    key = "gold-" + stable_id(value, spec["question_hash"], DEFAULT_CRITERIA)
    target = root / "data/materials/gold" / (key + ".json")
    if target.exists():
        saved = read_json(target)
        if saved.get("checksum") != digest(canonical({k: v for k, v in saved.items() if k != "checksum"})):
            raise RuntimeError("gold registration hash mismatch")
        return {"gold_id": key, "path": str(target)}
    spec["gold_id"] = key
    spec["checksum"] = digest(canonical(spec))
    write_json(target, spec)
    return {"gold_id": key, "path": str(target)}


def evaluate_quality(root: Path, gold_id: str, documents_path: Path) -> dict:
    spec = read_json(root / "data/materials/gold" / (gold_id + ".json"))
    if spec["checksum"] != digest(canonical({k: v for k, v in spec.items() if k != "checksum"})):
        raise RuntimeError("gold registration hash mismatch")
    output_manifest = read_json(documents_path.parent / "manifest.json")
    for name, hash_ in output_manifest["artifacts"].items():
        if digest((documents_path.parent / name).read_bytes()) != hash_:
            raise RuntimeError("quality input artifact hash mismatch")
    results = read_json(documents_path)
    if len({r["document_id"] for r in results}) != len(results):
        raise ValueError("duplicate material prediction")
    indexed = {r["document_id"]: r for r in results}
    questions, samples, tags = spec["question_set"]["questions"], [], Counter()
    for label in spec["label_set"]["labels"]:
        doc = verified_document(root, root / "data/materials/documents" / (label["document_id"] + ".json"))
        if doc["record_hash"] != spec["document_record_hashes"][doc["document_id"]]:
            raise RuntimeError("gold document changed")
        prediction = indexed.get(doc["document_id"], {})
        if prediction.get("identity") and prediction["identity"]["question_hash"] != digest(canonical(spec["question_set"])):
            raise ValueError("quality prediction question mismatch")
        tags.update(label["case_tags"])
        for key, definition in questions.items():
            truth = label["answers"][key]
            value = (prediction.get("answers") or {}).get(key)
            predicted = "unknown" if not value else value.get("choice", value.get("probability_yes", 0) >= .5)
            evidence_id = prediction.get("evidence", {}).get(key, {}).get("paragraph_id")
            matched = evidence_id in label["evidence_ids"][key] if truth != "unknown" else None
            samples.append({"document_id": doc["document_id"], "question": key, "truth": truth, "prediction": predicted,
                            "known_truth": truth != "unknown", "answered": predicted != "unknown", "correct": predicted == truth if truth != "unknown" else None,
                            "evidence_match": matched, "status": prediction.get("semantic_status", "unavailable"),
                            "brier": (value["probability_yes"] - int(truth)) ** 2 if definition["type"] == "noul" and isinstance(truth, bool) and value else None})
    metrics = {}
    for key in questions:
        rows = [r for r in samples if r["question"] == key]
        known = [r for r in rows if r["known_truth"]]
        answered = [r for r in known if r["answered"]]
        confusion = Counter(f"{r['truth']}->{r['prediction']}" for r in rows)
        per_class = {}
        for value in sorted({str(r["truth"]) for r in known}):
            tp = sum(str(r["truth"]) == value and str(r["prediction"]) == value for r in known)
            n_pred = sum(str(r["prediction"]) == value for r in known)
            n_true = sum(str(r["truth"]) == value for r in known)
            per_class[value] = {"precision": tp / n_pred if n_pred else None, "recall": tp / n_true if n_true else None}
        briers = [r["brier"] for r in rows if r["brier"] is not None]
        metrics[key] = {"total": len(rows), "known_truth": len(known), "answered_known": len(answered),
                        "accuracy_all_known": sum(r["correct"] for r in known) / len(known) if known else None,
                        "accuracy_among_answered": sum(r["correct"] for r in answered) / len(answered) if answered else None,
                        "unknown_truth": len(rows) - len(known), "abstentions": sum(not r["answered"] for r in rows),
                        "evidence_match_rate": sum(r["evidence_match"] is True for r in known) / len(known) if known else None,
                        "brier_mean": sum(briers) / len(briers) if briers else None, "confusion": dict(confusion), "per_class": per_class}
    labels = spec["label_set"]["labels"]
    invalid = sum(indexed.get(r["document_id"], {}).get("semantic_status") == "invalid_response" for r in labels)
    criteria = spec["criteria"]
    qualified = len(labels) >= criteria["min_documents"] and all(tags[t] >= criteria["min_case_count"] for t in CASE_TAGS)
    qualified = qualified and invalid / len(labels) <= criteria["max_invalid_rate"] and all(m["known_truth"] >= criteria["min_known_per_question"] and (m["accuracy_all_known"] or 0) >= criteria["min_accuracy"] and (m["evidence_match_rate"] or 0) >= criteria["min_evidence_match"] for m in metrics.values())
    report_id = "quality-" + stable_id(gold_id, digest(documents_path.read_bytes()))
    directory = root / "outputs/material_quality" / report_id
    if (directory / "manifest.json").exists():
        manifest = read_json(directory / "manifest.json")
        for name, hash_ in manifest["artifacts"].items():
            if digest((directory / name).read_bytes()) != hash_:
                raise RuntimeError("quality report artifact hash mismatch")
        return read_json(directory / "report.json")
    result = {"report_id": report_id, "gold_id": gold_id, "data_grade": spec["data_grade"], "metrics": metrics, "case_counts": dict(tags),
              "documents": len(labels), "invalid_response_rate": invalid / len(labels), "criteria": criteria,
              "status": "SYNTHETIC_ONLY" if spec["data_grade"] == "synthetic" else "READY_FOR_HUMAN_REVIEW" if qualified else "HOLD",
              "automatic_material_selection_enabled": False, "stock_prediction_performance_validated": False,
              "report_path": str(directory / "report.json")}
    write_json(directory / "report.json", result)
    write_csv(directory / "samples.csv", list(samples[0]), samples)
    write_json(directory / "provenance.json", {"gold_checksum": spec["checksum"], "input_hash": digest(documents_path.read_bytes()), "created_at": now_iso()})
    write_json(directory / "manifest.json", {"artifacts": {name: digest((directory / name).read_bytes()) for name in ("report.json", "samples.csv", "provenance.json")}})
    return result
