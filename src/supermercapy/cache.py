"""an opt-in, in-memory response cache that plugs into a client's transport."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Any

import httpx

from ._core.exceptions import ConfigurationError

__all__ = ["CacheTransport"]

_LOGGER = logging.getLogger("supermercapy.cache")
# credentials and session cookies change under a client without changing what
# a request asks for, so they stay out of the key
_UNKEYED_HEADERS = frozenset({"authorization", "cookie"})
# carried over to a replayed response; the rest describe a live connection
_KEPT_EXTENSIONS = ("http_version", "reason_phrase")
_DEFAULT_MAX_BYTES = 32 * 1024 * 1024

_Key = tuple[str, str, bytes, tuple[tuple[str, str], ...]]


@dataclass(frozen=True, slots=True)
class _Entry:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    content: bytes
    extensions: dict[str, Any]
    expires_at: float

    def replay(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self.status_code,
            headers=self.headers,
            stream=httpx.ByteStream(self.content),
            request=request,
            extensions=dict(self.extensions),
        )


class CacheTransport(httpx.BaseTransport):
    """keep successful json answers in memory for ``ttl`` seconds.

    pass one as a client's ``transport``. a client closes its transport when
    it closes, so give each client its own, or lend one to
    :func:`~supermercapy.search_all`, which leaves it open. a request is
    answered from memory when the same method, url, body and request headers,
    ``cookie`` and ``authorization`` aside, were answered within ``ttl``
    seconds; anything else goes to ``transport``, an ``httpx.HTTPTransport``
    by default.

    only a 2xx response with a json content type is stored, so errors,
    redirects, images, and the html pages that warm sessions up or carry csrf
    tokens always reach the storefront. a stored copy drops ``set-cookie``,
    so a cached answer never rewinds the client's session. ``GET`` is the only
    method cached unless ``methods`` names more, which is how bonàrea's form
    posts and mercadona's search join in.

    the storefronts' own ``cache-control`` headers are ignored, since nearly
    all of them forbid caching, and ``ttl`` is the one freshness rule. stored
    bodies are bounded by ``max_bytes``, and the least recently used go first.
    """

    def __init__(
        self,
        *,
        ttl: float,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        methods: Iterable[str] = ("GET",),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not isfinite(ttl)
            or ttl <= 0
        ):
            raise ConfigurationError(
                "ttl must be a finite number of seconds above zero"
            )
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ConfigurationError("max_bytes must be a positive integer")
        if isinstance(methods, str):
            raise ConfigurationError("methods must be a collection such as ('GET',)")
        names = tuple(methods)
        if not names or any(
            not isinstance(name, str) or not name.strip() for name in names
        ):
            raise ConfigurationError("methods must name at least one http method")
        self._ttl = float(ttl)
        self._max_bytes = max_bytes
        self._methods = frozenset(name.strip().upper() for name in names)
        self._transport = transport or httpx.HTTPTransport()
        self._entries: OrderedDict[_Key, _Entry] = OrderedDict()
        self._size = 0
        self._lock = Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """answer ``request`` from memory, or send it and keep what may be kept."""

        key = self._key(request)
        if key is None:
            return self._transport.handle_request(request)
        entry = self._lookup(key)
        if entry is not None:
            _LOGGER.debug("cache hit for %s %s", request.method, request.url)
            return entry.replay(request)
        response = self._transport.handle_request(request)
        if not _storable(response):
            return response
        headers, content = _read(response)
        self._store(
            key,
            _Entry(
                status_code=response.status_code,
                headers=tuple(
                    (name, value)
                    for name, value in headers
                    if name.lower() != "set-cookie"
                ),
                content=content,
                extensions={
                    name: response.extensions[name]
                    for name in _KEPT_EXTENSIONS
                    if name in response.extensions
                },
                expires_at=monotonic() + self._ttl,
            ),
        )
        # the live answer keeps its cookies; only the stored copy drops them
        return httpx.Response(
            response.status_code,
            headers=headers,
            stream=httpx.ByteStream(content),
            request=request,
            extensions=response.extensions,
        )

    def clear(self) -> None:
        """forget every stored response."""

        with self._lock:
            self._entries.clear()
            self._size = 0

    def close(self) -> None:
        """forget every stored response and close the wrapped transport."""

        self.clear()
        self._transport.close()

    def _key(self, request: httpx.Request) -> _Key | None:
        if request.method not in self._methods:
            return None
        try:
            body = request.content
        except httpx.RequestNotRead:
            # a streamed body cannot be read without consuming it
            return None
        headers = tuple(
            sorted(
                (name.lower(), value)
                for name, value in request.headers.multi_items()
                if name.lower() not in _UNKEYED_HEADERS
            )
        )
        return (request.method, str(request.url), body, headers)

    def _lookup(self, key: _Key) -> _Entry | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= monotonic():
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return entry

    def _store(self, key: _Key, entry: _Entry) -> None:
        size = len(entry.content)
        if size > self._max_bytes:
            return
        with self._lock:
            if key in self._entries:
                self._remove(key)
            self._entries[key] = entry
            self._size += size
            while self._size > self._max_bytes:
                self._remove(next(iter(self._entries)))
        _LOGGER.debug("cached %s bytes for %s", size, key[1])

    def _remove(self, key: _Key) -> None:
        # the caller holds the lock
        entry = self._entries.pop(key)
        self._size -= len(entry.content)


def _storable(response: httpx.Response) -> bool:
    if not 200 <= response.status_code < 300:
        return False
    content_type: str = response.headers.get("content-type", "")
    media_type = content_type.split(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _read(response: httpx.Response) -> tuple[list[tuple[str, str]], bytes]:
    """return the headers and body to keep, leaving ``response`` closed."""

    headers = response.headers.multi_items()
    if not response.is_stream_consumed:
        # off the network: keep the body as it was sent, still encoded
        try:
            return headers, b"".join(response.iter_raw())
        except BaseException:
            response.close()
            raise
    # a response built in memory, as a mock transport returns, is already
    # decoded, so it must no longer claim an encoding
    return [
        (name, value)
        for name, value in headers
        if name.lower() not in {"content-encoding", "content-length"}
    ], response.content
