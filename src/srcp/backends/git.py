"""Git-backed backend: flock + atomic rename + local commit per state change.

The JSON state file is authoritative; git supplies an independent, human-readable
history (the "isolation first" path from the research: private history + merges).
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from typing import Iterator

from .base import Backend, State


class GitBackend(Backend):
    name: str = "git"

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = os.path.abspath(repo_dir)
        os.makedirs(self.repo_dir, exist_ok=True)
        self.path = os.path.join(self.repo_dir, "state.json")
        self.lock_path = os.path.join(self.repo_dir, ".srcp.lock")
        self._ensure_repo()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )

    def _ensure_repo(self) -> None:
        if not os.path.isdir(os.path.join(self.repo_dir, ".git")):
            self._git("init", "-q")
        self._git("config", "user.name", "SRCP Reference")
        self._git("config", "user.email", "srcp@local")

    @contextmanager
    def transaction(self) -> Iterator[State]:
        with open(self.lock_path, "a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if os.path.exists(self.path):
                    with open(self.path, encoding="utf-8") as handle:
                        state = State.from_dict(json.load(handle))
                else:
                    state = State()
                try:
                    yield state
                except BaseException:
                    raise
                else:
                    tmp = self.path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as handle:
                        json.dump(state.to_dict(), handle, ensure_ascii=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, self.path)
                    self._git("add", "state.json")
                    self._git(
                        "-c", "user.name=SRCP Reference",
                        "-c", "user.email=srcp@local",
                        "commit", "-q", "-m", "srcp: state update",
                    )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
