"""Fencing demo: a paused agent wakes up with an obsolete lease and is rejected."""

from __future__ import annotations

from srcp import Coordinator
from srcp.backends import MemoryBackend

ALICE = "agent:alice@a"
BOB = "agent:bob@b"


def main() -> None:
    c = Coordinator(MemoryBackend())
    c.declare_resource({"resource_id": "shared/state.json", "content_type": "application/json"})

    alice_lease = c.acquire_lease("shared/state.json", ALICE)
    print(f"alice token = {alice_lease.token} (then Alice is paused)")

    bob_lease = c.acquire_lease("shared/state.json", BOB)
    winner = c.write({
        "resource_id": "shared/state.json", "principal": BOB,
        "lease_id": bob_lease.lease_id, "expected_generation": 0,
        "content": '{"owner": "bob"}', "idempotency_key": "b-1",
        "content_type": "application/json",
    })
    print(f"bob token = {bob_lease.token}, commit = {winner.status}")

    # Alice wakes up, still believing she holds the lock.
    stale = c.write({
        "resource_id": "shared/state.json", "principal": ALICE,
        "lease_id": alice_lease.lease_id, "expected_generation": 0,
        "content": '{"owner": "alice"}', "idempotency_key": "a-1",
        "content_type": "application/json",
    })
    print(f"alice after resume: {stale.status} / {stale.error['code']}")
    print(f"final content: {c.read('shared/state.json', ALICE).content}")
    print("resource-side high-water check is what stops the zombie writer")


if __name__ == "__main__":
    main()
