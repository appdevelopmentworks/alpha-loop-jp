"""Daily material side-output boundary; source absence is explicit and costs no GPU."""
from pathlib import Path
import time

from .common import canonical, digest, read_json
from .materials import batch, ingest, seal
from .model_session import local_model
from .semantic import MockSemanticProvider, OpenJevProvider


class MockMaterialProvider(MockSemanticProvider):
    """Two explicit fixture responses; never used to label real disclosures."""
    def __init__(self, response):
        super().__init__(response)
        self.model_revision = digest(canonical(response))

    def ask(self, state, questions):
        if all(q["type"] == "choice" and "unsupported" in q["criteria"] for q in questions.values()):
            return self.response["evidence_response"]
        return self.response["response"]


def load_settings(root: Path, path: str) -> dict:
    settings = read_json(root / path)
    if settings.get("schema_version") != 1 or settings.get("mode") != "shadow" or settings.get("cloud_enabled") is not False or settings.get("orders_enabled") is not False:
        raise ValueError("material settings must be local shadow-only")
    if settings["provider"] not in ("openjev", "mock") or type(settings["max_documents"]) is not int or not 1 <= settings["max_documents"] <= 100 or type(settings["budget_seconds"]) not in (int, float) or not 1 <= settings["budget_seconds"] <= 600:
        raise ValueError("invalid material provider/bounds")
    if settings["provider"] == "mock" and settings.get("purpose") != "synthetic_test":
        raise ValueError("mock material provider is synthetic-only")
    return settings


def import_pending(root: Path, path: str | None) -> dict:
    if not path:
        return {"status": "NOT_CONFIGURED"}
    settings = load_settings(root, path)
    file = root / settings["input_manifest"]
    if not file.exists():
        return {"status": "UNAVAILABLE_SOURCE", "reason": "file disclosure manifest not supplied"}
    return ingest(root, file)


def analyze_pending(root: Path, run_id: str, path: str | None, no_ai: bool = False, retry_unavailable: bool = False) -> dict:
    if not path:
        return {"status": "NOT_CONFIGURED"}
    settings = load_settings(root, path)
    sealed = seal(root, run_id)
    runtime = read_json(root / settings["runtime_config"]) if settings["provider"] == "openjev" else None
    provider = OpenJevProvider(settings["base_url"], settings["model_id"], runtime["model_revision"],
                              runtime["base_image_digest"] + "+topk50", "NVFP4-marlin", runtime, 30) if runtime else MockMaterialProvider(read_json(root / settings["mock_response"]))
    if settings["provider"] == "mock" and sealed["price_data_grade"] != "synthetic":
        raise ValueError("mock materials cannot be used for real-price runs")
    class UnavailableProvider:
        provider_id = "material-provider-disabled"
        model_id = "disabled"
        model_revision = "none"
        server_commit = "none"
        quantization_id = "none"
        def ask(self, *args):
            raise OSError("material inference disabled")
    if no_ai:
        provider = UnavailableProvider()
    if not sealed["documents"] or no_ai or settings["provider"] == "mock":
        return batch(root, run_id, provider, root / settings["question_set"], max_documents=settings["max_documents"], budget_seconds=settings["budget_seconds"], retry_unavailable=retry_unavailable)
    began = time.monotonic()
    with local_model(root, "openjev", min(180, settings["budget_seconds"])):
        remaining = settings["budget_seconds"] - (time.monotonic() - began)
        if remaining <= 0:
            raise TimeoutError("material startup exhausted its budget")
        return batch(root, run_id, provider, root / settings["question_set"], max_documents=settings["max_documents"], budget_seconds=remaining, retry_unavailable=retry_unavailable)
