"""the shared transport and standard interface every store client builds on."""

from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum, auto
from math import isfinite
from pathlib import Path
from random import SystemRandom
from secrets import token_hex
from threading import Lock
from types import TracebackType
from typing import Any, ClassVar, Self

import httpx

from .._version import __version__
from .capabilities import Capability
from .coerce import JsonObject
from .exceptions import (
    AuthenticationError,
    BlockedError,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    RateLimitError,
    TransportError,
    UnsupportedOperationError,
)
from .models import (
    Category,
    HomeSection,
    Language,
    Photo,
    Product,
    ProductSummary,
    SearchResult,
    Store,
)

_LOGGER = logging.getLogger("supermercapy")
_POSTAL_CODE_PATTERN = re.compile(r"^(?:0[1-9]|[1-4][0-9]|5[0-2])\d{3}$")
_RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})
_JITTER = SystemRandom()
# no explicit image/webp or image/avif: cdns that see them swap the format of
# the file behind its name, so a ``.jpg`` destination would hold webp bytes
_IMAGE_HEADERS = {"Accept": "*/*"}
DEFAULT_USER_AGENT = f"supermercapy/{__version__}"


class ResponseVerdict(Enum):
    """what the retry loop should do with a response it just received."""

    OK = auto()
    RETRY = auto()
    RATE_LIMITED = auto()
    NOT_FOUND = auto()
    AUTH_EXPIRED = auto()
    CHALLENGED = auto()
    BLOCKED = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """bounded retry settings for connection and transient http failures."""

    max_attempts: int = 3
    backoff_factor: float = 0.25
    max_delay: float = 5.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            raise ConfigurationError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ConfigurationError("max_attempts must be at least one")
        if (
            isinstance(self.backoff_factor, bool)
            or not isinstance(self.backoff_factor, (int, float))
            or not isfinite(self.backoff_factor)
            or self.backoff_factor < 0
        ):
            raise ConfigurationError(
                "backoff_factor must be a finite non-negative number"
            )
        if (
            isinstance(self.max_delay, bool)
            or not isinstance(self.max_delay, (int, float))
            or not isfinite(self.max_delay)
            or self.max_delay < 0
        ):
            raise ConfigurationError("max_delay must be a finite non-negative number")
        if (
            isinstance(self.jitter_ratio, bool)
            or not isinstance(self.jitter_ratio, (int, float))
            or not isfinite(self.jitter_ratio)
            or not 0 <= self.jitter_ratio <= 1
        ):
            raise ConfigurationError("jitter_ratio must be between zero and one")


def validate_postal_code(postal_code: str) -> str:
    """validate and return a five-digit spanish postal code."""

    if not isinstance(postal_code, str) or not _POSTAL_CODE_PATTERN.fullmatch(
        postal_code
    ):
        raise ConfigurationError(
            "postal_code must contain five digits with a province prefix from 01 to 52"
        )
    return postal_code


def validate_identifier(value: str | int, label: str) -> str:
    """return a stripped string id from a non-empty string or integer."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigurationError(f"{label} must be a non-empty string or integer")
    result = str(value).strip()
    if not result:
        raise ConfigurationError(f"{label} must be a non-empty string or integer")
    return result


def validate_timeout(timeout: float | httpx.Timeout) -> float | httpx.Timeout:
    """validate a single timeout number or an ``httpx.Timeout``."""

    if isinstance(timeout, httpx.Timeout):
        values = (timeout.connect, timeout.read, timeout.write, timeout.pool)
        if any(
            value is not None and (not isfinite(value) or value <= 0)
            for value in values
        ):
            raise ConfigurationError(
                "timeout values must be finite and greater than zero"
            )
        return timeout
    if isinstance(timeout, bool):
        raise ConfigurationError("timeout must be a finite number greater than zero")
    try:
        value = float(timeout)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("timeout must be a valid httpx timeout") from error
    if not isfinite(value) or value <= 0:
        raise ConfigurationError("timeout must be a finite number greater than zero")
    return value


def validate_request_interval(value: float) -> float:
    """validate a minimum gap between request starts."""

    if isinstance(value, bool):
        raise ConfigurationError(
            "min_request_interval must be a finite non-negative number"
        )
    try:
        interval = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(
            "min_request_interval must be a finite non-negative number"
        ) from error
    if not isfinite(interval) or interval < 0:
        raise ConfigurationError(
            "min_request_interval must be a finite non-negative number"
        )
    return interval


def _create_sibling(destination: Path) -> tuple[int, Path]:
    """open a new, empty temporary file in ``destination``'s directory.

    ``tempfile.mkstemp`` would create it readable by its owner alone, and
    ``os.replace`` keeps that mode, so the file is opened with the mode a
    plain ``open()`` gives a new file: 0o666 less the process umask.
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    while True:
        path = destination.with_name(f".{destination.name}.{token_hex(8)}.tmp")
        try:
            return os.open(path, flags, 0o666), path
        except FileExistsError:
            continue


