"""Run the identical conformance suite against every reference backend."""

from __future__ import annotations

import os
import tempfile
import unittest

from srcp import Coordinator
from srcp.backends import GitBackend, MemoryBackend, SQLiteBackend

from .conformance import ConformanceSuite


class MemoryConformance(ConformanceSuite, unittest.TestCase):
    def make_coordinator(self):
        return Coordinator(MemoryBackend())


class SQLiteConformance(ConformanceSuite, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def make_coordinator(self):
        return Coordinator(SQLiteBackend(os.path.join(self.directory.name, "state.sqlite3")))


class GitConformance(ConformanceSuite, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def make_coordinator(self):
        return Coordinator(GitBackend(os.path.join(self.directory.name, "repo")))


if __name__ == "__main__":
    unittest.main()
