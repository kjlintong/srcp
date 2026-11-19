"""Pluggable persistence backends. The protocol logic lives in Coordinator;
backends only provide an atomic load/modify/save transaction."""

from .base import Backend, State
from .git import GitBackend
from .memory import MemoryBackend
from .sqlite import SQLiteBackend

__all__ = ["Backend", "State", "MemoryBackend", "SQLiteBackend", "GitBackend"]
