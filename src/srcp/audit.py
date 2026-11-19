"""Append-only, hash-chained audit log primitives."""

from __future__ import annotations

from typing import Any

from .version import canonical_json, sha256_text

GENESIS_HASH = "sha256:" + "0" * 64

_HASHED_FIELDS = (
    "seq",
    "trace_id",
    "ts",
    "actor",
    "action",
    "resource_id",
    "outcome",
    "evidence",
    "prev_hash",
)


def compute_event_hash(event: dict[str, Any]) -> str:
    payload = {key: event.get(key) for key in _HASHED_FIELDS}
    return sha256_text(canonical_json(payload))


def audit_append(
    events: list[dict[str, Any]],
    *,
    trace_id: str,
    ts: str,
    actor: str,
    action: str,
    resource_id: str,
    outcome: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "seq": len(events) + 1,
        "trace_id": trace_id,
        "ts": ts,
        "actor": actor,
        "action": action,
        "resource_id": resource_id,
        "outcome": outcome,
        "evidence": evidence or {},
        "prev_hash": events[-1]["hash"] if events else GENESIS_HASH,
    }
    event["hash"] = compute_event_hash(event)
    events.append(event)
    return event


def audit_verify(events: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Recompute the chain. Any edit, deletion or insertion is detected."""
    problems: list[str] = []
    previous = GENESIS_HASH
    for index, event in enumerate(events, start=1):
        if event.get("seq") != index:
            problems.append(f"event {index}: seq mismatch ({event.get('seq')!r})")
        if event.get("prev_hash") != previous:
            problems.append(f"event {index}: prev_hash mismatch")
        if event.get("hash") != compute_event_hash(event):
            problems.append(f"event {index}: hash mismatch (payload tampered)")
        previous = event.get("hash", "")
    return (not problems), problems
