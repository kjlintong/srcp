"""Zero-dependency MCP stdio server exposing SRCP as tools.

Run:  PYTHONPATH=src python3 -m srcp.mcp_server [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

from .backends.sqlite import SQLiteBackend
from .coordinator import Coordinator
from .errors import SRPCError


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}

TOOL_DEFS: list[dict[str, Any]] = [
    {"name": "srcp_capabilities", "description": "Negotiate SRCP capabilities and compliance level.",
     "inputSchema": _object({})},
    {"name": "srcp_declare_resource", "description": "Idempotently declare a shared resource.",
     "inputSchema": _object({
         "resource_id": S, "content_type": S, "granularity": S, "isolation": S,
         "owners": {"type": "array", "items": S}, "writers": {"type": "array", "items": S},
         "readers": {"type": "array", "items": S}, "conflict_policy": S,
         "retention": S, "tenant": S,
     }, ["resource_id"])},
    {"name": "srcp_acquire_lease", "description": "Acquire a write lease with a monotonic fencing token.",
     "inputSchema": _object({"resource_id": S, "principal": S, "ttl_ms": I}, ["resource_id", "principal"])},
    {"name": "srcp_heartbeat", "description": "Extend an active lease.",
     "inputSchema": _object({"lease_id": S, "principal": S}, ["lease_id", "principal"])},
    {"name": "srcp_release_lease", "description": "Release a lease explicitly.",
     "inputSchema": _object({"lease_id": S, "principal": S}, ["lease_id", "principal"])},
    {"name": "srcp_read", "description": "Return a versioned read view; never modifies generation.",
     "inputSchema": _object({"resource_id": S, "principal": S}, ["resource_id", "principal"])},
    {"name": "srcp_write", "description": "Conditional write with lease/fencing/CAS and conflict policy.",
     "inputSchema": _object({
         "resource_id": S, "principal": S, "lease_id": S, "expected_generation": I,
         "content": S, "content_type": S, "intent": S, "idempotency_key": S,
         "expected_version_vector": {"type": "object"}, "conflict_policy": S,
         "deadline_ms": I,
     }, ["resource_id", "principal", "lease_id", "expected_generation", "content"])},
    {"name": "srcp_conflicts", "description": "List recorded conflicts, optionally unresolved only.",
     "inputSchema": _object({"resource_id": S, "unresolved_only": {"type": "boolean"}})},
    {"name": "srcp_audit", "description": "Read the hash-chained audit log and verify its integrity.",
     "inputSchema": _object({"resource_id": S})},
    {"name": "srcp_adjudicate", "description": "Owner-only resolution of a queued semantic conflict.",
     "inputSchema": _object({
         "conflict_id": S, "principal": S, "content": S, "decision": S,
     }, ["conflict_id", "principal"])},
]


class MCPServer:
    def __init__(self, coordinator: Coordinator) -> None:
        self.coordinator = coordinator
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "srcp_capabilities": self._capabilities,
            "srcp_declare_resource": self._declare,
            "srcp_acquire_lease": self._acquire,
            "srcp_heartbeat": self._heartbeat,
            "srcp_release_lease": self._release,
            "srcp_read": self._read,
            "srcp_write": self._write,
            "srcp_conflicts": self._conflicts,
            "srcp_audit": self._audit,
            "srcp_adjudicate": self._adjudicate,
        }

    # ------------------------------------------------------------- tool handlers

    def _capabilities(self, args: dict[str, Any]) -> Any:
        return self.coordinator.capabilities()

    def _declare(self, args: dict[str, Any]) -> Any:
        return {"decl": self.coordinator.declare_resource(args).to_dict()}

    def _acquire(self, args: dict[str, Any]) -> Any:
        lease = self.coordinator.acquire_lease(
            args["resource_id"], args["principal"], int(args.get("ttl_ms", 30_000))
        )
        return {"lease": lease.to_dict()}

    def _heartbeat(self, args: dict[str, Any]) -> Any:
        return {"lease": self.coordinator.heartbeat(args["lease_id"], args["principal"]).to_dict()}

    def _release(self, args: dict[str, Any]) -> Any:
        return {"released": self.coordinator.release_lease(args["lease_id"], args["principal"])}

    def _read(self, args: dict[str, Any]) -> Any:
        return {"view": self.coordinator.read(args["resource_id"], args["principal"]).to_dict()}

    def _write(self, args: dict[str, Any]) -> Any:
        return self.coordinator.write(args).to_dict()

    def _conflicts(self, args: dict[str, Any]) -> Any:
        return {
            "conflicts": self.coordinator.list_conflicts(
                args.get("resource_id"), bool(args.get("unresolved_only", False))
            )
        }

    def _audit(self, args: dict[str, Any]) -> Any:
        ok, problems = self.coordinator.verify_audit()
        return {
            "events": self.coordinator.audit_events(args.get("resource_id")),
            "verified": ok,
            "problems": problems,
        }

    def _adjudicate(self, args: dict[str, Any]) -> Any:
        result = self.coordinator.adjudicate(
            args["conflict_id"], args["principal"], args.get("content"),
            args.get("decision", "accept"),
        )
        return result.to_dict()

    # ------------------------------------------------------------- JSON-RPC loop

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "srcp", "version": "0.1.0"},
                },
            }
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOL_DEFS}}
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            handler = self._handlers.get(name)
            if handler is None:
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"},
                }
            try:
                payload = handler(params.get("arguments") or {})
                is_error = isinstance(payload, dict) and (
                    payload.get("status") in ("CONFLICT", "REJECTED") or payload.get("error")
                )
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "structuredContent": payload,
                        "isError": bool(is_error),
                    },
                }
            except SRPCError as exc:
                payload = {"ok": False, "error": exc.to_dict()}
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "structuredContent": payload,
                        "isError": True,
                    },
                }
            except Exception as exc:  # noqa: BLE001 - protocol server must stay alive
                payload = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}}
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "structuredContent": payload,
                        "isError": True,
                    },
                }
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }), flush=True)
                continue
            response = self.handle(message)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="SRCP MCP stdio server")
    parser.add_argument(
        "--db", default=os.environ.get("SRCP_DB", os.path.expanduser("~/.srcp/state.sqlite3"))
    )
    arguments = parser.parse_args()
    MCPServer(Coordinator(SQLiteBackend(arguments.db))).run()


if __name__ == "__main__":
    main()
