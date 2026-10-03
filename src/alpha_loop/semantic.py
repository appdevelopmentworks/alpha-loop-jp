from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .common import canonical, digest, now_iso, parse_time, read_json, stable_id, write_json


class SemanticProvider(Protocol):
    provider_id: str
    model_id: str
    model_revision: str
    server_commit: str
    quantization_id: str

    def ask(self, state: str, questions: dict) -> dict: ...


class MockSemanticProvider:
    provider_id = "mock"
    model_id = "mock-materials-v1"
    model_revision = "fixture-1"
    server_commit = "fixture"
    quantization_id = "none"

    def __init__(self, response: dict):
        self.response = response

    def ask(self, state: str, questions: dict) -> dict:
        return self.response


class OpenJevProvider:
    """Local-only TypeSafe-compatible HTTP adapter for razorback16/openjev."""

    provider_id = "razorback16/openjev"

    def __init__(self, base_url: str, model_id: str, model_revision: str, server_commit: str,
                 quantization_id: str, runtime_config: dict, timeout_seconds: float = 120):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
            raise ValueError("OpenJev endpoint must be loopback HTTP")
        if not all((model_id, model_revision, server_commit, quantization_id)) or "latest" in model_id:
            raise ValueError("pinned OpenJev model and runtime identity required")
        if not isinstance(runtime_config, dict) or not runtime_config.get("runtime_config_version"):
            raise ValueError("versioned OpenJev runtime config required")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.model_revision = model_revision
        self.server_commit = server_commit
        self.quantization_id = quantization_id
        self.runtime_config = runtime_config
        self.timeout_seconds = timeout_seconds

    def _http(self, path: str, body: dict | None = None) -> dict:
        data = canonical(body) if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers={"Content-Type": "application/json"} if data else {},
                                         method="POST" if data else "GET")
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read(4_000_000))

    def ask(self, state: str, questions: dict) -> dict:
        models = self._http("/v1/models")
        ids = {entry.get("name") for entry in models.get("models", [])}
        if self.model_id not in ids:
            raise ValueError("pinned model absent from /v1/models")
        return self._http("/v1/systemone", {"state": state, "model": self.model_id, "questions": questions})


def import_document(root: Path, file: Path, instrument_id: str, disclosure_id: str, revision_id: str,
                    published_at: str, synthetic_first_seen_at: str | None = None) -> dict:
    parse_time(published_at)
    if synthetic_first_seen_at is not None:
        parse_time(synthetic_first_seen_at)
    raw = file.read_bytes()
    hash_ = digest(raw)
    first_seen = synthetic_first_seen_at or now_iso()
    data_grade = "synthetic" if synthetic_first_seen_at else "observed"
    raw_path = root / "data" / "raw" / hash_
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if not raw_path.exists():
        raw_path.write_bytes(raw)
    try:
        decoded = raw.decode("utf-8-sig")
        paragraphs = [{"id": f"p{i:03d}", "text": text.strip()} for i, text in
                      enumerate(decoded.replace("\r\n", "\n").split("\n\n"), 1) if text.strip()]
        status = "ready" if paragraphs else "needs_review"
    except UnicodeDecodeError:
        paragraphs, status = [], "needs_review"
    document_id = stable_id(hash_, instrument_id, disclosure_id, revision_id, first_seen)
    record = {"document_id": document_id, "instrument_id": instrument_id,
              "disclosure_id": disclosure_id, "revision_id": revision_id,
              "published_at": published_at, "first_seen_at": first_seen,
              "fetched_at": synthetic_first_seen_at or now_iso(), "imported_at": now_iso(),
              "content_hash": hash_, "raw_ref": f"data/raw/{hash_}", "media_type": "text/plain; charset=utf-8",
              "extractor_version": "plain-text-v1", "paragraphs": paragraphs, "status": status,
              "data_grade": data_grade}
    write_json(root / "data" / "manual_disclosures" / f"{document_id}.json", record)
    return record


def validate_response(response: dict, questions: dict, expected_model: str) -> dict:
    if not isinstance(response, dict) or response.get("model") != expected_model:
        raise ValueError("invalid response model")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("response question mismatch")
    normalized = {}
    for key, question in questions.items():
        answer = answers[key]
        if not isinstance(answer, dict):
            raise ValueError(f"invalid answer: {key}")
        if question["type"] == "noul":
            value = answer.get("noul")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"invalid noul: {key}")
            normalized[key] = {"probability_yes": value}
        elif question["type"] == "choice":
            choices = set(question["criteria"])
            probabilities = answer.get("probabilities")
            selected = answer.get("choice")
            confidence = answer.get("confidence")
            if (selected not in choices or not isinstance(probabilities, dict) or set(probabilities) != choices
                    or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) or x < 0 or x > 1 for x in probabilities.values())
                    or abs(sum(probabilities.values()) - 1) > .01
                    or not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError(f"invalid choice: {key}")
            normalized[key] = {"choice": selected, "probabilities": probabilities, "confidence": confidence}
        elif question["type"] == "score":
            criteria = question["criteria"]
            if not isinstance(criteria, list) or not 1 <= len(criteria) <= 10:
                raise ValueError(f"invalid score criteria: {key}")
            expected_keys = {str(i) for i in range(len(criteria))}
            probabilities = answer.get("probabilities")
            legend = answer.get("legend")
            score = answer.get("score")
            confidence = answer.get("confidence")
            if (not isinstance(probabilities, dict) or set(probabilities) != expected_keys
                    or not isinstance(legend, dict) or set(legend) != expected_keys
                    or any(legend[str(i)] != level for i, level in enumerate(criteria))
                    or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
                           or not 0 <= x <= 1 for x in probabilities.values())
                    or abs(sum(probabilities.values()) - 1) > .01
                    or not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score)
                    or not 0 <= score <= len(criteria) - 1
                    or abs(score - sum(i * probabilities[str(i)] for i in range(len(criteria)))) > .01
                    or not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError(f"invalid score: {key}")
            normalized[key] = {"score": score, "probabilities": probabilities,
                               "legend": legend, "confidence": confidence}
        else:
            raise ValueError(f"unsupported question type: {key}")
    return normalized


