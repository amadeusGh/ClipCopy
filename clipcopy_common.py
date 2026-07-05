#!/usr/bin/env python3
"""Shared structures for text-only clipboard sync."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass


@dataclass
class ClipboardPayload:
    kind: str
    created_at_ms: int
    text: str | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ClipboardPayload | None":
        if raw is None:
            return None
        return cls(
            kind=str(raw.get("kind", "unknown")),
            created_at_ms=int(raw.get("created_at_ms", 0)),
            text=raw.get("text"),
            source=raw.get("source"),
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "created_at_ms": self.created_at_ms,
            "text": self.text,
            "source": self.source,
        }


def now_ms() -> int:
    return int(time.time() * 1000)


def payload_fingerprint(payload: ClipboardPayload) -> str:
    digest = hashlib.sha256()
    digest.update(payload.kind.encode("utf-8"))
    digest.update(b"\0")
    digest.update((payload.text or "").encode("utf-8"))
    return digest.hexdigest()


def payload_summary(payload: ClipboardPayload) -> str:
    if payload.kind == "text":
        preview = (payload.text or "").strip().replace("\n", "\\n")
        if len(preview) > 80:
            preview = f"{preview[:77]}..."
        return f"text: {preview or '(empty)'}"
    if payload.kind == "empty":
        return "empty clipboard"
    return f"{payload.kind}: unsupported"
