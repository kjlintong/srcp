"""SRCP Coordinator — the protocol control plane.

All concurrency decisions (ACL, lease, fencing, CAS, conflict policy, audit) are
made here so that every backend exposes exactly the same semantics. That is what
the conformance suite verifies.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import audit_append, audit_verify
from .backends.base import Backend
from .errors import (
    ConflictNotFound,
    InvalidPrincipal,
    InvalidRequest,
    InvalidResourceId,
    PermissionDenied,
    SRPCError,
    UnsupportedConsistency,
    error_dict,
)
from .types import ConflictRecord, LeaseInfo, ReadView, ResourceDecl, WriteRequest, WriteResult
from .version import sha256_text, vv_bump, vv_compare, vv_merge

_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.:@/+-]+$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:[A-Za-z0-9_.:@+-]+@[A-Za-z0-9_.:-]+$")
MAX_LEASE_TTL_MS = 300_000


class Coordinator:
    """The only component that interprets SRCP semantics."""

    POLICIES = ("abort", "append", "merge", "lww", "manual")
    ISOLATIONS = ("serializable", "causal", "eventual")

    def __init__(self, backend: Backend, level: str = "L2") -> None:
        if level not in ("L0", "L1", "L2"):
            raise InvalidRequest(f"unknown compliance level: {level}")
        self.backend = backend
        self.level = level

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _in_future(milliseconds: int) -> str:
        moment = datetime.now(timezone.utc) + timedelta(milliseconds=milliseconds)
        return moment.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _trace() -> str:
        return "tr-" + uuid.uuid4().hex[:16]

    @staticmethod
    def _identifier(prefix: str) -> str:
        return prefix + "-" + uuid.uuid4().hex[:16]

    @staticmethod
    def _validate_resource_id(resource_id: Any) -> None:
        if (
            not isinstance(resource_id, str)
            or not resource_id
            or len(resource_id) > 512
            or resource_id.startswith("/")
            or ".." in resource_id
            or not _RESOURCE_RE.match(resource_id)
        ):
            raise InvalidResourceId(f"invalid resource_id: {resource_id!r}")

    @staticmethod
    def _validate_principal(principal: Any) -> None:
        if not isinstance(principal, str) or not _PRINCIPAL_RE.match(principal):
            raise InvalidPrincipal(f"invalid principal: {principal!r}")

    @staticmethod
    def _acl_match(pattern: str, principal: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith("*"):
            return principal.startswith(pattern[:-1])
        return pattern == principal

    @classmethod
    def _permitted(cls, decl: dict[str, Any], principal: str, kind: str) -> bool:
        is_owner = any(cls._acl_match(p, principal) for p in decl.get("owners", []))
        if kind == "owner":
            return is_owner
        if is_owner:
            return True
        group = "readers" if kind == "read" else "writers"
        return any(cls._acl_match(p, principal) for p in decl.get(group, []))

    def _record_conflict(
        self,
        st: Any,
        *,
        trace_id: str,
        resource_id: str,
        class_: str,
        base_generation: int,
        parties: list[str],
        candidates: list[dict[str, Any]],
        resolution: str,
        detail: str = "",
        principal: str = "system",
    ) -> dict[str, Any]:
        record = ConflictRecord(
            conflict_id=self._identifier("cf"),
            resource_id=resource_id,
            class_=class_,
            detected_at=self._now(),
            resolution=resolution,
            base_generation=base_generation,
            parties=parties,
            candidates=candidates,
            trace_ids=[trace_id],
            detail=detail,
        ).to_dict()
        st.conflicts.append(record)
        audit_append(
            st.audit,
            trace_id=trace_id,
            ts=self._now(),
            actor=principal,
            action="conflict",
            resource_id=resource_id,
            outcome="ok",
            evidence={
                "conflict_id": record["conflict_id"],
                "class": class_,
                "resolution": resolution,
            },
        )
        return record

    def _finish_write(
        self,
        st: Any,
        req: WriteRequest,
        trace_id: str,
        *,
        status: str,
        generation: int,
        version_vector: dict[str, int] | None = None,
        content_hash: str = "",
        conflict: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> WriteResult:
        result = WriteResult(
            status=status,
            generation=generation,
            trace_id=trace_id,
            version_vector=version_vector or {},
            content_hash=content_hash,
            conflict=conflict,
            error=error,
        )
        audit_append(
            st.audit,
            trace_id=trace_id,
            ts=self._now(),
            actor=req.principal,
            action="write",
            resource_id=req.resource_id,
            outcome="denied" if status in ("REJECTED", "CONFLICT") else "ok",
            evidence={
                **(evidence or {}),
                "status": status,
                "intent": req.intent,
                **({"error": error} if error else {}),
            },
        )
        return result

    @staticmethod
    def _implicit_key(req: WriteRequest) -> str:
        return sha256_text(
            f"{req.resource_id}|{req.principal}|{req.lease_id}|"
            f"{req.expected_generation}|{req.content}"
        )

    # ------------------------------------------------------------------ interface

    def list_resources(self) -> list[str]:
        """List declared resource ids (read-only; used by the capability layer to
        declare shared resources idempotently)."""
        with self.backend.transaction() as st:
            return sorted(st.resources.keys())

    def capabilities(self) -> dict[str, Any]:
        return {
            "protocol": "srcp/0.1",
            "level": self.level,
            "isolation": list(self.ISOLATIONS),
            "policies": list(self.POLICIES),
            "backends": [self.backend.name],
            "max_lease_ttl_ms": MAX_LEASE_TTL_MS,
            "deprecated": [],
        }

    def declare_resource(self, declaration: Any) -> ResourceDecl:
        decl = ResourceDecl.from_dict(declaration)
        self._validate_resource_id(decl.resource_id)
        if not decl.content_type:
            raise InvalidRequest("content_type must not be empty")
        if decl.isolation not in self.ISOLATIONS:
            raise UnsupportedConsistency(f"unsupported isolation: {decl.isolation}")
        if decl.conflict_policy not in self.POLICIES:
            raise InvalidRequest(f"unsupported conflict_policy: {decl.conflict_policy}")
        trace_id = self._trace()
        with self.backend.transaction() as st:
            existing = st.resources.get(decl.resource_id)
            if existing:
                old = existing["decl"]
                for key in (
                    "tenant", "content_type", "granularity", "isolation",
                    "conflict_policy", "retention", "owners", "writers", "readers",
                ):
                    if old.get(key) != getattr(decl, key):
                        raise SRPCError(
                            f"resource already declared with different {key}",
                            code="DECLARATION_CONFLICT",
                        )
            else:
                content = ""
                st.resources[decl.resource_id] = {
                    "decl": decl.to_dict(),
                    "generation": 0,
                    "version_vector": {},
                    "content": content,
                    "content_hash": sha256_text(content),
                    "updated_at": self._now(),
                }
                st.fence[decl.resource_id] = 0
                st.tokens[decl.resource_id] = 0
                audit_append(
                    st.audit,
                    trace_id=trace_id,
                    ts=self._now(),
                    actor="system",
                    action="declare",
                    resource_id=decl.resource_id,
                    outcome="ok",
                    evidence={"decl": decl.to_dict()},
                )
        return decl

    def acquire_lease(
        self, resource_id: str, principal: str, ttl_ms: int = 30_000
    ) -> LeaseInfo:
        self._validate_resource_id(resource_id)
        self._validate_principal(principal)
        if not isinstance(ttl_ms, int) or ttl_ms < 1 or ttl_ms > MAX_LEASE_TTL_MS:
            raise InvalidRequest(f"ttl_ms must be in [1, {MAX_LEASE_TTL_MS}]")
        trace_id = self._trace()
        with self.backend.transaction() as st:
            record = st.resources.get(resource_id)
            if record is None:
                raise SRPCError(f"resource not found: {resource_id}", code="RESOURCE_NOT_FOUND")
            decl = record["decl"]
            if not self._permitted(decl, principal, "write"):
                raise PermissionDenied(f"{principal} may not write {resource_id}")
            # token comes from an independent monotonic counter; the fencing high-water
            # mark only advances after a successful commit (see SPEC §5.2).
            st.tokens[resource_id] = st.tokens.get(resource_id, 0) + 1
            lease = LeaseInfo(
                lease_id=self._identifier("lease"),
                resource_id=resource_id,
                principal=principal,
                token=int(st.tokens[resource_id]),
                issued_at=self._now(),
                expires_at=self._in_future(ttl_ms),
                ttl_ms=ttl_ms,
            )
            st.leases[lease.lease_id] = lease.to_dict()
            audit_append(
                st.audit,
                trace_id=trace_id,
                ts=self._now(),
                actor=principal,
                action="acquire",
                resource_id=resource_id,
                outcome="ok",
                evidence={"lease_id": lease.lease_id, "token": lease.token},
            )
        return lease

    def heartbeat(self, lease_id: str, principal: str) -> LeaseInfo:
        self._validate_principal(principal)
        with self.backend.transaction() as st:
            raw = st.leases.get(lease_id)
            if raw is None:
                raise SRPCError(f"lease not found: {lease_id}", code="LEASE_NOT_FOUND")
            lease = LeaseInfo.from_dict(raw)
            if lease.principal != principal:
                raise PermissionDenied("lease belongs to another principal")
            if lease.state != "active" or lease.is_expired(self._now()):
                raise SRPCError("lease is no longer active", code="STALE_LEASE")
            lease.expires_at = self._in_future(lease.ttl_ms)
            st.leases[lease_id] = lease.to_dict()
            audit_append(
                st.audit,
                trace_id=self._trace(),
                ts=self._now(),
                actor=principal,
                action="heartbeat",
                resource_id=lease.resource_id,
                outcome="ok",
                evidence={"lease_id": lease_id, "token": lease.token},
            )
        return lease

    def release_lease(self, lease_id: str, principal: str) -> bool:
        self._validate_principal(principal)
        with self.backend.transaction() as st:
            raw = st.leases.get(lease_id)
            if raw is None:
                return False
            lease = LeaseInfo.from_dict(raw)
            if lease.principal != principal:
                raise PermissionDenied("lease belongs to another principal")
            lease.state = "released"
            st.leases[lease_id] = lease.to_dict()
            audit_append(
                st.audit,
                trace_id=self._trace(),
                ts=self._now(),
                actor=principal,
                action="release",
                resource_id=lease.resource_id,
                outcome="ok",
                evidence={"lease_id": lease_id, "token": lease.token},
            )
        return True

    def read(self, resource_id: str, principal: str) -> ReadView:
        self._validate_resource_id(resource_id)
        self._validate_principal(principal)
        trace_id = self._trace()
        with self.backend.transaction() as st:
            record = st.resources.get(resource_id)
            if record is None:
                raise SRPCError(f"resource not found: {resource_id}", code="RESOURCE_NOT_FOUND")
            decl = record["decl"]
            if not self._permitted(decl, principal, "read"):
                raise PermissionDenied(f"{principal} may not read {resource_id}")
            now = self._now()
            active = [
                LeaseInfo.from_dict(raw)
                for raw in st.leases.values()
                if raw.get("resource_id") == resource_id
                and raw.get("state") == "active"
                and not LeaseInfo.from_dict(raw).is_expired(now)
            ]
            own = next((lease for lease in active if lease.principal == principal), None)
            view = ReadView(
                resource_id=resource_id,
                generation=int(record["generation"]),
                version_vector=dict(record["version_vector"]),
                content_hash=record["content_hash"],
                content=record["content"],
                content_type=decl["content_type"],
                read_at=now,
                stale_risk=(
                    "concurrent-writes-may-exist"
                    if any(lease.principal != principal for lease in active)
                    else "none"
                ),
                active_lease=(
                    {"lease_id": own.lease_id, "token": own.token, "expires_at": own.expires_at}
                    if own else None
                ),
            )
            audit_append(
                st.audit,
                trace_id=trace_id,
                ts=now,
                actor=principal,
                action="read",
                resource_id=resource_id,
                outcome="ok",
                evidence={"generation": view.generation, "stale_risk": view.stale_risk},
            )
        return view

    # ------------------------------------------------------------------ write path

    def write(self, request: Any) -> WriteResult:
        """Conditional write. Business failures are results (and always audited)."""
        req = WriteRequest.from_dict(request)
        trace_id = self._trace()
        started = time.monotonic()
        try:
            self._validate_resource_id(req.resource_id)
            self._validate_principal(req.principal)
            if req.conflict_policy is not None and req.conflict_policy not in self.POLICIES:
                raise InvalidRequest(f"unsupported policy: {req.conflict_policy}")
            if not isinstance(req.expected_generation, int) or req.expected_generation < 0:
                raise InvalidRequest("expected_generation must be a non-negative integer")
        except SRPCError as exc:
            return WriteResult(
                status="REJECTED", generation=0, trace_id=trace_id, error=exc.to_dict()
            )

        idempotency_key = req.idempotency_key or self._implicit_key(req)

        with self.backend.transaction() as st:
            record = st.resources.get(req.resource_id)
            if record is None:
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=0,
                    error=error_dict("RESOURCE_NOT_FOUND", f"no resource {req.resource_id}"),
                    evidence={"stage": "resolve"},
                )

            cached = st.idem.get(idempotency_key)
            if cached is not None:
                audit_append(
                    st.audit, trace_id=trace_id, ts=self._now(), actor=req.principal,
                    action="write", resource_id=req.resource_id, outcome="ok",
                    evidence={"idempotent_replay": True, "original_trace": cached["result"]["trace_id"]},
                )
                return WriteResult.from_dict(cached["result"])

            decl = record["decl"]
            if not self._permitted(decl, req.principal, "write"):
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("PERMISSION_DENIED", f"{req.principal} may not write"),
                    evidence={"stage": "acl"},
                )
            if req.content_type and req.content_type != decl["content_type"]:
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("SCHEMA_CONFLICT", "content_type mismatch"),
                    evidence={"expected": decl["content_type"], "got": req.content_type},
                )

            # lease + fencing validation (hard checks, never negotiable)
            lease_raw = st.leases.get(req.lease_id)
            if lease_raw is None:
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("LEASE_NOT_FOUND", "unknown lease_id"),
                    evidence={"stage": "lease"},
                )
            lease = LeaseInfo.from_dict(lease_raw)
            now = self._now()
            if lease.resource_id != req.resource_id or lease.principal != req.principal:
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("LEASE_MISMATCH", "lease does not match resource/principal"),
                    evidence={"stage": "lease"},
                )
            if lease.state != "active" or lease.is_expired(now):
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("LEASE_EXPIRED", "lease expired or released"),
                    evidence={"stage": "lease"},
                )
            high_water = int(st.fence.get(req.resource_id, 0))
            if int(lease.token) <= high_water:
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict(
                        "FENCE_REJECTED",
                        f"token {lease.token} <= high-water {high_water}",
                    ),
                    evidence={"stage": "fence", "token": lease.token, "high_water": high_water},
                )

            policy = req.conflict_policy or decl.get("conflict_policy", "abort")
            # A writer may only use the policy declared on the resource. Choosing a
            # different policy (e.g. lww instead of abort) is an owner-only act;
            # otherwise a malicious writer could negotiate away the conflict rule.
            if (
                req.conflict_policy is not None
                and req.conflict_policy != decl.get("conflict_policy")
                and not self._permitted(decl, req.principal, "owner")
            ):
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("POLICY_DENIED", "only owners may override conflict policy"),
                    evidence={"stage": "policy", "requested": req.conflict_policy},
                )
            if decl.get("retention") == "append-only" and policy not in ("append", "abort"):
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=int(record["generation"]),
                    error=error_dict("POLICY_DENIED", "retention=append-only forbids overwrite"),
                    evidence={"stage": "policy", "policy": policy},
                )

            current_generation = int(record["generation"])
            current_vector: dict[str, int] = dict(record["version_vector"])
            concurrent = req.expected_generation != current_generation
            if req.expected_version_vector is not None:
                relation = vv_compare(current_vector, req.expected_version_vector)
                if relation in ("concurrent", "before"):
                    concurrent = True

            new_content = req.content
            conflict_record: dict[str, Any] | None = None
            conflict_class = "stale-write" if concurrent else "no-conflict"
            candidates = [
                {
                    "principal": req.principal,
                    "content_hash": sha256_text(req.content),
                    "intent": req.intent,
                }
            ]

            if policy == "append":
                # Append is conflict-free by construction: both writers survive.
                new_content = (
                    record["content"] + "\n" + req.content if record["content"] else req.content
                )
                concurrent = False
                conflict_class = "no-conflict"
            elif policy == "merge":
                try:
                    base_obj = json.loads(record["content"] or "{}")
                    incoming_obj = json.loads(req.content or "{}")
                except (ValueError, TypeError):
                    return self._finish_write(
                        st, req, trace_id, status="REJECTED", generation=current_generation,
                        error=error_dict("SCHEMA_CONFLICT", "merge policy requires JSON objects"),
                        evidence={"stage": "merge"},
                    )
                if not isinstance(base_obj, dict) or not isinstance(incoming_obj, dict):
                    return self._finish_write(
                        st, req, trace_id, status="REJECTED", generation=current_generation,
                        error=error_dict("SCHEMA_CONFLICT", "merge policy requires JSON objects"),
                        evidence={"stage": "merge"},
                    )
                overlapping = [
                    key for key in set(base_obj) & set(incoming_obj)
                    if base_obj[key] != incoming_obj[key]
                ]
                if overlapping:
                    conflict_record = self._record_conflict(
                        st, trace_id=trace_id, resource_id=req.resource_id,
                        class_="semantic-conflict", base_generation=current_generation,
                        parties=[req.principal], candidates=candidates,
                        resolution="unresolved",
                        detail=f"overlapping fields changed by concurrent writers: {sorted(overlapping)}",
                        principal=req.principal,
                    )
                    return self._finish_write(
                        st, req, trace_id, status="CONFLICT", generation=current_generation,
                        conflict=conflict_record,
                        error=error_dict("CONFLICT", "semantic conflict requires adjudication"),
                        evidence={"stage": "merge", "fields": sorted(overlapping)},
                    )
                merged = dict(base_obj)
                merged.update(incoming_obj)
                new_content = json.dumps(merged, sort_keys=True, ensure_ascii=False)
                if concurrent:
                    conflict_class = "mergeable"
                    conflict_record = self._record_conflict(
                        st, trace_id=trace_id, resource_id=req.resource_id,
                        class_="mergeable", base_generation=current_generation,
                        parties=[req.principal], candidates=candidates,
                        resolution="merged", principal=req.principal,
                    )
            elif concurrent and policy == "abort":
                conflict_record = self._record_conflict(
                    st, trace_id=trace_id, resource_id=req.resource_id,
                    class_=conflict_class, base_generation=current_generation,
                    parties=[req.principal], candidates=candidates,
                    resolution="aborted", principal=req.principal,
                )
                return self._finish_write(
                    st, req, trace_id, status="CONFLICT", generation=current_generation,
                    version_vector=current_vector, conflict=conflict_record,
                    error=error_dict("CONFLICT", "stale write; re-read and retry"),
                    evidence={"stage": "conflict", "class": conflict_class},
                )
            elif concurrent and policy == "manual":
                conflict_record = self._record_conflict(
                    st, trace_id=trace_id, resource_id=req.resource_id,
                    class_=conflict_class, base_generation=current_generation,
                    parties=[req.principal], candidates=candidates,
                    resolution="unresolved", principal=req.principal,
                )
                return self._finish_write(
                    st, req, trace_id, status="QUEUED", generation=current_generation,
                    version_vector=current_vector, conflict=conflict_record,
                    evidence={"stage": "conflict", "class": conflict_class},
                )
            elif concurrent and policy == "lww":
                conflict_record = self._record_conflict(
                    st, trace_id=trace_id, resource_id=req.resource_id,
                    class_=conflict_class, base_generation=current_generation,
                    parties=[req.principal], candidates=candidates,
                    resolution="lww", principal=req.principal,
                )

            if time.monotonic() - started > req.deadline_ms / 1000:
                return self._finish_write(
                    st, req, trace_id, status="REJECTED", generation=current_generation,
                    error=error_dict("DEADLINE_EXCEEDED", "write deadline exceeded"),
                    evidence={"stage": "deadline"},
                )

            new_generation = current_generation + 1
            new_vector = vv_bump(
                vv_merge(current_vector, req.expected_version_vector), req.principal
            )
            new_hash = sha256_text(new_content)
            record.update(
                content=new_content,
                content_hash=new_hash,
                generation=new_generation,
                version_vector=new_vector,
                updated_at=self._now(),
            )
            st.resources[req.resource_id] = record
            st.fence[req.resource_id] = max(high_water, int(lease.token))
            result = self._finish_write(
                st, req, trace_id, status="COMMITTED", generation=new_generation,
                version_vector=new_vector, content_hash=new_hash,
                conflict=conflict_record,
                evidence={
                    "stage": "commit", "policy": policy,
                    "conflict_class": conflict_class, "lease_token": lease.token,
                },
            )
            st.idem[idempotency_key] = {"result": result.to_dict(), "at": self._now()}
            return result

    # ------------------------------------------------------------------ conflicts & audit

    def list_conflicts(
        self, resource_id: str | None = None, unresolved_only: bool = False
    ) -> list[dict[str, Any]]:
        with self.backend.transaction() as st:
            return [
                dict(item)
                for item in st.conflicts
                if (resource_id is None or item.get("resource_id") == resource_id)
                and (not unresolved_only or item.get("resolution") == "unresolved")
            ]

    def adjudicate(
        self, conflict_id: str, principal: str, content: str | None = None,
        decision: str = "accept",
    ) -> WriteResult:
        self._validate_principal(principal)
        trace_id = self._trace()
        with self.backend.transaction() as st:
            conflict = next(
                (item for item in st.conflicts if item.get("conflict_id") == conflict_id), None
            )
            if conflict is None:
                raise ConflictNotFound(f"unknown conflict: {conflict_id}")
            resource_id = conflict["resource_id"]
            record = st.resources.get(resource_id)
            if record is None or not self._permitted(record["decl"], principal, "owner"):
                raise PermissionDenied("only resource owners may adjudicate")

            if decision == "abort":
                conflict["resolution"] = "aborted"
                audit_append(
                    st.audit, trace_id=trace_id, ts=self._now(), actor=principal,
                    action="adjudicate", resource_id=resource_id, outcome="ok",
                    evidence={"conflict_id": conflict_id, "decision": "abort"},
                )
                return WriteResult(
                    status="REJECTED", generation=int(record["generation"]), trace_id=trace_id,
                    error=error_dict("ADJUDICATION_ABORTED", "conflict closed without write"),
                )
            if decision != "accept" or content is None:
                raise InvalidRequest("decision must be 'accept' with content, or 'abort'")

            current_generation = int(record["generation"])
            new_vector = vv_bump(dict(record["version_vector"]), principal)
            record.update(
                content=content,
                content_hash=sha256_text(content),
                generation=current_generation + 1,
                version_vector=new_vector,
                updated_at=self._now(),
            )
            conflict["resolution"] = "merged"
            audit_append(
                st.audit, trace_id=trace_id, ts=self._now(), actor=principal,
                action="adjudicate", resource_id=resource_id, outcome="ok",
                evidence={"conflict_id": conflict_id, "decision": "accept"},
            )
            return WriteResult(
                status="COMMITTED", generation=current_generation + 1, trace_id=trace_id,
                version_vector=new_vector, content_hash=sha256_text(content),
                conflict=conflict,
            )

    def audit_events(self, resource_id: str | None = None) -> list[dict[str, Any]]:
        with self.backend.transaction() as st:
            return [
                dict(event) for event in st.audit
                if resource_id is None or event.get("resource_id") == resource_id
            ]

    def verify_audit(self) -> tuple[bool, list[str]]:
        with self.backend.transaction() as st:
            return audit_verify(st.audit)
