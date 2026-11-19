"""Same lost-update race, but guarded by SRCP's CAS + conflict contract."""

from __future__ import annotations

from srcp import Coordinator
from srcp.backends import MemoryBackend

ALICE = "agent:alice@a"
BOB = "agent:bob@b"


def main() -> None:
    coordinator = Coordinator(MemoryBackend())
    coordinator.declare_resource({"resource_id": "shared/plan.md"})

    alice_lease = coordinator.acquire_lease("shared/plan.md", ALICE)
    bob_lease = coordinator.acquire_lease("shared/plan.md", BOB)
    # Both read generation 0.
    coordinator.read("shared/plan.md", ALICE)
    coordinator.read("shared/plan.md", BOB)

    first = coordinator.write({
        "resource_id": "shared/plan.md", "principal": ALICE,
        "lease_id": alice_lease.lease_id, "expected_generation": 0,
        "content": "v0 + A", "intent": "add Alice's section", "idempotency_key": "a-1",
    })
    stale = coordinator.write({
        "resource_id": "shared/plan.md", "principal": BOB,
        "lease_id": bob_lease.lease_id, "expected_generation": 0,
        "content": "v0 + B", "intent": "add Bob's section", "idempotency_key": "b-1",
    })
    print(f"alice: {first.status} generation={first.generation}")
    print(f"bob:   {stale.status} error={stale.error['code']} "
          f"conflict={stale.conflict['class']}")

    # Bob re-reads, reacquires a lease (fencing), and retries with fresh version.
    fresh_lease = coordinator.acquire_lease("shared/plan.md", BOB)
    view = coordinator.read("shared/plan.md", BOB)
    retry = coordinator.write({
        "resource_id": "shared/plan.md", "principal": BOB,
        "lease_id": fresh_lease.lease_id,
        "expected_generation": view.generation,
        "content": view.content + " + B",
        "intent": "retry after conflict",
        "idempotency_key": "b-2",
    })
    print(f"retry: {retry.status} generation={retry.generation}")
    print(f"final: {coordinator.read('shared/plan.md', ALICE).content!r}")
    print(f"audit valid: {coordinator.verify_audit()[0]}")


if __name__ == "__main__":
    main()
