"""Backend contract: a thin, atomic state container.

Design note: the *control-plane* state is deliberately kept small and serialisable.
It is not a replacement for the application database; production systems can map
these fields onto separate tables/keys while keeping the same Coordinator logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class State:
    resources: dict[str, Any] = field(default_factory=dict)
    leases: dict[str, Any] = field(default_factory=dict)
    fence: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    idem: dict[str, Any] = field(default_factory=dict)
    # resource_id -> relative file path for disk-backed resources. The disk file
    # is the content authority; SRCP stores metadata + the last-known content hash.
    bindings: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": self.resources,
            "leases": self.leases,
            "fence": self.fence,
            "tokens": self.tokens,
            "conflicts": self.conflicts,
            "audit": self.audit,
            "idem": self.idem,
            "bindings": self.bindings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "State":
        data = data or {}
        return cls(
            resources=data.get("resources") or {},
            leases=data.get("leases") or {},
            fence=data.get("fence") or {},
            tokens=data.get("tokens") or {},
            conflicts=data.get("conflicts") or [],
            audit=data.get("audit") or [],
            idem=data.get("idem") or {},
            bindings=data.get("bindings") or {},
        )


class Backend(ABC):
    """Atomic state transaction provider."""

    name: str = "base"

    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[State]:
        """Yield a private mutable copy of state; persist it on clean exit."""
        raise NotImplementedError
