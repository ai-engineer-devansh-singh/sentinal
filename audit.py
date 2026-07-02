"""Immutable-ish audit trail for governance (Checkpoint 4 / 5).

In-memory append-only log plus a persisted JSONL file. Every alert, every
clinician response, and every model invocation is recorded.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_LOG_PATH = Path(__file__).resolve().parent / "audit_trail.jsonl"


class AuditLog:
    def __init__(self) -> None:
        _LOG_PATH.touch(exist_ok=True)
        self.entries: list[dict[str, Any]] = []

    def append(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        try:
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def all(self) -> list[dict[str, Any]]:
        return list(self.entries)


audit = AuditLog()