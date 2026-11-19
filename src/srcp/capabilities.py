"""Capability layer — turn SRCP protocol primitives into things agents want to use.

The protocol layer (Coordinator) exposes lease/read/write. That is a *compliance
burden*: an agent must orchestrate three calls correctly on every edit. This layer
wraps those primitives into high-level capabilities that deliver value on their own:

    memory/commit + memory/recall   shared knowledge an agent actually wants to find
    plan/claim    + plan/complete   task reservation with fencing (one winner)
    file/edit                       one-call safe edit with internal retry

Every capability is implemented by mapping onto ordinary SRCP resources and calling
the same Coordinator. No lock, version vector or conflict policy is reimplemented here.
That is the point: the protocol stays thin and neutral; the *capability* is what drives
voluntary adoption.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .coordinator import Coordinator
from .errors import SRPCError

_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@+-]+$")


def normalize_principal(value: str, default_scope: str = "default") -> str:
    """Accept a friendly name ('hermes') or a full principal ('agent:hermes@s1')."""
    if not isinstance(value, str) or not value:
        raise ValueError("principal must be a non-empty string")
    if ":" in value and "@" in value:
        return value
    clean = re.sub(r"[^A-Za-z0-9_.:@+-]", "-", value)
    return f"agent:{clean}@{default_scope}"


def _slug(value: str) -> str:
    """Turn an arbitrary namespace/board name into a safe resource-id segment."""
    clean = re.sub(r"[^A-Za-z0-9_.:@+-]", "-", str(value)).strip("-.")
    return clean or "default"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime:
    if not value:
        return _now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return _now()


class CapabilityService:
    """High-level, agent-facing capabilities built on top of a Coordinator."""

    def __init__(self, coordinator: Coordinator, *, default_scope: str = "default") -> None:
        self.coordinator = coordinator
        self.default_scope = default_scope

    def _p(self, principal: str) -> str:
        return normalize_principal(principal, self.default_scope)

    def _ensure(
        self,
        resource_id: str,
        *,
        content_type: str = "application/json",
        granularity: str = "kv",
        conflict_policy: str = "abort",
        owners: list[str] | None = None,
    ) -> None:
        """Declare a resource only if absent. Safe under concurrent declaration."""
        if resource_id in self.coordinator.list_resources():
            return
        try:
            self.coordinator.declare_resource({
                "resource_id": resource_id,
                "content_type": content_type,
                "granularity": granularity,
                "conflict_policy": conflict_policy,
                "owners": owners or ["agent:owner@system"],
            })
        except SRPCError as exc:
            # A peer declared it between our check and declare; same id is fine.
            if exc.code != "DECLARATION_CONFLICT":
                raise

    # ------------------------------------------------------------ shared memory

    def memory_commit(
        self,
        namespace: str,
        entry: Any,
        principal: str,
        *,
        tags: list[str] | None = None,
        ttl_ms: int = 30_000,
    ) -> dict[str, Any]:
        """Append a fact/insight to a shared memory namespace.

        Uses the SRCP 'append' policy: concurrent commits are conflict-free by
        construction and both survive. This is the capability an agent wants —
        'record what I learned so the next agent benefits' — not a protocol chore.
        """
        rid = f"memory/{_slug(namespace)}"
        who = self._p(principal)
        self._ensure(rid, granularity="log", conflict_policy="append")
        payload = json.dumps(
            {
                "entry": entry,
                "tags": tags or [],
                "by": who,
                "at": _iso(_now()),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        last = {"ok": False, "status": "UNKNOWN"}
        for attempt in range(8):
            # Re-acquire and re-read on every try: a stale token gets fenced, and a
            # peer may have appended since our last read. This loop is the price of
            # a voluntary protocol — kept inside the capability, invisible to agents.
            lease = self.coordinator.acquire_lease(rid, who, ttl_ms)
            view = self.coordinator.read(rid, who)
            result = self.coordinator.write({
                "resource_id": rid,
                "principal": who,
                "lease_id": lease.lease_id,
                "expected_generation": view.generation,
                "content": payload,
                "content_type": "application/json",
                "intent": "memory.commit",
                "idempotency_key": f"mem:{rid}:{who}:{payload}",
            })
            if result.committed:
                return {
                    "ok": True,
                    "status": "COMMITTED",
                    "namespace": namespace,
                    "generation": result.generation,
                    "trace_id": result.trace_id,
                    "error": None,
                    "attempts": attempt + 1,
                }
            last = {"ok": False, "status": result.status, "error": result.error,
                    "attempts": attempt + 1}
            time.sleep(0.005)
        return last

    def memory_recall(
        self,
        namespace: str,
        principal: str,
        *,
        query: str | None = None,
        tag: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read back shared memory, optionally filtered by substring or tag."""
        rid = f"memory/{_slug(namespace)}"
        who = self._p(principal)
        self._ensure(rid, granularity="log", conflict_policy="append")
        view = self.coordinator.read(rid, who)
        entries: list[dict[str, Any]] = []
        for line in view.content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items = json.loads(line)
            except ValueError:
                continue
            records = items if isinstance(items, list) else [items]
            for record in records:
                if not isinstance(record, dict) or "entry" not in record:
                    continue
                if tag and tag not in (record.get("tags") or []):
                    continue
                if query and query.lower() not in json.dumps(
                    record.get("entry"), ensure_ascii=False
                ).lower():
                    continue
                entries.append(record)
        return {
            "ok": True,
            "namespace": namespace,
            "generation": view.generation,
            "count": len(entries),
            "entries": entries[:limit],
            "stale_risk": view.stale_risk,
        }

    # --------------------------------------------------------------- task plan

    def _read_json(self, rid: str, who: str) -> tuple[int, dict[str, Any], str]:
        view = self.coordinator.read(rid, who)
        payload = view.content.strip() or "{}"
        try:
            obj = json.loads(payload)
        except ValueError:
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        return view.generation, obj, view.content

    def plan_claim(
        self,
        board: str,
        task_id: str,
        principal: str,
        *,
        note: str = "",
        hold_ms: int = 600_000,
        ttl_ms: int = 30_000,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        """Atomically reserve a task. Exactly one concurrent claimant wins.

        Fencing is inherited: the winner holds an SRCP lease token, and any stale
        claimant that tries to write with an old token is rejected by the protocol.
        The claim also expires after hold_ms so a crashed agent does not block work.
        """
        rid = f"plan/{_slug(board)}"
        who = self._p(principal)
        self._ensure(rid, granularity="kv", conflict_policy="abort")
        now = _now()

        for attempt in range(max_retries):
            lease = self.coordinator.acquire_lease(rid, who, ttl_ms)
            generation, tasks, _ = self._read_json(rid, who)
            current = tasks.get(task_id) or {}
            status = current.get("status")
            expired = status == "claimed" and _parse(current.get("expires_at")) <= now
            if status == "done":
                return {"ok": False, "status": "ALREADY_DONE", "task_id": task_id,
                        "owner": current.get("owner"), "attempts": attempt + 1}
            if status == "claimed" and not expired:
                return {"ok": False, "status": "ALREADY_CLAIMED", "task_id": task_id,
                        "owner": current.get("owner"), "attempts": attempt + 1}

            tasks[task_id] = {
                "status": "claimed",
                "owner": who,
                "claimed_at": _iso(now),
                "expires_at": _iso(now + timedelta(milliseconds=hold_ms)),
                "note": note,
            }
            result = self.coordinator.write({
                "resource_id": rid,
                "principal": who,
                "lease_id": lease.lease_id,
                "expected_generation": generation,
                "content": json.dumps(tasks, sort_keys=True, ensure_ascii=False),
                "content_type": "application/json",
                "intent": f"plan.claim:{task_id}",
                "idempotency_key": f"claim:{rid}:{task_id}:{who}:{generation}",
            })
            if result.committed:
                return {"ok": True, "status": "CLAIMED", "task_id": task_id,
                        "owner": who, "generation": result.generation,
                        "expires_at": tasks[task_id]["expires_at"],
                        "trace_id": result.trace_id, "attempts": attempt + 1}
            # Someone won the race; loop and re-read their decision.
            time.sleep(0.005)
        return {"ok": False, "status": "RETRY_EXHAUSTED", "task_id": task_id,
                "attempts": max_retries}

    def plan_complete(
        self,
        board: str,
        task_id: str,
        principal: str,
        result_value: Any,
        *,
        ttl_ms: int = 30_000,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        """Mark a claimed task done. Only its owner may complete it."""
        rid = f"plan/{_slug(board)}"
        who = self._p(principal)
        self._ensure(rid, granularity="kv", conflict_policy="abort")
        for attempt in range(max_retries):
            lease = self.coordinator.acquire_lease(rid, who, ttl_ms)
            generation, tasks, _ = self._read_json(rid, who)
            current = tasks.get(task_id) or {}
            if current.get("status") == "done":
                return {"ok": True, "status": "ALREADY_DONE", "task_id": task_id}
            if current.get("owner") != who:
                return {"ok": False, "status": "NOT_OWNER", "task_id": task_id,
                        "owner": current.get("owner")}
            tasks[task_id] = {**current, "status": "done",
                              "result": result_value, "completed_at": _iso(_now())}
            result = self.coordinator.write({
                "resource_id": rid,
                "principal": who,
                "lease_id": lease.lease_id,
                "expected_generation": generation,
                "content": json.dumps(tasks, sort_keys=True, ensure_ascii=False),
                "content_type": "application/json",
                "intent": f"plan.complete:{task_id}",
                "idempotency_key": f"done:{rid}:{task_id}:{who}:{generation}",
            })
            if result.committed:
                return {"ok": True, "status": "DONE", "task_id": task_id,
                        "generation": result.generation, "trace_id": result.trace_id}
            time.sleep(0.005)
        return {"ok": False, "status": "RETRY_EXHAUSTED", "task_id": task_id}

    def plan_status(self, board: str, principal: str) -> dict[str, Any]:
        rid = f"plan/{_slug(board)}"
        who = self._p(principal)
        self._ensure(rid, granularity="kv", conflict_policy="abort")
        generation, tasks, _ = self._read_json(rid, who)
        summary = {
            task_id: {
                "status": info.get("status"),
                "owner": info.get("owner"),
                "result": info.get("result"),
            }
            for task_id, info in tasks.items()
        }
        return {"ok": True, "board": board, "generation": generation, "tasks": summary}

    # --------------------------------------------------------------- safe edit

    @staticmethod
    def _apply(op: dict[str, Any], content: str) -> tuple[str, str]:
        """Return (new_content, policy_hint) for a declarative edit op."""
        kind = op.get("kind")
        if kind == "replace":
            return str(op.get("content", "")), "abort"
        if kind == "append":
            sep = op.get("separator", "\n")
            return (content + sep + str(op.get("content", ""))).lstrip(sep), "append"
        if kind in ("json_set", "json_merge"):
            try:
                obj = json.loads(content or "{}")
            except ValueError:
                obj = {}
            if not isinstance(obj, dict):
                obj = {}
            if kind == "json_set":
                obj[str(op.get("key"))] = op.get("value")
            else:
                patch = op.get("value")
                if isinstance(patch, dict):
                    obj.update(patch)
            return json.dumps(obj, sort_keys=True, ensure_ascii=False), "merge"
        raise ValueError(f"unsupported op kind: {kind!r}")

    def file_edit(
        self,
        resource_id: str,
        principal: str,
        op: dict[str, Any],
        *,
        content_type: str = "application/json",
        ttl_ms: int = 30_000,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        """One-call safe edit: lease + read + conditional write + retry on conflict.

        The agent makes ONE call and expresses intent declaratively. All protocol
        choreography (token, generation, conflict handling) stays inside — this is
        the 'capability' that makes following the protocol the easy path.
        """
        who = self._p(principal)
        # Declared as 'abort' (plain CAS): the op itself computes the FULL new
        # content from the latest read, so there is no second merge at the server.
        # Conflicts are handled by this retry loop, which re-reads and re-applies.
        # This keeps roles clean: Coordinator = CAS + conflict detection;
        # capability = semantic transform + retry.
        self._ensure(resource_id, content_type=content_type, conflict_policy="abort")

        last: dict[str, Any] = {"ok": False, "status": "UNKNOWN"}
        for attempt in range(max_retries):
            lease = self.coordinator.acquire_lease(resource_id, who, ttl_ms)
            view = self.coordinator.read(resource_id, who)
            try:
                new_content, _hint = self._apply(op, view.content)
            except ValueError as exc:
                return {"ok": False, "status": "BAD_OP", "error": str(exc)}
            result = self.coordinator.write({
                "resource_id": resource_id,
                "principal": who,
                "lease_id": lease.lease_id,
                "expected_generation": view.generation,
                "content": new_content,
                "content_type": content_type,
                "intent": f"file.edit:{op.get('kind')}",
                "idempotency_key": (
                    f"edit:{resource_id}:{who}:{view.generation}:{op.get('kind')}:"
                    f"{op.get('key', '')}:{json.dumps(op.get('value'), sort_keys=True, default=str)}"
                ),
            })
            if result.committed:
                return {
                    "ok": True,
                    "status": "COMMITTED",
                    "resource_id": resource_id,
                    "generation": result.generation,
                    "attempts": attempt + 1,
                    "conflict": result.conflict,
                    "trace_id": result.trace_id,
                }
            last = {
                "ok": False,
                "status": result.status,
                "error": result.error,
                "attempts": attempt + 1,
            }
            if result.status == "CONFLICT" and op.get("kind") in ("replace",):
                # Whole-file replace cannot be auto-merged; surface the real conflict
                # instead of silently clobbering the other writer.
                last["conflict"] = result.conflict
                break
            time.sleep(0.005)
        return last

    def file_read(self, resource_id: str, principal: str) -> dict[str, Any]:
        who = self._p(principal)
        view = self.coordinator.read(resource_id, who)
        return {
            "ok": True,
            "resource_id": resource_id,
            "generation": view.generation,
            "content": view.content,
            "content_type": view.content_type,
            "version_vector": view.version_vector,
            "stale_risk": view.stale_risk,
        }
