"""Command-line client for the SRCP reference coordinator.

Example:
  srcp declare --resource-id shared/plan.md --policy abort
  srcp lease --resource-id shared/plan.md --principal agent:alice@a
  srcp read --resource-id shared/plan.md --principal agent:alice@a
  srcp write --resource-id shared/plan.md --principal agent:alice@a \
      --lease-id lease-xxx --expected-generation 0 --content '# Plan' --key a-1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .backends.sqlite import SQLiteBackend
from .coordinator import Coordinator
from .errors import SRPCError


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="srcp", description="SRCP coordination client")
    parser.add_argument(
        "--db", default=os.environ.get("SRCP_DB", os.path.expanduser("~/.srcp/state.sqlite3"))
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities", help="show negotiated capabilities")

    declare = sub.add_parser("declare", help="declare a resource")
    declare.add_argument("--resource-id", required=True)
    declare.add_argument("--content-type", default="text/plain")
    declare.add_argument("--granularity", default="file")
    declare.add_argument("--isolation", default="causal")
    declare.add_argument("--policy", dest="conflict_policy", default="abort")
    declare.add_argument("--retention", default="snapshot")
    declare.add_argument("--owners", nargs="*")
    declare.add_argument("--writers", nargs="*")
    declare.add_argument("--readers", nargs="*")
    declare.add_argument("--tenant", default="default")

    lease = sub.add_parser("lease", help="acquire a write lease")
    lease.add_argument("--resource-id", required=True)
    lease.add_argument("--principal", required=True)
    lease.add_argument("--ttl-ms", type=int, default=30_000)

    heartbeat = sub.add_parser("heartbeat", help="extend a lease")
    heartbeat.add_argument("--lease-id", required=True)
    heartbeat.add_argument("--principal", required=True)

    release = sub.add_parser("release", help="release a lease")
    release.add_argument("--lease-id", required=True)
    release.add_argument("--principal", required=True)

    read = sub.add_parser("read", help="read a versioned view")
    read.add_argument("--resource-id", required=True)
    read.add_argument("--principal", required=True)

    write = sub.add_parser("write", help="conditional write")
    write.add_argument("--resource-id", required=True)
    write.add_argument("--principal", required=True)
    write.add_argument("--lease-id", required=True)
    write.add_argument("--expected-generation", type=int, required=True)
    write.add_argument("--content", required=True)
    write.add_argument("--content-type", default="text/plain")
    write.add_argument("--intent", default="")
    write.add_argument("--key", dest="idempotency_key", default="")
    write.add_argument("--policy", dest="conflict_policy", default=None)
    write.add_argument("--deadline-ms", type=int, default=5000)

    conflicts = sub.add_parser("conflicts", help="list conflicts")
    conflicts.add_argument("--resource-id")
    conflicts.add_argument("--unresolved", action="store_true")

    audit = sub.add_parser("audit", help="read and verify the audit chain")
    audit.add_argument("--resource-id")
    audit.add_argument("--verify", action="store_true")

    adjudicate = sub.add_parser("adjudicate", help="owner resolves a queued conflict")
    adjudicate.add_argument("--conflict-id", required=True)
    adjudicate.add_argument("--principal", required=True)
    adjudicate.add_argument("--content")
    adjudicate.add_argument("--decision", default="accept")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    coordinator = Coordinator(SQLiteBackend(args.db))
    try:
        if args.command == "capabilities":
            emit(coordinator.capabilities())
        elif args.command == "declare":
            payload = {
                "resource_id": args.resource_id,
                "content_type": args.content_type,
                "granularity": args.granularity,
                "isolation": args.isolation,
                "conflict_policy": args.conflict_policy,
                "retention": args.retention,
                "tenant": args.tenant,
            }
            for key in ("owners", "writers", "readers"):
                value = getattr(args, key)
                if value:
                    payload[key] = value
            emit(coordinator.declare_resource(payload).to_dict())
        elif args.command == "lease":
            emit(coordinator.acquire_lease(
                args.resource_id, args.principal, args.ttl_ms
            ).to_dict())
        elif args.command == "heartbeat":
            emit(coordinator.heartbeat(args.lease_id, args.principal).to_dict())
        elif args.command == "release":
            emit({"released": coordinator.release_lease(args.lease_id, args.principal)})
        elif args.command == "read":
            emit(coordinator.read(args.resource_id, args.principal).to_dict())
        elif args.command == "write":
            result = coordinator.write({
                "resource_id": args.resource_id,
                "principal": args.principal,
                "lease_id": args.lease_id,
                "expected_generation": args.expected_generation,
                "content": args.content,
                "content_type": args.content_type,
                "intent": args.intent,
                "idempotency_key": args.idempotency_key,
                "conflict_policy": args.conflict_policy,
                "deadline_ms": args.deadline_ms,
            })
            emit(result.to_dict())
            return 0 if result.committed else 1
        elif args.command == "conflicts":
            emit(coordinator.list_conflicts(args.resource_id, args.unresolved))
        elif args.command == "audit":
            if args.verify:
                ok, problems = coordinator.verify_audit()
                emit({"verified": ok, "problems": problems})
            else:
                emit(coordinator.audit_events(args.resource_id))
        elif args.command == "adjudicate":
            result = coordinator.adjudicate(
                args.conflict_id, args.principal, args.content, args.decision
            )
            emit(result.to_dict())
            return 0 if result.committed else 1
        return 0
    except SRPCError as exc:
        emit({"ok": False, "error": exc.to_dict()})
        return 2


if __name__ == "__main__":
    sys.exit(main())