def shadow(root: Path, run_id: str, document_id: str, provider: SemanticProvider,
           question_path: Path, evidence_ids: list[str] | None = None, max_state_chars: int = 12000) -> dict:
    from .pipeline import _verified_manifest

    manifest = _verified_manifest(root / "outputs" / run_id)
    if manifest is None:
        raise FileNotFoundError(run_id)
    snapshot = read_json(root / "data" / "snapshots" / (manifest["snapshot_id"] + ".json"))
    document = read_json(root / "data" / "manual_disclosures" / f"{document_id}.json")
    output_path = root / "outputs" / run_id / "semantic" / f"{document_id}.json"
    def hold(status: str, reason: str, **detail):
        result = {"run_id": run_id, "document_id": document_id, "semantic_status": status,
                  "reason": reason, "baseline_unchanged": True, **detail}
        write_json(output_path, result)
        return result
    if document["data_grade"] != snapshot["data_grade"]:
        raise ValueError("document data_grade differs from snapshot")
    if document["status"] != "ready":
        return hold("needs_review", "text_extraction_unavailable")
    if any(parse_time(document[field]) > parse_time(snapshot["as_of"]) for field in ("published_at", "first_seen_at")):
        return hold("unavailable", "after_as_of")
    matches = [item for item in snapshot["usable_disclosures"] if
               item["instrument_id"] == document["instrument_id"] and item["disclosure_id"] == document["disclosure_id"]
               and item["revision_id"] == document["revision_id"]]
    if not matches:
        return hold("unavailable", "not_in_sealed_snapshot")
    if snapshot["data_grade"] != "synthetic" and matches[0].get("content_hash") != document["content_hash"]:
        return hold("needs_review", "source_hash_mismatch")
    valid_ids = {p["id"] for p in document["paragraphs"]}
    evidence_ids = evidence_ids or []
    if not set(evidence_ids) <= valid_ids:
        raise ValueError("unknown evidence paragraph")
    question_set = read_json(question_path)
    questions = question_set["questions"]
    question_hash = digest(canonical(question_set))
    inference_config = {"timeout_seconds": getattr(provider, "timeout_seconds", None),
                        "runtime_config": getattr(provider, "runtime_config", None)}
    identity = {"provider_id": provider.provider_id, "model_id": provider.model_id,
                "model_revision": provider.model_revision, "server_commit": provider.server_commit,
                "quantization_id": provider.quantization_id,
                "inference_config": inference_config, "inference_config_hash": digest(canonical(inference_config)),
                "question_set_version": question_set["question_set_version"], "question_set_hash": question_hash,
                "source_hashes": [document["content_hash"]], "extractor_version": document["extractor_version"]}
    cache_key = digest(canonical(identity))
    cache_path = root / "data" / "semantic_cache" / f"{cache_key}.json"
    state = "\n\n".join(f'[{p["id"]}] {p["text"]}' for p in document["paragraphs"])
    if len(state) > max_state_chars:
        return hold("needs_review", "state_exceeds_limit", state_chars=len(state), limit_chars=max_state_chars)
    if cache_path.exists():
        cached = read_json(cache_path)
    else:
        try:
            raw_response = provider.ask(state, questions)
            normalized = validate_response(raw_response, questions, provider.model_id)
            cached = {"semantic_status": "ok", "response": raw_response, "normalized": normalized,
                      "inferred_at": now_iso(), "identity": identity}
        except (ValueError, KeyError, TypeError) as error:
            cached = {"semantic_status": "invalid_response", "error": str(error), "identity": identity,
                      "inferred_at": now_iso()}
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            cached = {"semantic_status": "unavailable", "error": type(error).__name__, "identity": identity,
                      "inferred_at": now_iso()}
        if cached["semantic_status"] != "unavailable":
            write_json(cache_path, cached)
    status = cached["semantic_status"]
    if status == "ok" and not evidence_ids:
        status = "missing_evidence"
    output = {"run_id": run_id, "document_id": document_id, "instrument_id": document["instrument_id"],
              "semantic_status": status, "evidence_ids": evidence_ids,
              "cache_key": cache_key, "identity": identity, "answers": cached.get("normalized"),
              "error": cached.get("error"),
              "baseline_unchanged": True, "baseline_stage": next((d["stage"] for d in read_json(root / "outputs" / run_id / "decisions.json")
                                                            if d["instrument_id"] == document["instrument_id"]), None)}
    write_json(output_path, output)
    return output
