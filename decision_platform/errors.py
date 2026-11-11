"""Typed error hierarchy.

Every error carries a stable machine-readable ``code`` and the HTTP status the
API layer should map it to, so transport concerns never leak into the domain.
"""


class PlatformError(Exception):
    code = "platform_error"
    http_status = 500

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self):
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(PlatformError):
    code = "validation_failed"
    http_status = 422


class ConfigError(PlatformError):
    code = "invalid_config"
    http_status = 400


class NotFound(PlatformError):
    code = "not_found"
    http_status = 404


class IdempotencyConflict(PlatformError):
    """Same idempotency key re-used with a different payload."""

    code = "idempotency_conflict"
    http_status = 409


class InvalidTransition(PlatformError):
    code = "invalid_state_transition"
    http_status = 409


class ConcurrencyConflict(PlatformError):
    """Optimistic-lock or processing-lease conflict."""

    code = "concurrency_conflict"
    http_status = 409


class StorageError(PlatformError):
    code = "storage_failure"
    http_status = 503


# --- external dependency errors -------------------------------------------

class DependencyError(PlatformError):
    code = "dependency_error"
    http_status = 502


class TransientDependencyError(DependencyError):
    """Retryable: timeouts, 5xx, connection resets."""

    code = "dependency_transient_error"


class PermanentDependencyError(DependencyError):
    """Not retryable: bad request, unknown subject, contract violation."""

    code = "dependency_permanent_error"


class DependencyUnavailable(DependencyError):
    """Retries exhausted or circuit breaker open."""

    code = "dependency_unavailable"
    http_status = 503
