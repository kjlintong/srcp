"""A realistic two-agent collaboration through the capability layer.

Contrast with multi_agent_plan.py: that example had agents call raw protocol
primitives (declare/lease/read/write) — a compliance burden. Here agents call
capabilities that deliver value on their own: recall what others learned, claim
tasks so work is not duplicated, and edit shared files without losing updates.

Run:  PYTHONPATH=src python3 examples/capability_collab.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from srcp import Coordinator
from srcp.backends import SQLiteBackend
from srcp.capabilities import CapabilityService


def agent(name: str, service: CapabilityService, tasks: list[str], barrier: threading.Barrier):
    """One agent's workflow, driven entirely by capability calls."""
    who = f"agent:{name}@team"
    label = name.upper()

    # 1. Value first: recall what the team already learned.
    notes = service.memory_recall("build", who, query="gotcha", limit=10)
    known = [json.dumps(n["entry"]) for n in notes["entries"]]
    print(f"[{label}] recalled {notes['count']} prior note(s): {known}")

    barrier.wait()  # start the race together

    # 2. Each agent claims the tasks it can get; claims are atomic.
    claimed = []
    for task in tasks:
        result = service.plan_claim("sprint-1", task, who)
        mark = "got" if result["ok"] else result["status"]
        print(f"[{label}] claim {task}: {mark}")
        if result["ok"]:
            claimed.append(task)

    # 3. Both agents edit DIFFERENT fields of the same shared plan concurrently.
    field = "research" if name == "hermes" else "implementation"
    edit = service.file_edit("shared/plan.json", who,
                             {"kind": "json_set", "key": field, "value": f"done-by-{name}"})
    print(f"[{label}] edit {field}: {edit['status']} (attempts={edit.get('attempts')})")

    # 4. Record a non-obvious finding for future agents.
    service.memory_commit("build", f"{name}: {field} needs extra review", who,
                          tags=["gotcha", field])
    print(f"[{label}] remembered a gotcha")

    # 5. Finish only the tasks this agent owns.
    for task in claimed:
        service.plan_complete("sprint-1", task, who, {"by": name})
        print(f"[{label}] completed {task}")

    return claimed, edit


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = CapabilityService(
            Coordinator(SQLiteBackend(os.path.join(directory, "collab.sqlite3")))
        )

        # Seed one prior lesson so recall has something valuable to find.
        service.memory_commit("build", "build gotcha: set PYTHONPATH before import",
                              "agent:previous@team", tags=["gotcha"])

        barrier = threading.Barrier(2)
        results = {}

        def run(name, tasks):
            results[name] = agent(name, service, tasks, barrier)

        t1 = threading.Thread(target=run, args=("hermes", ["design-api", "write-tests"]))
        t2 = threading.Thread(target=run, args=("claude", ["design-api", "write-tests"]))
        t1.start(); t2.start()
        t1.join(); t2.join()

        print("\n=== final shared state ===")
        plan = service.file_read("shared/plan.json", "agent:observer@team")
        print("plan:", plan["content"])
        print("board:", json.dumps(service.plan_status("sprint-1", "observer")["tasks"],
                                   ensure_ascii=False))
        print("memory notes:", service.memory_recall("build", "observer", limit=20)["count"])
        print("audit valid:", service.coordinator.verify_audit()[0])

        # Assertions that make the value concrete.
        doc = json.loads(plan["content"])
        assert doc == {"research": "done-by-hermes", "implementation": "done-by-claude"}, doc
        board = service.plan_status("sprint-1", "observer")["tasks"]
        assert all(t["status"] == "done" for t in board.values()), board
        assert all("by" in t["result"] for t in board.values()), board
        assert service.coordinator.verify_audit()[0]
        print("\nOK: no duplicated work, no lost edits, full audit trail.")


if __name__ == "__main__":
    main()
