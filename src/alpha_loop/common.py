from __future__ import annotations

import csv
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9), "JST")


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"timezone required: {value}")
    return result


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_bytes(data)
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.05 * 2 ** attempt)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8"))


def safe_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    value = str(value)
    if value and value[0] in "=+-@\t\r\n":
        return "'" + value
    return value


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    import io
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: safe_cell(row.get(key)) for key in fields})
    atomic_bytes(path, b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))


def stable_id(*parts: object) -> str:
    return digest(canonical(parts))[:20]
