"""Tests for the capability layer — the agent-facing surface built on SRCP.

These verify that capabilities inherit the protocol's correctness: no lost
updates, single-winner claims, owner-only completion, and conflict surfacing.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from srcp import Coordinator
from srcp.backends import MemoryBackend, SQLiteBackend
from srcp.capabilities import CapabilityService, normalize_principal


class CapabilityMixin:
    def make_service(self):
        raise NotImplementedError

    def test_normalize_principal(self):
        self.assertEqual(normalize_principal("hermes"), "agent:hermes@default")
        self.assertEqual(normalize_principal("agent:c@s1"), "agent:c@s1")
        self.assertEqual(normalize_principal("bad name!"), "agent:bad-name-@default")

    def test_memory_append_no_loss(self):
        s = self.make_service()
        n = 20
        threads = [
            threading.Thread(
                target=lambda i=i: s.memory_commit("lessons", f"fact-{i}", f"agent:w{i}@t")
            )
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        view = s.memory_recall("lessons", "reader", limit=100)
        self.assertEqual(view["count"], n)
        entries = {json.dumps(e["entry"]) for e in view["entries"]}
        self.assertEqual(len(entries), n)

    def test_memory_recall_filters(self):
        s = self.make_service()
        s.memory_commit("db", "WAL is faster", "a", tags=["sqlite"])
        s.memory_commit("db", "unrelated note", "b", tags=["misc"])
        found = s.memory_recall("db", "reader", tag="sqlite")
        self.assertEqual(found["count"], 1)
        self.assertIn("WAL", json.dumps(found["entries"][0]["entry"]))
        by_query = s.memory_recall("db", "reader", query="unrelated")
        self.assertEqual(by_query["count"], 1)

    def test_plan_claim_single_winner(self):
        s = self.make_service()
        outcomes = []
        barrier = threading.Barrier(6)

        def claim(name):
            barrier.wait()
            outcomes.append(s.plan_claim("board", "task", name)["status"])

        threads = [threading.Thread(target=claim, args=(f"agent:{i}@t",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("CLAIMED"), 1)
        self.assertEqual(outcomes.count("ALREADY_CLAIMED"), 5)

    def test_plan_complete_owner_only(self):
        s = self.make_service()
        s.plan_claim("b", "t", "hermes")
        denied = s.plan_complete("b", "t", "claude", {"x": 1})
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["status"], "NOT_OWNER")
        ok = s.plan_complete("b", "t", "hermes", {"done": True})
        self.assertTrue(ok["ok"])
        again = s.plan_claim("b", "t", "claude")
        self.assertEqual(again["status"], "ALREADY_DONE")

    def test_plan_claim_expires(self):
        s = self.make_service()
        s.plan_claim("b", "t", "hermes", hold_ms=1)
        time.sleep(0.02)
        second = s.plan_claim("b", "t", "claude", hold_ms=600_000)
        self.assertEqual(second["status"], "CLAIMED")

    def test_plan_status(self):
        s = self.make_service()
        s.plan_claim("b", "a", "hermes")
        s.plan_complete("b", "a", "hermes", "shipped")
        s.plan_claim("b", "b", "claude")
        status = s.plan_status("b", "owner")["tasks"]
        self.assertEqual(status["a"]["status"], "done")
        self.assertEqual(status["b"]["status"], "claimed")

    def test_json_set_concurrent_fields_both_survive(self):
        s = self.make_service()
        s.file_edit("shared/plan.json", "hermes",
                    {"kind": "json_set", "key": "research", "value": "h"})
        r = s.file_edit("shared/plan.json", "claude",
                        {"kind": "json_set", "key": "impl", "value": "c"})
        self.assertTrue(r["ok"])
        doc = json.loads(s.file_read("shared/plan.json", "owner")["content"])
        self.assertEqual(doc, {"research": "h", "impl": "c"})

    def test_json_merge(self):
        s = self.make_service()
        s.file_edit("state.json", "a", {"kind": "json_merge", "value": {"x": 1, "y": 2}})
        s.file_edit("state.json", "b", {"kind": "json_merge", "value": {"y": 3, "z": 4}})
        doc = json.loads(s.file_read("state.json", "owner")["content"])
        self.assertEqual(doc, {"x": 1, "y": 3, "z": 4})

    def test_append_op(self):
        s = self.make_service()
        s.file_edit("notes.txt", "a", {"kind": "append", "content": "first"},
                    content_type="text/plain")
        s.file_edit("notes.txt", "b", {"kind": "append", "content": "second"},
                    content_type="text/plain")
        content = s.file_read("notes.txt", "owner")["content"]
        self.assertIn("first", content)
        self.assertIn("second", content)

    def test_bad_op_rejected(self):
        s = self.make_service()
        r = s.file_edit("x.json", "a", {"kind": "unknown"})
        self.assertEqual(r["status"], "BAD_OP")
        self.assertFalse(r["ok"])

    def test_audit_chain_still_valid(self):
        s = self.make_service()
        s.memory_commit("m", "x", "a")
        s.plan_claim("b", "t", "a")
        s.file_edit("f.json", "a", {"kind": "json_set", "key": "k", "value": 1})
        self.assertTrue(s.coordinator.verify_audit()[0])


class TestCapabilitiesMemory(CapabilityMixin, unittest.TestCase):
    def make_service(self):
        return CapabilityService(Coordinator(MemoryBackend()))


class TestCapabilitiesSQLite(CapabilityMixin, unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "cap.sqlite3")

    def tearDown(self):
        self.dir.cleanup()

    def make_service(self):
        return CapabilityService(Coordinator(SQLiteBackend(self.db)))


if __name__ == "__main__":
    unittest.main()
