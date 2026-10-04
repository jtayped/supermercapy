"""exceptions raised by supermercapy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities import Capability


class SupermercapyError(Exception):
    """base class for all supermercapy errors."""


class ConfigurationError(SupermercapyError, ValueError):
    """the client or a request was configured with an invalid value."""


class UnsupportedOperationError(ConfigurationError):
    """the store's client does not implement an optional operation."""

    def __init__(self, capability: Capability, store: str) -> None:
        name = capability.name or str(capability)
        super().__init__(f"{store} does not support {name.lower()}")
        self.capability = capability
        self.store = store


class TransportError(SupermercapyError):
    """a request failed before supermercapy received a usable response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(TransportError):
    """the upstream service kept returning http 429."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class NotFoundError(TransportError):
    """the requested resource does not exist."""

    def __init__(self, message: str, *, status_code: int | None = 404) -> None:
        super().__init__(message, status_code=status_code)


class NotAvailableError(NotFoundError):
    """the resource exists but is not offered by the selected store or zone."""


class OutOfCoverageError(TransportError):
    """the postal code is not served by the store."""


class BlockedError(TransportError):
    """the upstream service refused the request with a bot-protection rule."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        permanent: bool = False,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after
        self.permanent = permanent


class ChallengedError(BlockedError):
    """the upstream service answered with a browser challenge instead of data."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        suggested_backoff: float = 1800.0,
    ) -> None:
        super().__init__(
            message, status_code=status_code, retry_after=suggested_backoff
        )
        self.suggested_backoff = suggested_backoff


class AuthenticationError(TransportError):
    """the client could not obtain or refresh the credentials a store needs."""


class InvalidResponseError(SupermercapyError):
    """the upstream response is not valid json or has no usable structure."""
