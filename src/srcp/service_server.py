"""Capability MCP server — expose high-level abilities, not protocol primitives.

Run:  PYTHONPATH=src python3 -m srcp.service_server [--db PATH]

Why a separate server from mcp_server.py?
    mcp_server exposes the 10 protocol primitives (compliance burden).
    This server exposes 7 capabilities an agent actually wants to call. Both sit
    on the same Coordinator/SQLite state, so an agent can mix them freely. The
    point is to make 'doing the safe thing' the *easiest* thing: one tool call
    instead of orchestrating lease+read+write and handling conflicts itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

from .backends.sqlite import SQLiteBackend
from .capabilities import CapabilityService
from .coordinator import Coordinator

S = {"type": "string"}
I = {"type": "integer"}
ANY = {}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


TOOL_DEFS: list[dict[str, Any]] = [
    {"name": "memory_commit",
     "description": "Record something you learned into a shared memory namespace. Others can recall it later. Concurrent records never overwrite each other.",
     "inputSchema": _obj({
         "namespace": S, "entry": ANY, "principal": S,
         "tags": {"type": "array", "items": S},
     }, ["namespace", "entry", "principal"])},
    {"name": "memory_recall",
     "description": "Find shared knowledge left by other agents. Use this BEFORE starting work to avoid redoing what others already learned.",
     "inputSchema": _obj({
         "namespace": S, "principal": S, "query": S, "tag": S, "limit": I,
     }, ["namespace", "principal"])},
    {"name": "plan_claim",
     "description": "Atomically reserve a task so no two agents do the same work. Returns ALREADY_CLAIMED if someone else won. Claims expire if an agent crashes.",
     "inputSchema": _obj({
         "board": S, "task_id": S, "principal": S, "note": S, "hold_ms": I,
     }, ["board", "task_id", "principal"])},
    {"name": "plan_complete",
     "description": "Mark your own claimed task as done with a result. Only the task's owner may complete it.",
     "inputSchema": _obj({
         "board": S, "task_id": S, "principal": S, "result": ANY,
     }, ["board", "task_id", "principal", "result"])},
    {"name": "plan_status",
     "description": "See which tasks are claimed, done, and by whom — a shared view of team progress.",
     "inputSchema": _obj({"board": S, "principal": S}, ["board", "principal"])},
    {"name": "file_edit",
     "description": "Safely edit a shared resource in one call. Declarative ops: json_set, json_merge, append, replace. Handles lease/version/conflict internally; concurrent edits to different fields both survive.",
     "inputSchema": _obj({
         "resource_id": S, "principal": S,
         "op": _obj({
             "kind": {"enum": ["json_set", "json_merge", "append", "replace"]},
             "key": S, "value": ANY, "content": S, "separator": S,
         }, ["kind"]),
         "content_type": S,
     }, ["resource_id", "principal", "op"])},
    {"name": "file_read",
     "description": "Read a shared resource with its version and a stale-risk warning if others hold write leases.",
     "inputSchema": _obj({"resource_id": S, "principal": S}, ["resource_id", "principal"])},
]


class CapabilityMCPServer:
    def __init__(self, service: CapabilityService) -> None:
        self.service = service
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "memory_commit": self._memory_commit,
            "memory_recall": self._memory_recall,
            "plan_claim": self._plan_claim,
            "plan_complete": self._plan_complete,
            "plan_status": self._plan_status,
            "file_edit": self._file_edit,
            "file_read": self._file_read,
        }

    def _memory_commit(self, a: dict[str, Any]) -> Any:
        return self.service.memory_commit(
            a["namespace"], a["entry"], a["principal"], tags=a.get("tags")
        )

    def _memory_recall(self, a: dict[str, Any]) -> Any:
        return self.service.memory_recall(
            a["namespace"], a["principal"], query=a.get("query"),
            tag=a.get("tag"), limit=int(a.get("limit", 50)),
        )

    def _plan_claim(self, a: dict[str, Any]) -> Any:
        return self.service.plan_claim(
            a["board"], a["task_id"], a["principal"],
            note=a.get("note", ""), hold_ms=int(a.get("hold_ms", 600_000)),
        )

    def _plan_complete(self, a: dict[str, Any]) -> Any:
        return self.service.plan_complete(
            a["board"], a["task_id"], a["principal"], a["result"]
        )

    def _plan_status(self, a: dict[str, Any]) -> Any:
        return self.service.plan_status(a["board"], a["principal"])

    def _file_edit(self, a: dict[str, Any]) -> Any:
        return self.service.file_edit(
            a["resource_id"], a["principal"], a["op"],
            content_type=a.get("content_type", "application/json"),
        )

    def _file_read(self, a: dict[str, Any]) -> Any:
        return self.service.file_read(a["resource_id"], a["principal"])

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "srcp-capabilities", "version": "0.1.0"},
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
                is_error = isinstance(payload, dict) and payload.get("ok") is False
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        "structuredContent": payload,
                        "isError": bool(is_error),
                    },
                }
            except Exception as exc:  # noqa: BLE001 - keep the server alive
                return {
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(
                            {"ok": False, "error": str(exc)}, ensure_ascii=False)}],
                        "structuredContent": {"ok": False, "error": str(exc)},
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
    parser = argparse.ArgumentParser(description="SRCP capability MCP server")
    parser.add_argument(
        "--db", default=os.environ.get("SRCP_DB", os.path.expanduser("~/.srcp/state.sqlite3"))
    )
    args = parser.parse_args()
    service = CapabilityService(Coordinator(SQLiteBackend(args.db)))
    CapabilityMCPServer(service).run()


if __name__ == "__main__":
    main()
