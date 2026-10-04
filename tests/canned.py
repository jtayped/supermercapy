"""one canned transport that answers for every store at once.

``search_all()`` and the command line share one transport between all the
clients they build, so this routes each request by host to the canned
storefront of the store that owns the host, and records it.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping

import httpx

from supermercapy import ALL_CLIENTS
from tests.harness import HARNESSES, Handler

# plusfresc delivers around barcelona, lleida, and tarragona only
PLUSFRESC_PROVINCES = ("08", "25", "43")


def _routes() -> dict[str, list[tuple[str, str]]]:
    """map every host a store's constants name to each url path and its store.

    a host two stores share, such as the empathy search index carrefour and
    condis both call, is told apart by the path each store's constant names.
    """

    routes: dict[str, list[tuple[str, str]]] = {}
    for client_type in ALL_CLIENTS:
        package = client_type.__module__.rsplit(".", 1)[0]
        constants = importlib.import_module(f"{package}._constants")
        for name, value in vars(constants).items():
            if name.endswith("_URL") and isinstance(value, str):
                url = httpx.URL(value)
                route = (url.path, client_type.store_name)
                routes.setdefault(url.host, []).append(route)
    return routes


ROUTES = _routes()


def store_of(url: httpx.URL) -> str:
    """return the store that owns ``url``: by host, or by path on a shared host."""

    routes = ROUTES[url.host]
    if len({store for _, store in routes}) == 1:
        return routes[0][1]
    matching = [route for route in routes if url.path.startswith(route[0])]
    return max(matching, key=lambda route: len(route[0]))[1]


def _plusfresc(request: httpx.Request) -> httpx.Response:
    """add the postcode lookup the shared plusfresc harness does not route."""

    parts = request.url.path.split("/")
    if parts[1:3] == ["api", "utils"] and parts[-1] == "centre":
        if parts[3].startswith(PLUSFRESC_PROVINCES):
            return httpx.Response(200, request=request, json="12")
        # the live answer for a postcode outside the delivery area
        return httpx.Response(409, request=request, json="0")
    return HARNESSES["plusfresc"].handler(request)


DEFAULT_HANDLERS: dict[str, Handler] = {
    **{store: harness.handler for store, harness in HARNESSES.items()},
    "plusfresc": _plusfresc,
}


def failing(status: int = 400) -> Handler:
    """return a handler that answers every request with ``status``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, json={})

    return handler


class Storefronts:
    """every canned storefront behind one transport, with per-store overrides."""

    def __init__(self, overrides: Mapping[str, Handler] | None = None) -> None:
        self.handlers = {**DEFAULT_HANDLERS, **(overrides or {})}
        self.requests: list[tuple[str, httpx.Request]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        store = store_of(request.url)
        self.requests.append((store, request))
        return self.handlers[store](request)

    def count(self, store: str) -> int:
        """return how many requests ``store`` received."""

        return sum(1 for name, _ in self.requests if name == store)
