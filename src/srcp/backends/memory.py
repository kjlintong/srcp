"""In-process backend. Useful for unit tests and embedded single-process agents."""

from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Iterator

from .base import Backend, State


class MemoryBackend(Backend):
    name: str = "memory"

    def __init__(self) -> None:
        self._state = State()
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[State]:
        with self._lock:
            working = copy.deepcopy(self._state)
            try:
                yield working
            except BaseException:
                raise
            else:
                self._state = working
