"""SRCP — Shared Resource Coordination Protocol reference implementation."""

__version__ = "0.1.0"

from .coordinator import Coordinator
from .errors import SRPCError
from .types import ResourceDecl, WriteRequest, WriteResult

__all__ = [
    "Coordinator",
    "SRPCError",
    "ResourceDecl",
    "WriteRequest",
    "WriteResult",
    "__version__",
]
