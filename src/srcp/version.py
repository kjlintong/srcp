"""Version vectors and content hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

VersionVector = dict[str, int]


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def vv_merge(left: VersionVector | None, right: VersionVector | None) -> VersionVector:
    """Join two version vectors by component-wise maximum (causal merge)."""
    result: VersionVector = {k: int(v) for k, v in (left or {}).items()}
    for key, value in (right or {}).items():
        result[key] = max(result.get(key, 0), int(value))
    return result


def vv_bump(vector: VersionVector | None, principal: str) -> VersionVector:
    result = vv_merge(vector, None)
    result[principal] = result.get(principal, 0) + 1
    return result


def vv_compare(left: VersionVector | None, right: VersionVector | None) -> str:
    """Return equal / before / after / concurrent.

    ``before`` means every component of left <= right and at least one is smaller.
    """
    a = left or {}
    b = right or {}
    keys = set(a) | set(b)
    a_le_b = all(a.get(k, 0) <= b.get(k, 0) for k in keys)
    b_le_a = all(b.get(k, 0) <= a.get(k, 0) for k in keys)
    if a_le_b and b_le_a:
        return "equal"
    if a_le_b:
        return "before"
    if b_le_a:
        return "after"
    return "concurrent"