class BaseClient(ABC):
    """shared transport, retries, pacing, and the standard storefront interface.

    subclasses set the class attributes, implement the four required methods,
    override the optional methods whose capability they declare, and adjust
    transport behaviour through the ``_prime``, ``_prepare_request``,
    ``_classify``, ``_on_auth_expired``, and ``_on_challenge`` hooks.
    """

    store_name: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    supported_languages: ClassVar[frozenset[Language]] = frozenset({Language.SPANISH})
    default_language: ClassVar[Language] = Language.SPANISH
    default_user_agent: ClassVar[str] = DEFAULT_USER_AGENT
    default_min_request_interval: ClassVar[float] = 0.0
    default_page_size: ClassVar[int] = 24
    max_page_size: ClassVar[int | None] = None

    def __init__(
        self,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._language = self._validate_language(language)
        self._retry_policy = retry_policy or RetryPolicy()
        self._min_request_interval = validate_request_interval(
            self.default_min_request_interval
            if min_request_interval is None
            else min_request_interval
        )
        if user_agent is not None and (
            not isinstance(user_agent, str) or not user_agent.strip()
        ):
            raise ConfigurationError("user_agent must be a non-empty string")
        self._user_agent = user_agent or self.default_user_agent
        self._logger = _LOGGER.getChild(self.store_name) if self.store_name else _LOGGER
        self._request_lock = Lock()
        self._last_request_started_at: float | None = None
        self._primed_at: float | None = None
        self._priming = False
        timeout_config = validate_timeout(timeout)
        try:
            self._client = httpx.Client(
                timeout=timeout_config,
                transport=transport,
                headers=self._default_headers(),
                follow_redirects=False,
            )
        except (TypeError, ValueError) as error:
            raise ConfigurationError("timeout must be a valid httpx timeout") from error
        self._closed = False

    @classmethod
    def _validate_language(cls, language: Language | str | None) -> Language:
        if language is None:
            return cls.default_language
        try:
            value = Language(language)
        except ValueError:
            value = None
        if value is None or value not in cls.supported_languages:
            supported = ", ".join(
                repr(item.value) for item in sorted(cls.supported_languages)
            )
            raise ConfigurationError(f"language must be one of {supported}")
        return value

    # ------------------------------------------------------------------ lifecycle

    @property
    def language(self) -> Language:
        """return the selected storefront language."""

        return self._language

    @property
    def store_id(self) -> str | None:
        """return the store, warehouse, or zone this client is bound to."""

        return None

    @property
    def is_closed(self) -> bool:
        """return whether the client has been closed."""

        return self._closed

    @property
    def min_request_interval(self) -> float:
        """return the minimum time between request starts in seconds."""

        return self._min_request_interval

    @property
    def user_agent(self) -> str:
        """return the user agent sent with every request."""

        return self._user_agent

    @classmethod
    def supports(cls, capability: Capability) -> bool:
        """return whether this store's client declares ``capability``."""

        return capability in cls.capabilities

    def close(self) -> None:
        """close the http connection pool."""

        if not self._closed:
            self._client.close()
            self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------- required interface

    @abstractmethod
    def search_products(
        self,
        query: str,
        *,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> SearchResult:
        """return one page of summaries; pass ``next_cursor`` back to continue."""

    @abstractmethod
    def get_product(self, product_id: str | int) -> Product:
        """return a complete product record by store id."""

    @abstractmethod
    def get_categories(self) -> tuple[Category, ...]:
        """return the root of the storefront category tree."""

    @abstractmethod
    def get_category(self, category_id: str | int) -> Category:
        """return one category with its children and listed products."""

    # ------------------------------------------------------- concrete generics

    def iter_search(
        self, query: str, *, page_size: int | None = None
    ) -> Iterator[ProductSummary]:
        """yield summaries for ``query`` across every page."""

        cursor: str | None = None
        while True:
            result = self.search_products(query, page_size=page_size, cursor=cursor)
            yield from result.products
            if result.next_cursor is None or result.next_cursor == cursor:
                return
            cursor = result.next_cursor

    def get_catalog(self) -> tuple[ProductSummary, ...]:
        """return every catalog summary once, keyed on product id."""

        self._require(Capability.CATALOG)
        products: dict[str, ProductSummary] = {}
        for product in self.iter_catalog():
            products.setdefault(product.id, product)
        return tuple(products.values())

    def download(
        self, source: Photo | str, destination: str | os.PathLike[str]
    ) -> Path:
        """stream an image to ``destination`` through an atomic replacement."""

        if isinstance(source, Photo):
            url = source.url
        elif isinstance(source, str) and source.strip():
            url = source
        else:
            raise ConfigurationError("source must be a Photo or a non-empty url")
        destination_path = Path(destination)
        if destination_path.exists() and destination_path.is_dir():
            raise ConfigurationError("destination must be a file path")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = _create_sibling(destination_path)
        try:
            with (
                os.fdopen(descriptor, "wb") as output,
                self._stream(
                    "GET", url, headers=_IMAGE_HEADERS, follow_redirects=True
                ) as response,
            ):
                try:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
                except httpx.RequestError as error:
                    raise TransportError(f"download failed: {error}") from error
            os.replace(temporary_path, destination_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return destination_path

    # ------------------------------------------------------- optional interface

    @classmethod
    def from_postal_code(
        cls,
        postal_code: str,
        *,
        language: Language | str | None = None,
        timeout: float | httpx.Timeout = 10.0,
        retry_policy: RetryPolicy | None = None,
        min_request_interval: float | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> Self:
        """resolve a postal code to a store binding and return a client."""

        raise UnsupportedOperationError(Capability.POSTAL_CODE, cls.store_name)

    def list_stores(self, postal_code: str | None = None) -> tuple[Store, ...]:
        """return the stores or zones the storefront can be bound to."""

        raise UnsupportedOperationError(Capability.STORES, self.store_name)

    def iter_catalog(self) -> Iterator[ProductSummary]:
        """yield every catalog summary, possibly with duplicates."""

        raise UnsupportedOperationError(Capability.CATALOG, self.store_name)

    def get_product_by_ean(self, ean: str) -> Product:
        """return a complete product record by its ean."""

        raise UnsupportedOperationError(Capability.EAN_LOOKUP, self.store_name)

    def get_new_arrivals(self) -> tuple[ProductSummary, ...]:
        """return the products the storefront presents as new."""

        raise UnsupportedOperationError(Capability.NEW_ARRIVALS, self.store_name)

    def get_offers(self) -> tuple[ProductSummary, ...]:
        """return the products currently on offer."""

        raise UnsupportedOperationError(Capability.OFFERS, self.store_name)

    def get_home(self) -> tuple[HomeSection, ...]:
        """return the ordered sections of the storefront home page."""

        raise UnsupportedOperationError(Capability.HOME, self.store_name)

    # ---------------------------------------------------------- transport hooks

    def _default_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": self._language.value,
            "User-Agent": self._user_agent,
        }

    def _prime(self) -> None:  # noqa: B027
        """perform any one-off session warm-up; the default does nothing."""

    def _prime_ttl(self) -> float | None:
        """return how many seconds a warm-up stays valid, or ``None`` for ever."""

        return None

    def _prepare_request(self, request: httpx.Request) -> None:  # noqa: B027
        """mutate a built request before it is sent; the default does nothing."""

    def _classify(self, response: httpx.Response) -> ResponseVerdict:
        status = response.status_code
        if status == 429:
            return ResponseVerdict.RATE_LIMITED
        if status in _RETRYABLE_STATUS_CODES:
            return ResponseVerdict.RETRY
        if status == 404:
            return ResponseVerdict.NOT_FOUND
        if status >= 400:
            return ResponseVerdict.ERROR
        return ResponseVerdict.OK

    def _on_auth_expired(self) -> bool:
        """refresh credentials and return whether the request may be retried."""

        return False

    def _on_challenge(self, response: httpx.Response) -> bool:
        """react to a challenge and return whether the request may be retried."""

        return False

    # ---------------------------------------------------- transport primitives

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        def send() -> httpx.Response:
            request = self._client.build_request(method, url, **kwargs)
            self._prepare_request(request)
            return self._client.send(request)

        return self._send_with_retries(send, method=method, url=url)

    def _request_json(self, method: str, url: str, **kwargs: Any) -> JsonObject:
        data = self._request_json_any(method, url, **kwargs)
        if not isinstance(data, dict):
            raise InvalidResponseError(
                f"{method} {url} returned a non-object JSON value"
            )
        return data

    def _request_json_any(self, method: str, url: str, **kwargs: Any) -> object:
        response = self._request(method, url, **kwargs)
        if response.is_redirect:
            # an api that starts redirecting has moved; say so rather than
            # failing later on the empty body as if it were malformed json
            location = response.headers.get("Location", "")
            raise TransportError(
                f"{method} {url} redirected (HTTP {response.status_code}) "
                f"to {location or 'nowhere'}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as error:
            raise InvalidResponseError(
                f"{method} {url} returned invalid JSON"
            ) from error

    def _request_text(self, method: str, url: str, **kwargs: Any) -> str:
        return self._request(method, url, **kwargs).text

    @contextmanager
    def _stream(
        self,
        method: str,
        url: str,
        *,
        follow_redirects: bool = False,
        **kwargs: Any,
    ) -> Iterator[httpx.Response]:
        def send() -> httpx.Response:
            request = self._client.build_request(method, url, **kwargs)
            self._prepare_request(request)
            return self._client.send(
                request, stream=True, follow_redirects=follow_redirects
            )

        response = self._send_with_retries(send, method=method, url=url)
        try:
            yield response
        finally:
            response.close()

    def _send_with_retries(
        self,
        send: Callable[[], httpx.Response],
        *,
        method: str,
        url: str,
    ) -> httpx.Response:
        self._ensure_open()
        self._ensure_primed()
        policy = self._retry_policy
        attempt = 0
        auth_retried = False
        while True:
            self._wait_for_request_slot()
            started = time.perf_counter()
            try:
                response = send()
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                if attempt + 1 >= policy.max_attempts:
                    raise TransportError(
                        f"{method} {url} failed after "
                        f"{policy.max_attempts} connection attempts: {error}"
                    ) from error
                delay = self._backoff(attempt)
                self._logger.info(
                    "retry %s of %s for %s %s: %s, sleeping %.2fs",
                    attempt + 2,
                    policy.max_attempts,
                    method,
                    url,
                    type(error).__name__,
                    delay,
                )
                time.sleep(delay)
                attempt += 1
                continue
            except httpx.RequestError as error:
                raise TransportError(f"{method} {url} failed: {error}") from error

            self._logger.debug(
                "%s %s -> %s in %.0f ms",
                method,
                response.request.url,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
            verdict = self._classify(response)
            if verdict is ResponseVerdict.OK:
                return response
            if verdict is ResponseVerdict.AUTH_EXPIRED and not auth_retried:
                response.close()
                auth_retried = True
                self._logger.info(
                    "refreshing credentials after HTTP %s for %s %s",
                    response.status_code,
                    method,
                    url,
                )
                if self._on_auth_expired():
                    continue
                raise AuthenticationError(
                    f"{method} {url} was rejected and credentials could not be "
                    "refreshed",
                    status_code=response.status_code,
                )
            if verdict is ResponseVerdict.CHALLENGED:
                self._logger.info(
                    "handing challenge (HTTP %s) for %s %s to the store",
                    response.status_code,
                    method,
                    url,
                )
            if verdict is ResponseVerdict.CHALLENGED and not self._on_challenge(
                response
            ):
                self._raise_error(response, verdict, method=method, url=url)

            retryable = verdict in {
                ResponseVerdict.RETRY,
                ResponseVerdict.RATE_LIMITED,
                ResponseVerdict.CHALLENGED,
            }
            if not retryable or attempt + 1 >= policy.max_attempts:
                self._raise_error(response, verdict, method=method, url=url)
            retry_after = (
                self._retry_after(response)
                if verdict is ResponseVerdict.RATE_LIMITED
                else None
            )
            response.close()
            delay = (
                min(retry_after, self._retry_policy.max_delay)
                if retry_after is not None
                else self._backoff(attempt)
            )
            self._logger.info(
                "retry %s of %s for %s %s: %s, sleeping %.2fs",
                attempt + 2,
                policy.max_attempts,
                method,
                url,
                verdict.name,
                delay,
            )
            time.sleep(delay)
            attempt += 1

    def _raise_error(
        self,
        response: httpx.Response,
        verdict: ResponseVerdict,
        *,
        method: str,
        url: str,
    ) -> None:
        status = response.status_code
        retry_after = self._retry_after(response)
        response.close()
        attempts = self._retry_policy.max_attempts
        if verdict is ResponseVerdict.NOT_FOUND:
            raise NotFoundError(f"{method} {url} returned HTTP {status}")
        if verdict is ResponseVerdict.RATE_LIMITED:
            raise RateLimitError(
                f"{method} {url} was rate limited after {attempts} attempts",
                retry_after=retry_after,
            )
        if verdict is ResponseVerdict.CHALLENGED:
            raise ChallengedError(
                f"{method} {url} answered with a bot challenge (HTTP {status})",
                status_code=status,
            )
        if verdict is ResponseVerdict.BLOCKED:
            raise BlockedError(
                f"{method} {url} was blocked (HTTP {status})",
                status_code=status,
                retry_after=retry_after,
            )
        if verdict is ResponseVerdict.AUTH_EXPIRED:
            raise AuthenticationError(
                f"{method} {url} was rejected (HTTP {status})", status_code=status
            )
        raise TransportError(
            f"{method} {url} returned HTTP {status}", status_code=status
        )

    def _backoff(self, attempt: int) -> float:
        delay = self._retry_policy.backoff_factor * (2.0**attempt)
        delay = min(delay, self._retry_policy.max_delay)
        if delay == 0 or self._retry_policy.jitter_ratio == 0:
            return delay
        jitter = _JITTER.uniform(0, delay * self._retry_policy.jitter_ratio)
        return min(delay + jitter, self._retry_policy.max_delay)

    def _wait_for_request_slot(self) -> None:
        if self._min_request_interval == 0:
            return
        with self._request_lock:
            started_at = time.monotonic()
            if self._last_request_started_at is not None:
                elapsed = started_at - self._last_request_started_at
                delay = self._min_request_interval - elapsed
                if delay > 0:
                    self._logger.info("pacing: waiting %.2fs", delay)
                    time.sleep(delay)
                    started_at = time.monotonic()
            self._last_request_started_at = started_at

    def _retry_after(self, response: httpx.Response) -> float | None:
        """return the delay the server asked for in seconds, not capped."""

        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_date = parsedate_to_datetime(value)
                if retry_date.tzinfo is None:
                    retry_date = retry_date.replace(tzinfo=UTC)
                delay = (retry_date - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        if not isfinite(delay):
            return None
        return max(delay, 0.0)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigurationError(f"{self.store_name} client is closed")

    def _ensure_primed(self) -> None:
        if self._priming:
            return
        ttl = self._prime_ttl()
        now = time.monotonic()
        if self._primed_at is not None and (ttl is None or now - self._primed_at < ttl):
            return
        self._priming = True
        if type(self)._prime is not BaseClient._prime:
            self._logger.info("running session warm-up")
        try:
            self._prime()
            self._primed_at = time.monotonic()
        finally:
            self._priming = False

    def _invalidate_priming(self) -> None:
        self._primed_at = None

    def _require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise UnsupportedOperationError(capability, self.store_name)

    def _resolve_page_size(self, page_size: int | None) -> int:
        if page_size is None:
            return self.default_page_size
        if isinstance(page_size, bool) or not isinstance(page_size, int):
            raise ConfigurationError("page_size must be a positive integer")
        upper = self.max_page_size
        if page_size < 1 or (upper is not None and page_size > upper):
            limit = f" and {upper}" if upper is not None else ""
            raise ConfigurationError(f"page_size must be between 1{limit}")
        return page_size
