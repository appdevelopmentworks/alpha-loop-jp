"""Narrow startup profiling workaround for the pinned vLLM build on RTX 5090.

See https://github.com/vllm-project/vllm/issues/42987. This only changes the
dummy sampler's top_k used during KV-cache memory profiling; real requests keep
their original sampling code. Fail the image build if the upstream code changes.
"""

from importlib.util import find_spec
from pathlib import Path


spec = find_spec("vllm")
if spec is None or spec.submodule_search_locations is None:
    raise RuntimeError("vllm package not found")

source = Path(next(iter(spec.submodule_search_locations))) / "v1" / "worker" / "gpu_model_runner.py"
original = source.read_text(encoding="utf-8")
old = "top_k=dummy_tensors(logits.size(1) - 1),"
new = "top_k=dummy_tensors(50),"
if original.count(old) != 1:
    raise RuntimeError(f"expected exactly one dummy sampler top_k site in {source}")
source.write_text(original.replace(old, new, 1), encoding="utf-8")
print(f"patched dummy sampler top_k in {source}")
