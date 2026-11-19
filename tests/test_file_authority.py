"""Tests for disk authority — proving the shadow copy is eliminated.

These verify the real file, not SRCP's blob, is the content authority, and that
an out-of-band edit can never be silently overwritten.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from srcp import Coordinator
from srcp.backends import MemoryBackend, SQLiteBackend
from srcp.capabilities import CapabilityService
from srcp.errors import InvalidRequest, PermissionDenied
from srcp.file_authority import FileAuthority

OWNER = "agent:owner@system"


class FileAuthorityMixin:
    def make(self, root):
        raise NotImplementedError

    def _write_disk(self, fa, rel, content):
        path = fa.resolve(rel)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _read_disk(self, fa, rel):
        with open(fa.resolve(rel), encoding="utf-8") as handle:
            return handle.read()

    def test_bind_seeds_from_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            self._write_disk(fa, "notes.md", "original content")
            result = fa.bind("files/notes.md", "notes.md", OWNER)
            self.assertTrue(result["ok"])
            view = fa.read("files/notes.md", "agent:a@x")
            self.assertEqual(view["content"], "original content")
            self.assertEqual(view["generation"], 0)
            self.assertEqual(view["authority"], "disk")

    def test_protocol_write_lands_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            result = fa.write("f", "agent:a@x", "written via protocol", 0)
            self.assertTrue(result["ok"])
            self.assertEqual(self._read_disk(fa, "plan.md"), "written via protocol")
            # And the metadata hash matches the real file.
            self.assertEqual(result["content_hash"], fa.hash_file(fa.resolve("plan.md")))

    def test_out_of_band_edit_detected_on_read(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            fa.write("f", "agent:a@x", "protocol value", 0)
            self._write_disk(fa, "plan.md", "raw tool value")
            view = fa.read("f", "agent:a@x")
            self.assertEqual(view["status"], "AUTHORITY_CONFLICT")
            self.assertEqual(view["disk_content"], "raw tool value")
            self.assertEqual(view["srcp_content"], "protocol value")

    def test_out_of_band_edit_blocks_write(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            fa.write("f", "agent:a@x", "v1", 0)
            self._write_disk(fa, "plan.md", "oob")
            result = fa.write("f", "agent:b@x", "v2", 1)
            self.assertEqual(result["status"], "AUTHORITY_CONFLICT")
            self.assertEqual(self._read_disk(fa, "plan.md"), "oob")

    def test_adopt_brings_disk_back_in_sync(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            fa.write("f", "agent:a@x", "v1", 0)
            self._write_disk(fa, "plan.md", "oob")
            with self.assertRaises(PermissionDenied):
                fa.adopt("f", "agent:notowner@x")
            adopted = fa.adopt("f", OWNER, note="reviewed")
            self.assertEqual(adopted["status"], "ADOPTED")
            self.assertEqual(adopted["generation"], 2)
            view = fa.read("f", "agent:a@x")
            self.assertEqual(view["content"], "oob")
            # After adopt, writes work again and land on disk.
            result = fa.write("f", "agent:b@x", "v3", adopted["generation"])
            self.assertEqual(result["status"], "COMMITTED")
            self.assertEqual(self._read_disk(fa, "plan.md"), "v3")

    def test_adopt_requires_owner(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            self._write_disk(fa, "plan.md", "oob")
            with self.assertRaises(PermissionDenied):
                fa.adopt("f", "agent:writer@x")

    def test_hash_guard_between_read_and_write(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            fa.write("f", "agent:a@x", "v1", 0)
            view = fa.read("f", "agent:a@x")
            # Simulate a raw edit AFTER read but BEFORE write.
            self._write_disk(fa, "plan.md", "changed in the gap")
            result = fa.write("f", "agent:a@x", "v2", view["generation"],
                              expected_content_hash=view["content_hash"])
            self.assertEqual(result["status"], "AUTHORITY_CONFLICT")

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "ws")
            fa = self.make(root)
            for bad in ("../outside", "/etc/passwd", "a/../../escape"):
                with self.assertRaises(InvalidRequest):
                    fa.resolve(bad)
            # Paths that normalize back inside root are safe (`x/../y` -> `y`).
            self.assertTrue(fa.resolve("x/../y").startswith(os.path.realpath(root)))
            self.assertTrue(fa.resolve("a/b/c.md").startswith(os.path.realpath(root)))

    def test_binding_conflict_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "one.md", OWNER)
            with self.assertRaises(Exception):
                fa.bind("f", "two.md", OWNER)

    def test_write_requires_binding(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            with self.assertRaises(InvalidRequest):
                fa.write("unbound", "agent:a@x", "x", 0)

    def test_audit_chain_valid_after_all_ops(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "plan.md", OWNER)
            fa.write("f", "agent:a@x", "v1", 0)
            self._write_disk(fa, "plan.md", "oob")
            fa.read("f", "agent:a@x")
            fa.adopt("f", OWNER)
            self.assertTrue(fa.coordinator.verify_audit()[0])

    def test_capability_layer_uses_disk_authority(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            service = CapabilityService(fa.coordinator, file_authority=fa)
            service.file_bind("f", "shared.json", OWNER, content_type="application/json")
            r1 = service.file_edit("f", "agent:hermes@x",
                                   {"kind": "json_set", "key": "a", "value": 1})
            self.assertTrue(r1["ok"])
            r2 = service.file_edit("f", "agent:claude@x",
                                   {"kind": "json_set", "key": "b", "value": 2})
            self.assertTrue(r2["ok"])
            on_disk = json.loads(self._read_disk(fa, "shared.json"))
            self.assertEqual(on_disk, {"a": 1, "b": 2})
            # Out-of-band edit surfaces through the capability too.
            self._write_disk(fa, "shared.json", '{"a": 99}')
            conflict = service.file_edit("f", "agent:hermes@x",
                                         {"kind": "json_set", "key": "c", "value": 3})
            self.assertEqual(conflict["status"], "AUTHORITY_CONFLICT")

    def test_stale_generation_write_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            fa = self.make(os.path.join(d, "ws"))
            fa.bind("f", "race.md", OWNER)
            view_a = fa.read("f", "agent:a@x")
            view_b = fa.read("f", "agent:b@x")  # both read generation 0
            first = fa.write("f", "agent:a@x", "value-a", view_a["generation"])
            self.assertEqual(first["status"], "COMMITTED")
            stale = fa.write("f", "agent:b@x", "value-b", view_b["generation"])
            self.assertNotIn(stale["status"], ("COMMITTED", "OK"))
            self.assertEqual(self._read_disk(fa, "race.md"), "value-a")


class TestFileAuthorityMemory(FileAuthorityMixin, unittest.TestCase):
    def make(self, root):
        return FileAuthority(Coordinator(MemoryBackend()), root)


class TestFileAuthoritySQLite(FileAuthorityMixin, unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def make(self, root):
        db = os.path.join(self.dir.name, "fa.sqlite3")
        return FileAuthority(Coordinator(SQLiteBackend(db)), root)


if __name__ == "__main__":
    unittest.main()
