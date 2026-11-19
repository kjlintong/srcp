"""Protocol-level tests: schemas, parity, chaos, transports and negative controls."""

from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from srcp import Coordinator
from srcp.backends import GitBackend, MemoryBackend, SQLiteBackend
from srcp.version import vv_bump, vv_compare, vv_merge

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "spec" / "schema"
ALICE = "agent:alice@a"
BOB = "agent:bob@b"
OWNER = "agent:owner@system"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def cas_worker(db_path: str, principal: str, content: str, queue) -> None:
    """Runs in a separate process; forces SQLite's real cross-process lock."""
    coordinator = Coordinator(SQLiteBackend(db_path))
    lease = coordinator.acquire_lease("shared/x", principal)
    result = coordinator.write({
        "resource_id": "shared/x",
        "principal": principal,
        "lease_id": lease.lease_id,
        "expected_generation": 0,
        "content": content,
        "idempotency_key": principal,
    })
    queue.put({"principal": principal, "content": content,
               "status": result.status, "generation": result.generation})


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.c = Coordinator(MemoryBackend())

    def test_wire_objects_match_published_schemas(self):
        decl = self.c.declare_resource({"resource_id": "shared/plan.md"})
        jsonschema.validate(decl.to_dict(), load_schema("resource.json"))
        lease = self.c.acquire_lease("shared/plan.md", ALICE)
        jsonschema.validate(lease.to_dict(), load_schema("lease.json"))
        view = self.c.read("shared/plan.md", ALICE)
        jsonschema.validate(view.to_dict(), load_schema("read-view.json"))
        result = self.c.write({
            "resource_id": "shared/plan.md", "principal": ALICE,
            "lease_id": lease.lease_id, "expected_generation": 0,
            "content": "hello", "idempotency_key": "k1",
        })
        jsonschema.validate(result.to_dict(), load_schema("write-result.json"))
        jsonschema.validate(self.c.capabilities(), load_schema("capabilities.json"))
        for event in self.c.audit_events():
            jsonschema.validate(event, load_schema("audit.json"))

    def test_conflict_object_matches_schema(self):
        self.c.declare_resource({"resource_id": "shared/plan.md"})
        la = self.c.acquire_lease("shared/plan.md", ALICE)
        lb = self.c.acquire_lease("shared/plan.md", BOB)
        self.c.write({"resource_id": "shared/plan.md", "principal": ALICE,
                      "lease_id": la.lease_id, "expected_generation": 0,
                      "content": "A", "idempotency_key": "a"})
        result = self.c.write({"resource_id": "shared/plan.md", "principal": BOB,
                               "lease_id": lb.lease_id, "expected_generation": 0,
                               "content": "B", "idempotency_key": "b"})
        jsonschema.validate(result.conflict, load_schema("conflict.json"))


class CrossBackendParityTests(unittest.TestCase):
    @staticmethod
    def scenario(coordinator) -> tuple:
        coordinator.declare_resource({"resource_id": "shared/x"})
        la = coordinator.acquire_lease("shared/x", ALICE)
        lb = coordinator.acquire_lease("shared/x", BOB)
        first = coordinator.write({
            "resource_id": "shared/x", "principal": ALICE, "lease_id": la.lease_id,
            "expected_generation": 0, "content": "A", "idempotency_key": "a",
        })
        stale = coordinator.write({
            "resource_id": "shared/x", "principal": BOB, "lease_id": lb.lease_id,
            "expected_generation": 0, "content": "B", "idempotency_key": "b",
        })
        view = coordinator.read("shared/x", BOB)
        return (
            first.status, first.generation,
            stale.status, stale.generation, stale.error["code"],
            view.content, view.generation,
            len(coordinator.list_conflicts()),
            coordinator.verify_audit()[0],
        )

    def test_all_backends_produce_identical_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinators = [
                Coordinator(MemoryBackend()),
                Coordinator(SQLiteBackend(os.path.join(directory, "state.sqlite3"))),
                Coordinator(GitBackend(os.path.join(directory, "repo"))),
            ]
            outcomes = [self.scenario(item) for item in coordinators]
            for other in outcomes[1:]:
                self.assertEqual(outcomes[0], other)


class CrossProcessTests(unittest.TestCase):
    def test_sqlite_cas_is_atomic_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "state.sqlite3")
            Coordinator(SQLiteBackend(db_path)).declare_resource({"resource_id": "shared/x"})
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            processes = [
                context.Process(target=cas_worker, args=(db_path, ALICE, "from-A", queue)),
                context.Process(target=cas_worker, args=(db_path, BOB, "from-B", queue)),
            ]
            for process in processes:
                process.start()
            results = [queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            committed = [item for item in results if item["status"] == "COMMITTED"]
            self.assertEqual(len(committed), 1)
            final = Coordinator(SQLiteBackend(db_path)).read("shared/x", ALICE)
            self.assertEqual(final.generation, 1)
            self.assertEqual(final.content, committed[0]["content"])


class GitBackendTests(unittest.TestCase):
    def test_every_state_change_creates_a_git_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = os.path.join(directory, "repo")
            c = Coordinator(GitBackend(repo))
            c.declare_resource({"resource_id": "shared/x"})
            lease = c.acquire_lease("shared/x", ALICE)
            c.write({"resource_id": "shared/x", "principal": ALICE,
                     "lease_id": lease.lease_id, "expected_generation": 0,
                     "content": "A", "idempotency_key": "a"})
            log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                                 capture_output=True, text=True, check=True).stdout
            self.assertGreaterEqual(len(log.strip().splitlines()), 3)


