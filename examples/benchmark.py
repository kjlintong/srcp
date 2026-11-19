"""Quantify the difference: uncoordinated writes vs SRCP-guarded writes.

Run: PYTHONPATH=src python3 examples/benchmark.py --rounds 50
Writes results/benchmark.json (overwritable; no secrets, no personal data).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import threading
from datetime import datetime, timezone

from srcp import Coordinator
from srcp.backends import MemoryBackend

ALICE = "agent:alice@a"
BOB = "agent:bob@b"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def baseline_round(directory: str, index: int) -> int:
    """Return how many writers were silently lost."""
    path = pathlib.Path(directory) / f"plan-{index}.md"
    path.write_text("v0", encoding="utf-8")
    barrier = threading.Barrier(2)
    markers = {"A": " + A", "B": " + B"}

    def writer(name: str, marker: str) -> None:
        observed = path.read_text(encoding="utf-8")
        barrier.wait()
        path.write_text(observed + marker, encoding="utf-8")

    threads = [
        threading.Thread(target=writer, args=("A", markers["A"])),
        threading.Thread(target=writer, args=("B", markers["B"])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = path.read_text(encoding="utf-8")
    return sum(marker not in final for marker in markers.values())


def srcp_round(index: int) -> dict[str, int]:
    """Return conflicts, retries and silent losses for one guarded round."""
    resource = f"shared/plan-{index}.md"
    c = Coordinator(MemoryBackend())
    c.declare_resource({"resource_id": resource})
    barrier = threading.Barrier(2)
    outcomes: list = []

    def writer(principal: str, marker: str, key: str) -> None:
        lease = c.acquire_lease(resource, principal)
        view = c.read(resource, principal)
        barrier.wait()
        result = c.write({
            "resource_id": resource, "principal": principal,
            "lease_id": lease.lease_id, "expected_generation": view.generation,
            "content": view.content + marker, "idempotency_key": key,
        })
        result0 = result
        # A stale writer may be rejected at the fence gate before it even reaches
        # the CAS check. Either way the correct client behaviour is: re-acquire,
        # re-read, retry.
        retried = result.status != "COMMITTED"
        if retried:
            # Explicit, auditable conflict -> caller re-reads and retries.
            fresh = c.acquire_lease(resource, principal)
            latest = c.read(resource, principal)
            result = c.write({
                "resource_id": resource, "principal": principal,
                "lease_id": fresh.lease_id, "expected_generation": latest.generation,
                "content": latest.content + marker, "idempotency_key": key + "-retry",
            })
        outcomes.append((result, marker, retried,
                         result0.error["code"] if result0.error else None))

    threads = [
        threading.Thread(target=writer, args=(ALICE, " + A", "a")),
        threading.Thread(target=writer, args=(BOB, " + B", "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = c.read(resource, ALICE).content
    conflicts = c.list_conflicts(resource)
    return {
        "detected_conflicts": len(conflicts),
        "retries": sum(1 for _, _, retried, _ in outcomes if retried),
        "silent_lost_updates": sum(marker not in final for _, marker, _, _ in outcomes),
        "final_integrity": int(all(marker in final for _, marker, _, _ in outcomes)),
        "audit_valid": int(c.verify_audit()[0]),
        "statuses": [item[0].status for item in outcomes],
        "fence_rejections": sum(
            1 for _, _, _, code in outcomes if code == "FENCE_REJECTED"
        ),
        "first_errors": [code for _, _, _, code in outcomes],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--output", default=str(ROOT / "results" / "benchmark.json"))
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as directory:
        baseline_lost = sum(baseline_round(directory, i) for i in range(args.rounds))

    guarded = [srcp_round(i) for i in range(args.rounds)]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rounds": args.rounds,
        "baseline": {
            "silent_lost_updates": baseline_lost,
            "rate": round(baseline_lost / (2 * args.rounds), 4),
            "note": "two uncoordinated read-modify-write agents; losses are silent",
        },
        "srcp": {
            "silent_lost_updates": sum(item["silent_lost_updates"] for item in guarded),
            "detected_conflicts": sum(item["detected_conflicts"] for item in guarded),
            "retries": sum(item["retries"] for item in guarded),
            "rounds_with_both_updates": sum(item["final_integrity"] for item in guarded),
            "rounds_with_valid_audit": sum(item["audit_valid"] for item in guarded),
            "stale_writer_rejections": sum(item["fence_rejections"] for item in guarded),
            "sample_statuses": guarded[0]["statuses"] if guarded else [],
            "sample_first_errors": guarded[0]["first_errors"] if guarded else [],
            "note": "conflicts become explicit objects; retry after re-read preserves both updates",
        },
    }
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwritten to {output}")


if __name__ == "__main__":
    main()
