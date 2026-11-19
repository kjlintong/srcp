"""Backend-agnostic conformance suite.

Every scenario below must produce identical observable results on every backend.
This is the core of the project: the coordinator, not the storage engine, owns the
concurrency semantics.
"""

from __future__ import annotations

import json
import time
import unittest

from srcp.errors import InvalidResourceId, PermissionDenied

ALICE = "agent:alice@a"
BOB = "agent:bob@b"
OWNER = "agent:owner@system"


class ConformanceSuite:
    """Mixin: concrete classes only need to implement make_coordinator()."""

    def make_coordinator(self):  # pragma: no cover - abstract in mixin
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers

    def declare(self, coordinator, resource_id="shared/plan.md", **overrides):
        if "policy" in overrides:
            overrides["conflict_policy"] = overrides.pop("policy")
        payload = {"resource_id": resource_id}
        payload.update(overrides)
        return coordinator.declare_resource(payload)

    def lease(self, coordinator, resource_id="shared/plan.md", principal=ALICE, ttl=30_000):
        return coordinator.acquire_lease(resource_id, principal, ttl)

    def write(self, coordinator, resource_id, principal, lease_id, expected, content, key, **extra):
        request = {
            "resource_id": resource_id,
            "principal": principal,
            "lease_id": lease_id,
            "expected_generation": expected,
            "content": content,
            "idempotency_key": key,
        }
        request.update(extra)
        return coordinator.write(request)

    # ------------------------------------------------------------------ scenarios

    def test_01_basic_commit_and_read(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c)
        result = self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "# plan", "a-1")
        self.assertTrue(result.committed)
        self.assertEqual(result.generation, 1)
        view = c.read("shared/plan.md", ALICE)
        self.assertEqual(view.generation, 1)
        self.assertEqual(view.content, "# plan")
        self.assertTrue(view.content_hash.startswith("sha256:"))
        self.assertEqual(self.declare(c).resource_id, "shared/plan.md")  # idempotent

    def test_02_lost_update_is_blocked(self):
        c = self.make_coordinator()
        self.declare(c)
        la = self.lease(c, principal=ALICE)
        lb = self.lease(c, principal=BOB)
        first = self.write(c, "shared/plan.md", ALICE, la.lease_id, 0, "A", "a-1")
        stale = self.write(c, "shared/plan.md", BOB, lb.lease_id, 0, "B", "b-1")
        self.assertTrue(first.committed)
        self.assertEqual(stale.status, "CONFLICT")
        self.assertEqual(stale.generation, 1)
        self.assertEqual(c.read("shared/plan.md", BOB).content, "A")

    def test_03_fencing_rejects_stale_writer(self):
        c = self.make_coordinator()
        self.declare(c)
        la = self.lease(c, principal=ALICE)
        lb = self.lease(c, principal=BOB)
        self.assertLess(la.token, lb.token)
        newer = self.write(c, "shared/plan.md", BOB, lb.lease_id, 0, "new", "b-1")
        self.assertTrue(newer.committed)
        stale = self.write(c, "shared/plan.md", ALICE, la.lease_id, 0, "old", "a-1")
        self.assertEqual(stale.status, "REJECTED")
        self.assertEqual(stale.error["code"], "FENCE_REJECTED")
        self.assertEqual(c.read("shared/plan.md", ALICE).content, "new")

    def test_04_expired_lease_cannot_write(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c, ttl=60)
        time.sleep(0.10)
        result = self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "late", "a-1")
        self.assertEqual(result.error["code"], "LEASE_EXPIRED")

    def test_05_heartbeat_extends_lease(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c, ttl=150)
        time.sleep(0.06)
        renewed = c.heartbeat(lease.lease_id, ALICE)
        self.assertEqual(renewed.token, lease.token)
        time.sleep(0.10)
        result = self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "ok", "a-1")
        self.assertTrue(result.committed)

    def test_06_idempotent_retry_is_safe(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c)
        payload = dict(
            resource_id="shared/plan.md", principal=ALICE, lease_id=lease.lease_id,
            expected_generation=0, content="same", idempotency_key="retry-key",
        )
        first = c.write(payload)
        second = c.write(payload)
        self.assertTrue(first.committed)
        self.assertTrue(second.committed)
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(c.read("shared/plan.md", ALICE).generation, 1)

    def test_07_append_policy_keeps_both_writers(self):
        c = self.make_coordinator()
        self.declare(c, policy="append")
        la = self.lease(c, principal=ALICE)
        lb = self.lease(c, principal=BOB)
        a = self.write(c, "shared/plan.md", ALICE, la.lease_id, 0, "A", "a-1")
        b = self.write(c, "shared/plan.md", BOB, lb.lease_id, 0, "B", "b-1")
        self.assertTrue(a.committed)
        self.assertTrue(b.committed)
        content = c.read("shared/plan.md", ALICE).content
        self.assertIn("A", content)
        self.assertIn("B", content)
        self.assertEqual(c.read("shared/plan.md", ALICE).generation, 2)

    def test_08_merge_combines_disjoint_fields(self):
        c = self.make_coordinator()
        self.declare(c, policy="merge", content_type="application/json")
        seed_lease = self.lease(c, principal=ALICE)
        seeded = self.write(c, "shared/plan.md", ALICE, seed_lease.lease_id, 0,
                            '{"seed": true}', "seed", content_type="application/json")
        self.assertTrue(seeded.committed)
        lb = self.lease(c, principal=BOB)
        b = self.write(c, "shared/plan.md", BOB, lb.lease_id, 1,
                       '{"bob": 2}', "b-1", content_type="application/json")
        self.assertTrue(b.committed)
        la2 = self.lease(c, principal=ALICE)
        a = self.write(c, "shared/plan.md", ALICE, la2.lease_id, 1,
                       '{"alice": 1}', "a-2", content_type="application/json")
        self.assertTrue(a.committed)
        final = c.read("shared/plan.md", ALICE).content
        self.assertEqual(set(__import__("json").loads(final)), {"seed", "bob", "alice"})

    def test_09_merge_detects_semantic_conflict_and_adjudicates(self):
        c = self.make_coordinator()
        self.declare(c, policy="merge", content_type="application/json")
        lease = self.lease(c, principal=ALICE)
        self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0,
                   '{"x": 0}', "seed", content_type="application/json")
        lb = self.lease(c, principal=BOB)
        conflicted = self.write(c, "shared/plan.md", BOB, lb.lease_id, 1,
                                '{"x": 2}', "b-1", content_type="application/json")
        self.assertEqual(conflicted.status, "CONFLICT")
        self.assertEqual(conflicted.conflict["class"], "semantic-conflict")
        pending = c.list_conflicts("shared/plan.md", unresolved_only=True)
        self.assertEqual(len(pending), 1)
        # A normal write with the same field still conflicts: the protocol never
        # silently adjudicates semantic facts. Only the owner's explicit
        # adjudication may resolve it.
        decided = c.adjudicate(pending[0]["conflict_id"], OWNER, '{"x": 1}')
        self.assertTrue(decided.committed)
        self.assertEqual(json.loads(c.read("shared/plan.md", OWNER).content)["x"], 1)
        self.assertEqual(c.list_conflicts("shared/plan.md", unresolved_only=True), [])

    def test_10_lww_accepts_but_logs_conflict(self):
        c = self.make_coordinator()
        self.declare(c, policy="lww")
        la = self.lease(c, principal=ALICE)
        lb = self.lease(c, principal=BOB)
        self.write(c, "shared/plan.md", ALICE, la.lease_id, 0, "A", "a-1")
        second = self.write(c, "shared/plan.md", BOB, lb.lease_id, 0, "B", "b-1")
        self.assertTrue(second.committed)
        self.assertEqual(second.generation, 2)
        records = c.list_conflicts("shared/plan.md")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["resolution"], "lww")
        ok, problems = c.verify_audit()
        self.assertTrue(ok, problems)

    def test_11_manual_queues_for_owner(self):
        c = self.make_coordinator()
        self.declare(c, policy="manual")
        la = self.lease(c, principal=ALICE)
        lb = self.lease(c, principal=BOB)
        self.write(c, "shared/plan.md", ALICE, la.lease_id, 0, "A", "a-1")
        queued = self.write(c, "shared/plan.md", BOB, lb.lease_id, 0, "B", "b-1")
        self.assertEqual(queued.status, "QUEUED")
        self.assertIsNotNone(queued.conflict)
        self.assertEqual(c.read("shared/plan.md", ALICE).content, "A")
        pending = c.list_conflicts("shared/plan.md", unresolved_only=True)
        self.assertEqual(len(pending), 1)
        resolved = c.adjudicate(pending[0]["conflict_id"], OWNER, "B")
        self.assertTrue(resolved.committed)
        self.assertEqual(c.read("shared/plan.md", ALICE).content, "B")

    def test_12_schema_conflict_is_rejected(self):
        c = self.make_coordinator()
        self.declare(c, content_type="text/markdown")
        lease = self.lease(c)
        result = self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0,
                            "x", "a-1", content_type="application/json")
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(result.error["code"], "SCHEMA_CONFLICT")

    def test_13_unauthorized_writer_is_denied(self):
        c = self.make_coordinator()
        self.declare(c, writers=[ALICE], readers=[ALICE, BOB])
        with self.assertRaises(PermissionDenied):
            c.acquire_lease("shared/plan.md", BOB)
        self.assertIsNotNone(self.lease(c, principal=ALICE))

    def test_14_unsafe_resource_id_is_rejected(self):
        c = self.make_coordinator()
        with self.assertRaises(InvalidResourceId):
            c.declare_resource({"resource_id": "../secret.txt"})
        with self.assertRaises(InvalidResourceId):
            c.declare_resource({"resource_id": "/absolute/path"})

    def test_15_audit_chain_detects_tampering(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c)
        self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "A", "a-1")
        ok, problems = c.verify_audit()
        self.assertTrue(ok, problems)
        with c.backend.transaction() as state:
            state.audit[0]["evidence"]["forged"] = True
        ok, problems = c.verify_audit()
        self.assertFalse(ok)
        self.assertTrue(any("hash mismatch" in item for item in problems))

    def test_16_stale_read_risk_is_visible(self):
        c = self.make_coordinator()
        self.declare(c)
        self.lease(c, principal=ALICE)
        bob_view = c.read("shared/plan.md", BOB)
        self.assertEqual(bob_view.stale_risk, "concurrent-writes-may-exist")
        alice_view = c.read("shared/plan.md", ALICE)
        self.assertEqual(alice_view.stale_risk, "none")
        self.assertIsNotNone(alice_view.active_lease)

    def test_17_lease_principal_mismatch(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c, principal=ALICE)
        result = self.write(c, "shared/plan.md", BOB, lease.lease_id, 0, "X", "b-1")
        self.assertEqual(result.error["code"], "LEASE_MISMATCH")

    def test_18_policy_override_requires_owner(self):
        c = self.make_coordinator()
        self.declare(c, policy="abort")
        lease = self.lease(c, principal=BOB)
        denied = self.write(c, "shared/plan.md", BOB, lease.lease_id, 0,
                            "X", "b-1", conflict_policy="lww")
        self.assertEqual(denied.error["code"], "POLICY_DENIED")
        owner_lease = self.lease(c, principal=OWNER)
        allowed = self.write(c, "shared/plan.md", OWNER, owner_lease.lease_id, 0,
                             "X", "o-1", conflict_policy="lww")
        self.assertTrue(allowed.committed)

    def test_19_conflict_is_a_queryable_object(self):
        c = self.make_coordinator()
        self.declare(c)
        la = self.lease(c, principal=ALICE)
        lb = self.lease(c, principal=BOB)
        self.write(c, "shared/plan.md", ALICE, la.lease_id, 0, "A", "a-1")
        stale = self.write(c, "shared/plan.md", BOB, lb.lease_id, 0, "B", "b-1")
        record = stale.conflict
        self.assertIsNotNone(record)
        self.assertTrue(record["conflict_id"].startswith("cf-"))
        self.assertEqual(record["class"], "stale-write")
        self.assertEqual(record["resolution"], "aborted")
        listed = c.list_conflicts("shared/plan.md")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["conflict_id"], record["conflict_id"])

    def test_20_clean_write_creates_no_conflict(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c)
        result = self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "A", "a-1")
        self.assertIsNone(result.conflict)
        self.assertEqual(c.list_conflicts("shared/plan.md"), [])

    def test_21_audit_records_rejected_writes(self):
        c = self.make_coordinator()
        self.declare(c)
        lease = self.lease(c)
        self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "A", "a-1")
        self.write(c, "shared/plan.md", ALICE, lease.lease_id, 0, "A", "a-2")
        rejected = [e for e in c.audit_events() if e["outcome"] == "denied"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["evidence"]["error"]["code"], "FENCE_REJECTED")
