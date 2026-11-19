"""Three heterogeneous runtimes (hermes / claude-code / codex) share one plan.

The runtime names are principals, not a shared framework: none of them knows the
others' memory or checkpoints. They only agree on SRCP's resource contract.
"""

from __future__ import annotations

import json

from srcp import Coordinator
from srcp.backends import SQLiteBackend
import os
import tempfile

HERMES = "agent:hermes@sess-1"
CLAUDE = "agent:claude-code@sess-2"
CODEX = "agent:codex@sess-3"
OWNER = "agent:owner@team"


def write_field(c: Coordinator, principal: str, payload: str, key: str):
    lease = c.acquire_lease("shared/plan.json", principal)
    view = c.read("shared/plan.json", principal)
    return c.write({
        "resource_id": "shared/plan.json", "principal": principal,
        "lease_id": lease.lease_id, "expected_generation": view.generation,
        "content": payload, "idempotency_key": key,
        "content_type": "application/json",
    })


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        c = Coordinator(SQLiteBackend(os.path.join(directory, "plan.sqlite3")))
        c.declare_resource({
            "resource_id": "shared/plan.json",
            "content_type": "application/json",
            "granularity": "kv",
            "conflict_policy": "merge",
            "owners": [OWNER],
        })
        for principal, field in (
            (HERMES, '"research": "hermes"'),
            (CLAUDE, '"implementation": "claude-code"'),
            (CODEX, '"review": "codex"'),
        ):
            result = write_field(c, principal, "{" + field + "}", field)
            print(f"{principal:28s} -> {result.status} gen={result.generation}")

        plan = json.loads(c.read("shared/plan.json", OWNER).content)
        print(f"merged plan = {plan}")

        # Now two runtimes claim the SAME field: a semantic conflict, not LWW.
        a = write_field(c, HERMES, '{"decision": "ship"}', "decision-a")
        b = write_field(c, CODEX, '{"decision": "hold"}', "decision-b")
        print(f"same-field writes: {a.status} / {b.status} "
              f"({b.conflict['class'] if b.conflict else '-'})")
        pending = c.list_conflicts("shared/plan.json", unresolved_only=True)
        print(f"pending conflicts = {len(pending)}")
        decided = c.adjudicate(pending[0]["conflict_id"], OWNER,
                               json.dumps({**plan, "decision": "ship"}))
        print(f"owner adjudication: {decided.status}, gen={decided.generation}")
        print(f"final = {c.read('shared/plan.json', OWNER).content}")
        print(f"audit valid = {c.verify_audit()[0]}")


if __name__ == "__main__":
    main()
