"""Stable, non-disclosing errors returned by the application layer."""


class MemoryError(Exception):
    """Base class for expected memory-service failures."""


class AuthorizationDenied(MemoryError):
    """The capability is missing, invalid, expired, or insufficient."""


class NotFoundOrDenied(MemoryError):
    """The requested object is unavailable without revealing why."""


class InvalidMemoryPath(MemoryError):
    """A virtual path is invalid. The rejected path is intentionally omitted."""


class VersionConflict(MemoryError):
    """The expected version no longer matches the current version."""


class EditConflict(MemoryError):
    """An exact-text edit has no match, ambiguous matches, or no change."""


class IdempotencyConflict(MemoryError):
    """An idempotency key was reused for a different request."""


class QuotaExceeded(MemoryError):
    """The configured memory quota would be exceeded."""


class RateLimitExceeded(MemoryError):
    """The current scope exceeded its configured operation rate."""


class IntegrityFailure(MemoryError):
    """Encrypted storage failed authentication or was malformed."""


class ConfigurationError(MemoryError):
    """The host configuration is incomplete or unsafe."""
