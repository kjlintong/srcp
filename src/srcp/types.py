"""Protocol data models. All models are JSON-serialisable and match spec/schema/*."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


class SRCPModel:
    """Small helper providing dict conversion with reserved-word mapping."""

    @classmethod
    def from_dict(cls, value: Any):
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError(f"{cls.__name__}.from_dict expects dict, got {type(value).__name__}")
        names = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, item in value.items():
            mapped = "class_" if key == "class" else key
            if mapped in names:
                kwargs[mapped] = item
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if "class_" in data:
            data["class"] = data.pop("class_")
        return data


@dataclass
class ResourceDecl(SRCPModel):
    resource_id: str
    content_type: str = "text/plain"
    granularity: str = "file"
    isolation: str = "causal"
    owners: list[str] = field(default_factory=lambda: ["agent:owner@system"])
    writers: list[str] = field(default_factory=lambda: ["*"])
    readers: list[str] = field(default_factory=lambda: ["*"])
    conflict_policy: str = "abort"
    retention: str = "snapshot"
    tenant: str = "default"


@dataclass
class LeaseInfo(SRCPModel):
    lease_id: str
    resource_id: str
    principal: str
    token: int
    issued_at: str
    expires_at: str
    ttl_ms: int = 30000
    state: str = "active"

    def is_expired(self, now_iso: str) -> bool:
        from datetime import datetime, timezone

        def parse(value: str) -> datetime:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        return parse(now_iso) >= parse(self.expires_at)


@dataclass
class ReadView(SRCPModel):
    resource_id: str
    generation: int
    version_vector: dict[str, int]
    content_hash: str
    content: str
    content_type: str
    read_at: str
    stale_risk: str = "none"
    active_lease: dict[str, Any] | None = None


@dataclass
class WriteRequest(SRCPModel):
    resource_id: str
    principal: str
    lease_id: str
    expected_generation: int
    content: str
    content_type: str = "text/plain"
    intent: str = ""
    idempotency_key: str = ""
    expected_version_vector: dict[str, int] | None = None
    conflict_policy: str | None = None
    deadline_ms: int = 5000


@dataclass
class ConflictRecord(SRCPModel):
    conflict_id: str
    resource_id: str
    class_: str
    detected_at: str
    resolution: str = "unresolved"
    base_generation: int = 0
    parties: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class WriteResult(SRCPModel):
    status: str
    generation: int
    trace_id: str
    version_vector: dict[str, int] = field(default_factory=dict)
    content_hash: str = ""
    conflict: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @property
    def committed(self) -> bool:
        return self.status == "COMMITTED"
