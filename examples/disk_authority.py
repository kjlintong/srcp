"""Demo: the shadow copy is gone.

Shows the full lifecycle that eliminates drift between SRCP metadata and the
real file agents edit with their own tools:

    1. bind a real file -> its bytes seed the resource
    2. agents edit through capabilities -> the real file changes in the same commit
    3. someone edits the file directly (out-of-band) -> detected, never overwritten
    4. owner adopts the change -> metadata and file are back in sync

Run:  PYTHONPATH=src python3 examples/disk_authority.py
"""

from __future__ import annotations

import json
import os
import tempfile

from srcp import Coordinator
from srcp.backends import SQLiteBackend
from srcp.capabilities import CapabilityService
from srcp.file_authority import FileAuthority

OWNER = "agent:owner@system"


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = os.path.join(directory, "workspace")
        os.makedirs(workspace)
        target = os.path.join(workspace, "shared_plan.json")

        # A file that already exists, written by some earlier step.
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('{"title": "release plan"}')

        coordinator = Coordinator(SQLiteBackend(os.path.join(directory, "state.sqlite3")))
        authority = FileAuthority(coordinator, workspace)
        service = CapabilityService(coordinator, file_authority=authority)

        print("=== 1. bind: real file seeds the resource ===")
        print(service.file_bind("shared/plan", "shared_plan.json", OWNER,
                                content_type="application/json"))
        print("disk:", open(target, encoding="utf-8").read())

        print("\n=== 2. agents edit via capabilities -> disk stays in sync ===")
        service.file_edit("shared/plan", "hermes",
                          {"kind": "json_set", "key": "research", "value": "done"})
        service.file_edit("shared/plan", "claude",
                          {"kind": "json_set", "key": "code", "value": "done"})
        print("disk:", open(target, encoding="utf-8").read())
        print("read:", service.file_read("shared/plan", "observer")["content"])

        print("\n=== 3. out-of-band edit is detected, not overwritten ===")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('{"title": "release plan", "research": "changed by raw tool"}')
        conflicting = service.file_edit("shared/plan", "hermes",
                                        {"kind": "json_set", "key": "docs", "value": "wip"})
        print("edit result:", conflicting["status"])
        print("disk still intact:", open(target, encoding="utf-8").read())

        print("\n=== 4. owner adopts -> back in sync ===")
        print("adopt:", service.file_adopt("shared/plan", OWNER, note="reviewed raw edit")["status"])
        after = service.file_read("shared/plan", "observer")
        print("read after adopt:", after["content"])
        result = service.file_edit("shared/plan", "claude",
                                   {"kind": "json_set", "key": "docs", "value": "done"})
        print("write after adopt:", result["status"])
        final = json.loads(open(target, encoding="utf-8").read())
        print("final disk:", final)
        print("audit valid:", coordinator.verify_audit()[0])

        assert final["research"] == "changed by raw tool"
        assert final["docs"] == "done"
        assert coordinator.verify_audit()[0]
        print("\nOK: no shadow copy — SRCP metadata and the real file never diverge silently.")


if __name__ == "__main__":
    main()
