"""Immutable local source bundles for research runs in this workspace without Git."""
from pathlib import Path

from .common import atomic_bytes, canonical, digest, read_json, write_json


def archive_code(root: Path) -> str:
    source = Path(__file__).parent
    contents = {path.name: path.read_bytes() for path in sorted(source.glob("*.py"))}
    hashes = {name: digest(raw) for name, raw in contents.items()}
    bundle_id = digest(canonical(hashes))
    directory = root / "data" / "code_bundles" / bundle_id
    manifest = directory / "manifest.json"
    if manifest.exists():
        verify_code(root, bundle_id)
        return bundle_id
    for name, raw in contents.items():
        target = directory / "src" / "alpha_loop" / name
        if target.exists() and digest(target.read_bytes()) != hashes[name]:
            raise RuntimeError("code bundle file hash mismatch")
        atomic_bytes(target, raw)
    write_json(manifest, {"bundle_id": bundle_id, "files": hashes, "python_requirement": ">=3.11"})
    return bundle_id


def verify_code(root: Path, bundle_id: str) -> None:
    if len(bundle_id) != 64 or any(ch not in "0123456789abcdef" for ch in bundle_id):
        raise ValueError("invalid code bundle id")
    directory = root / "data" / "code_bundles" / bundle_id
    manifest = read_json(directory / "manifest.json")
    if digest(canonical(manifest["files"])) != bundle_id:
        raise RuntimeError("code bundle manifest hash mismatch")
    for name, hash_ in manifest["files"].items():
        if Path(name).name != name or digest((directory / "src" / "alpha_loop" / name).read_bytes()) != hash_:
            raise RuntimeError("code bundle file hash mismatch")
