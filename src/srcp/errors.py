"""SRCP error taxonomy. Business-level write failures are returned as WriteResult
objects (so they can be audited); programming/configuration errors raise SRPCError.
"""

from __future__ import annotations

from typing import Any


class SRPCError(Exception):
    """Base protocol error."""

    code = "INVALID_REQUEST"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to_dict()!r})"


class InvalidRequest(SRPCError):
    code = "INVALID_REQUEST"


class InvalidResourceId(SRPCError):
    code = "INVALID_RESOURCE_ID"


class InvalidPrincipal(SRPCError):
    code = "INVALID_PRINCIPAL"


class ResourceNotFound(SRPCError):
    code = "RESOURCE_NOT_FOUND"


class DeclarationConflict(SRPCError):
    code = "DECLARATION_CONFLICT"


class UnsupportedConsistency(SRPCError):
    code = "UNSUPPORTED_CONSISTENCY"


class UnsupportedPolicy(SRPCError):
    code = "UNSUPPORTED_POLICY"


class PermissionDenied(SRPCError):
    code = "PERMISSION_DENIED"


class AuditIntegrityError(SRPCError):
    code = "AUDIT_INTEGRITY_ERROR"


class ConflictNotFound(SRPCError):
    code = "CONFLICT_NOT_FOUND"


ERROR_TYPES: dict[str, type[SRPCError]] = {
    cls.code: cls
    for cls in (
        InvalidRequest,
        InvalidResourceId,
        InvalidPrincipal,
        ResourceNotFound,
        DeclarationConflict,
        UnsupportedConsistency,
        UnsupportedPolicy,
        PermissionDenied,
        AuditIntegrityError,
        ConflictNotFound,
    )
}


def error_dict(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}