class TransportTests(unittest.TestCase):
    @staticmethod
    def rpc_line(identifier: int, method: str, params: dict | None = None) -> str:
        message = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            message["params"] = params
        return json.dumps(message)

    def run_server(self, lines: list[str], db_path: str) -> list[dict]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["SRCP_DB"] = db_path
        completed = subprocess.run(
            [sys.executable, "-m", "srcp.mcp_server"],
            input="\n".join(lines) + "\n", text=True, capture_output=True,
            env=environment, cwd=ROOT, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]

    def test_mcp_stdio_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "state.sqlite3")
            responses = self.run_server([
                self.rpc_line(1, "initialize"),
                self.rpc_line(2, "tools/list"),
                self.rpc_line(3, "tools/call", {"name": "srcp_declare_resource",
                                                "arguments": {"resource_id": "shared/x"}}),
                self.rpc_line(4, "tools/call", {"name": "srcp_acquire_lease",
                                                "arguments": {"resource_id": "shared/x",
                                                              "principal": ALICE}}),
            ], db_path)
            self.assertEqual(len(responses), 4)
            self.assertGreaterEqual(len(responses[1]["result"]["tools"]), 10)
            lease = responses[3]["result"]["structuredContent"]["lease"]
            second = self.run_server([
                self.rpc_line(5, "tools/call", {
                    "name": "srcp_write",
                    "arguments": {
                        "resource_id": "shared/x", "principal": ALICE,
                        "lease_id": lease["lease_id"], "expected_generation": 0,
                        "content": "written over MCP", "idempotency_key": "mcp-1",
                    },
                }),
            ], db_path)
            payload = second[0]["result"]["structuredContent"]
            self.assertEqual(payload["status"], "COMMITTED")
            self.assertEqual(payload["generation"], 1)

    def test_cli_reports_conflict_as_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "state.sqlite3")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src")
            base = [sys.executable, "-m", "srcp.cli", "--db", db_path]
            def run(*args):
                return subprocess.run(base + list(args), text=True, capture_output=True,
                                      env=environment, cwd=ROOT, timeout=30)
            run("declare", "--resource-id", "shared/x")
            lease = json.loads(run("lease", "--resource-id", "shared/x",
                                   "--principal", ALICE).stdout)
            first = run("write", "--resource-id", "shared/x", "--principal", ALICE,
                        "--lease-id", lease["lease_id"], "--expected-generation", "0",
                        "--content", "A", "--key", "a")
            self.assertEqual(first.returncode, 0)
            other = json.loads(run("lease", "--resource-id", "shared/x",
                                   "--principal", BOB).stdout)
            stale = run("write", "--resource-id", "shared/x", "--principal", BOB,
                        "--lease-id", other["lease_id"], "--expected-generation", "0",
                        "--content", "B", "--key", "b")
            self.assertEqual(stale.returncode, 1)
            self.assertEqual(json.loads(stale.stdout)["status"], "CONFLICT")


class NegativeControlTests(unittest.TestCase):
    def test_uncoordinated_baseline_really_loses_updates(self):
        """Without SRCP, two readers both write back and one update disappears."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "plan.md"
            path.write_text("v0", encoding="utf-8")
            alice_view = path.read_text(encoding="utf-8")
            bob_view = path.read_text(encoding="utf-8")
            path.write_text(alice_view + " + A", encoding="utf-8")
            path.write_text(bob_view + " + B", encoding="utf-8")
            self.assertEqual(path.read_text(encoding="utf-8"), "v0 + B")


class VersionVectorTests(unittest.TestCase):
    def test_comparison_and_bump(self):
        self.assertEqual(vv_compare({"a": 1}, {"a": 1}), "equal")
        self.assertEqual(vv_compare({"a": 1}, {"a": 2}), "before")
        self.assertEqual(vv_compare({"a": 2}, {"a": 1}), "after")
        self.assertEqual(vv_compare({"a": 1}, {"b": 1}), "concurrent")
        self.assertEqual(vv_merge({"a": 2}, {"a": 1, "b": 3}), {"a": 2, "b": 3})
        self.assertEqual(vv_bump({"a": 2}, "a")["a"], 3)


if __name__ == "__main__":
    unittest.main()
