"""Disk authority — make the real file the source of truth, not a shadow copy.

Problem this solves:
    Without this layer, SRCP stores content in its own SQLite blob while agents
    edit the real file with their own tools. The two copies drift apart forever;
    the audit log then describes content nobody actually sees.

Design:
    A resource can be *bound* to a path under a workspace root. Once bound:
      - the disk file is the content authority;
      - SRCP keeps generation / lease / fence / conflict / audit plus the hash
        of the last content it committed;
      - every read hashes the disk file and compares it to SRCP's recorded hash;
      - every write re-checks the hash, then commits metadata AND writes the file
        inside the SAME state transaction (via Coordinator.commit_hook), so they
        cannot diverge;
      - an out-of-band disk edit is detected as AUTHORITY_CONFLICT. Nothing is
        silently overwritten; an owner explicitly adopts it with adopt_external().

Path safety:
    Relative paths are resolved under `root` and re-checked with os.path.commonpath
    after resolution, so `..` traversal and symlink escapes are rejected.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any

from .coordinator import Coordinator
from .errors import InvalidRequest, SRPCError
from .version import sha256_text


class FileAuthority:
    """Bind SRCP resources to authoritative files on disk."""

    def __init__(self, coordinator: Coordinator, root: str) -> None:
        self.coordinator = coordinator
        self.root = os.path.realpath(os.path.abspath(root))
        os.makedirs(self.root, exist_ok=True)
        # Wire the atomic disk write into the protocol's commit transaction.
        coordinator.commit_hook = self._commit_hook

    # ------------------------------------------------------------------ path safety

    def resolve(self, rel_path: str) -> str:
        """Resolve a relative path under root, rejecting traversal / symlink escape."""
        if not isinstance(rel_path, str) or not rel_path or os.path.isabs(rel_path):
            raise InvalidRequest(f"path must be a non-empty relative path: {rel_path!r}")
        candidate = os.path.realpath(os.path.join(self.root, rel_path))
        try:
            inside = os.path.commonpath([self.root, candidate]) == self.root
        except ValueError:
            inside = False
        if not inside:
            raise InvalidRequest(f"path escapes workspace root: {rel_path!r}")
        return candidate

    def _rel(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.root).replace(os.sep, "/")

    # ------------------------------------------------------------------- disk hash

    @staticmethod
    def hash_file(path: str) -> str | None:
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    @staticmethod
    def _read_file(path: str) -> str:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _atomic_write(self, path: str, content: str) -> None:
        """Write file atomically: temp file in same dir + fsync + os.replace."""
        parent = os.path.dirname(path) or self.root
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".srcp-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _commit_hook(
        self, state: Any, resource_id: str, content: str, trace_id: str
    ) -> None:
        """Called inside the Coordinator transaction. Writes the bound file only."""
        rel = state.bindings.get(resource_id)
        if rel is None:
            return
        self._atomic_write(self.resolve(rel), content)

    # ---------------------------------------------------------------------- binding

    def bind(
        self,
        resource_id: str,
        rel_path: str,
        principal: str,
        *,
        content_type: str = "text/plain",
        conflict_policy: str = "abort",
    ) -> dict[str, Any]:
        """Bind a resource to a file. If the file exists, its bytes seed generation 0."""
        abs_path = self.resolve(rel_path)
        rel = self._rel(abs_path)
        existing_binding = self.coordinator.get_binding(resource_id)
        if existing_binding is not None and existing_binding != rel:
            raise SRPCError(
                f"resource already bound to {existing_binding}",
                code="BINDING_CONFLICT",
            )
        content = self._read_file(abs_path)
        if existing_binding is None:
            self.coordinator.bind_resource(
                resource_id,
                rel,
                content,
                declaration={
                    "resource_id": resource_id,
                    "content_type": content_type,
                    "conflict_policy": conflict_policy,
                },
            )
        return {
            "ok": True,
            "status": "BOUND",
            "resource_id": resource_id,
            "path": rel,
            "content_hash": sha256_text(content),
            "generation": self.coordinator.read(resource_id, principal).generation,
        }

    def is_bound(self, resource_id: str) -> bool:
        return self.coordinator.get_binding(resource_id) is not None

    # -------------------------------------------------------------- authority check

    def _check_authority(self, resource_id: str, expected_hash: str | None) -> dict[str, Any]:
        """Compare disk hash to what SRCP recorded. Return a report dict."""
        rel = self.coordinator.get_binding(resource_id)
        if rel is None:
            return {"bound": False, "authority": "srcp"}
        abs_path = self.resolve(rel)
        disk_hash = self.hash_file(abs_path)
        recorded = self.coordinator.bound_content_hash(resource_id)
        # A missing file is treated as empty content (hash of ""), matching bind's
        # seeding so a newly-bound absent file is not spuriously a conflict.
        if disk_hash is None:
            disk_hash = sha256_text("")
        if expected_hash is not None and disk_hash != expected_hash:
            # Disk changed between the caller's read and write: out-of-band edit.
            return {"bound": True, "authority": "disk", "mismatch": True,
                    "disk_hash": disk_hash, "recorded_hash": recorded}
        if disk_hash != recorded:
            return {"bound": True, "authority": "disk", "mismatch": True,
                    "disk_hash": disk_hash, "recorded_hash": recorded}
        return {"bound": True, "authority": "disk", "mismatch": False,
                "disk_hash": disk_hash, "recorded_hash": recorded}

    def read(self, resource_id: str, principal: str) -> dict[str, Any]:
        """Authoritative read: content comes from disk, hash is verified."""
        view = self.coordinator.read(resource_id, principal)
        report = self._check_authority(resource_id, None)
        if report.get("bound") and report["mismatch"]:
            # Out-of-band change: surface it, never silently reconcile.
            disk_content = self._read_file(self.resolve(
                self.coordinator.get_binding(resource_id)  # type: ignore[arg-type]
            ))
            return {
                "ok": False,
                "status": "AUTHORITY_CONFLICT",
                "resource_id": resource_id,
                "generation": view.generation,
                "srcp_content": view.content,
                "disk_content": disk_content,
                "disk_hash": report["disk_hash"],
                "recorded_hash": report["recorded_hash"],
                "note": "disk was modified outside SRCP; owner must adopt_external()",
            }
        if not report.get("bound"):
            return {
                "ok": True, "status": "OK", "resource_id": resource_id,
                "content": view.content, "generation": view.generation,
                "version_vector": view.version_vector, "stale_risk": view.stale_risk,
                "authority": "srcp",
            }
        disk_content = self._read_file(self.resolve(
            self.coordinator.get_binding(resource_id)  # type: ignore[arg-type]
        ))
        return {
            "ok": True,
            "status": "OK",
            "resource_id": resource_id,
            "content": disk_content,
            "generation": view.generation,
            "version_vector": view.version_vector,
            "content_hash": report["disk_hash"],
            "stale_risk": view.stale_risk,
            "authority": "disk",
        }

    def write(
        self,
        resource_id: str,
        principal: str,
        content: str,
        expected_generation: int,
        expected_content_hash: str | None = None,
        *,
        intent: str = "",
        ttl_ms: int = 30_000,
    ) -> dict[str, Any]:
        """Conditional write guarded by both generation CAS and disk hash."""
        if not self.is_bound(resource_id):
            raise InvalidRequest(
                f"resource {resource_id} is not bound to a disk file; use file_bind first"
            )
        # Detect out-of-band disk edit before acquiring a lease.
        report = self._check_authority(resource_id, expected_content_hash)
        if report["mismatch"]:
            return {
                "ok": False,
                "status": "AUTHORITY_CONFLICT",
                "resource_id": resource_id,
                "disk_hash": report["disk_hash"],
                "recorded_hash": report["recorded_hash"],
                "note": "disk changed outside SRCP; re-read and adopt before writing",
            }
        lease = self.coordinator.acquire_lease(resource_id, principal, ttl_ms)
        result = self.coordinator.write({
            "resource_id": resource_id,
            "principal": principal,
            "lease_id": lease.lease_id,
            "expected_generation": expected_generation,
            "content": content,
            "content_type": self.coordinator.content_type_of(resource_id),
            "intent": intent or "fileAuthority.write",
            "idempotency_key": f"fa:{resource_id}:{principal}:{expected_generation}:{sha256_text(content)}",
        })
        return {
            "ok": result.committed,
            "status": result.status,
            "resource_id": resource_id,
            "generation": result.generation,
            "content_hash": result.content_hash,
            "error": result.error,
            "trace_id": result.trace_id,
        }

    def adopt(self, resource_id: str, principal: str, *, note: str = "") -> dict[str, Any]:
        """Sanction the disk's current bytes as a new protocol generation."""
        if not self.is_bound(resource_id):
            raise InvalidRequest(f"resource {resource_id} is not bound to a disk file")
        content = self._read_file(self.resolve(
            self.coordinator.get_binding(resource_id)  # type: ignore[arg-type]
        ))
        return self.coordinator.adopt_external(resource_id, principal, content, note=note)
