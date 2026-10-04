"""the in-memory cache transport, alone and under the store clients."""

from __future__ import annotations

import gzip
import logging
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

import supermercapy.cache
from supermercapy import (
    AuthenticationError,
    Bonarea,
    Bonpreu,
    CacheTransport,
    ConfigurationError,
    Mercadona,
    Plusfresc,
    search_all,
)
from tests.harness import HARNESSES, Handler

URL = "https://store.test/api/items"


class Recorder:
    """a canned server that remembers every request it answered."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def json_server(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={"path": request.url.path, "body": request.content.decode()},
        headers={"Set-Cookie": "session=fresh; path=/"},
    )


def cached(
    handler: Handler, **options: Any
) -> tuple[httpx.Client, Recorder, CacheTransport]:
    recorder = Recorder(handler)
    cache = CacheTransport(
        **{"ttl": 60.0, **options}, transport=httpx.MockTransport(recorder)
    )
    return httpx.Client(transport=cache), recorder, cache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(supermercapy.cache, "monotonic", clock)
    return clock


# ----------------------------------------------------------------- the transport


def test_a_repeated_get_is_answered_from_memory_until_the_ttl_ends(
    clock: Clock,
) -> None:
    client, recorder, _ = cached(json_server, ttl=30.0)
    with client:
        first = client.get(URL).json()
        clock.now += 29.9
        assert client.get(URL).json() == first
        assert len(recorder.requests) == 1
        clock.now += 0.1
        assert client.get(URL).json() == first
        assert len(recorder.requests) == 2


def test_only_successful_json_is_stored() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/html":
            return httpx.Response(200, request=request, html="<p>warm</p>")
        if path == "/image":
            return httpx.Response(200, request=request, content=b"jpeg")
        if path == "/missing":
            return httpx.Response(404, request=request, json={"error": "missing"})
        if path == "/moved":
            return httpx.Response(301, request=request, headers={"Location": "/"})
        if path == "/vendor":
            return httpx.Response(
                200,
                request=request,
                content=b"{}",
                headers={"Content-Type": "application/x.search+json;version=2"},
            )
        return httpx.Response(
            200,
            request=request,
            content=b"{}",
            headers={"Content-Type": "Application/JSON; charset=utf-8"},
        )

    client, recorder, _ = cached(server)
    with client:
        for path in ("/html", "/image", "/missing", "/moved", "/vendor", "/plain"):
            client.get(f"https://store.test{path}")
            client.get(f"https://store.test{path}")
    assert recorder.paths() == [
        "/html",
        "/html",
        "/image",
        "/image",
        "/missing",
        "/missing",
        "/moved",
        "/moved",
        "/vendor",
        "/plain",
    ]


def test_post_is_cached_only_when_asked_and_keyed_on_its_body() -> None:
    client, recorder, _ = cached(json_server)
    with client:
        client.post(URL, data={"q": "leche"})
        client.post(URL, data={"q": "leche"})
    assert len(recorder.requests) == 2

    client, recorder, _ = cached(json_server, methods=("get", " POST "))
    with client:
        leche = client.post(URL, data={"q": "leche"}).json()
        assert client.post(URL, data={"q": "leche"}).json() == leche
        pan = client.post(URL, data={"q": "pan"}).json()
        assert pan["body"] == "q=pan"
        client.get(URL)
        client.get(URL)
    assert len(recorder.requests) == 3


def test_request_headers_are_part_of_the_key_except_credentials() -> None:
    client, recorder, _ = cached(json_server)
    with client:
        client.get(URL, headers={"X-Zone": "1", "Authorization": "Bearer a"})
        client.get(URL, headers={"X-Zone": "1", "Authorization": "Bearer b"})
        client.get(URL, headers={"X-Zone": "1", "Cookie": "session=other"})
        assert len(recorder.requests) == 1
        client.get(URL, headers={"X-Zone": "2"})
        assert len(recorder.requests) == 2


def test_a_hit_never_replays_set_cookie() -> None:
    issued = iter(("first", "second", "third"))

    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={},
            headers={"Set-Cookie": f"session={next(issued)}; path=/"},
        )

    client, recorder, _ = cached(server)
    with client:
        client.get(f"{URL}/one")
        assert client.cookies["session"] == "first"
        client.get(f"{URL}/two")
        assert client.cookies["session"] == "second"
        # a hit for /one must not rewind the session to "first"
        response = client.get(f"{URL}/one")
        assert "set-cookie" not in response.headers
        assert client.cookies["session"] == "second"
    assert len(recorder.requests) == 2


def test_the_least_recently_used_answers_go_first() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json="abcd")  # six bytes

    client, recorder, _ = cached(server, max_bytes=13)
    with client:
        client.get(f"{URL}/a")
        client.get(f"{URL}/b")
        client.get(f"{URL}/a")  # a hit, which makes /b the oldest
        client.get(f"{URL}/c")  # nineteen bytes: /b goes
        client.get(f"{URL}/a")
        client.get(f"{URL}/c")
        client.get(f"{URL}/b")
    assert recorder.paths() == [
        "/api/items/a",
        "/api/items/b",
        "/api/items/c",
        "/api/items/b",
    ]


def test_an_answer_larger_than_the_cache_is_passed_on_and_not_kept() -> None:
    client, recorder, _ = cached(json_server, max_bytes=4)
    with client:
        assert client.get(URL).json()["path"] == "/api/items"
        client.get(URL)
    assert len(recorder.requests) == 2


def test_an_encoded_body_off_the_network_is_kept_encoded() -> None:
    body = gzip.compress(b'{"ok": true}')

    def server(request: httpx.Request) -> httpx.Response:
        # a stream, as the network hands it over, not yet read
        return httpx.Response(
            200,
            request=request,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Content-Length": str(len(body)),
            },
            stream=httpx.ByteStream(body),
        )

    client, recorder, _ = cached(server)
    with client:
        assert client.get(URL).json() == {"ok": True}
        hit = client.get(URL)
        assert hit.json() == {"ok": True}
        assert hit.headers["content-encoding"] == "gzip"
    assert len(recorder.requests) == 1


def test_a_body_already_decoded_in_memory_drops_its_encoding() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            content=gzip.compress(b'{"ok": true}'),
        )

    client, recorder, _ = cached(server)
    with client:
        assert client.get(URL).json() == {"ok": True}
        hit = client.get(URL)
        assert hit.json() == {"ok": True}
        assert "content-encoding" not in hit.headers
    assert len(recorder.requests) == 1


def test_a_body_that_fails_half_way_is_not_kept() -> None:
    closed: list[bool] = []

    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"half":'
            raise httpx.ReadError("connection reset")

        def close(self) -> None:
            closed.append(True)

    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "application/json"},
            stream=Broken(),
        )

    client, recorder, _ = cached(server)
    with client:
        for _ in range(2):
            with pytest.raises(httpx.ReadError):
                client.get(URL)
    assert len(recorder.requests) == 2
    assert closed


def test_a_streamed_request_body_is_passed_on() -> None:
    client, recorder, _ = cached(json_server, methods=("POST",))
    with client:
        for _ in range(2):
            response = client.post(URL, content=iter([b"q=", b"leche"]))
            assert response.json()["body"] == "q=leche"
    assert len(recorder.requests) == 2


def test_a_hit_keeps_the_http_version_and_reason() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={},
            extensions={"http_version": b"HTTP/1.1", "reason_phrase": b"Fine"},
        )

    client, _, _ = cached(server)
    with client:
        client.get(URL)
        hit = client.get(URL)
    assert (hit.http_version, hit.reason_phrase) == ("HTTP/1.1", "Fine")


def test_two_threads_that_miss_together_are_both_answered() -> None:
    together = threading.Barrier(2, timeout=5)

    def server(request: httpx.Request) -> httpx.Response:
        together.wait()
        return json_server(request)

    client, recorder, _ = cached(server)
    answers: list[object] = []
    with client:
        threads = [
            threading.Thread(target=lambda: answers.append(client.get(URL).json()))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        client.get(URL)
    assert len(answers) == 2
    assert len(recorder.requests) == 2


def test_clear_and_close_forget_everything() -> None:
    closed: list[bool] = []

    class Inner(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)

    recorder = Recorder(json_server)
    cache = CacheTransport(ttl=60, transport=Inner(recorder))
    client = httpx.Client(transport=cache)
    client.get(URL)
    cache.clear()
    client.get(URL)
    client.get(URL)
    assert len(recorder.requests) == 2
    client.close()
    assert closed == [True]
    CacheTransport(ttl=1).close()


def test_hits_and_stores_are_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _, _ = cached(json_server)
    with caplog.at_level(logging.DEBUG, logger="supermercapy.cache"), client:
        size = len(client.get(URL).content)
        client.get(URL)
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        f"cached {size} bytes for {URL}",
        f"cache hit for GET {URL}",
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"ttl": 0}, "ttl"),
        ({"ttl": -1.0}, "ttl"),
        ({"ttl": float("inf")}, "ttl"),
        ({"ttl": float("nan")}, "ttl"),
        ({"ttl": True}, "ttl"),
        ({"ttl": "60"}, "ttl"),
        ({"ttl": 60, "max_bytes": 0}, "max_bytes"),
        ({"ttl": 60, "max_bytes": True}, "max_bytes"),
        ({"ttl": 60, "max_bytes": 1.5}, "max_bytes"),
        ({"ttl": 60, "methods": "GET"}, "collection"),
        ({"ttl": 60, "methods": ()}, "at least one"),
        ({"ttl": 60, "methods": ("GET", " ")}, "at least one"),
        ({"ttl": 60, "methods": ("GET", 1)}, "at least one"),
    ],
)
def test_invalid_options_are_refused(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        CacheTransport(**options)


# ------------------------------------------------------------ under the clients


def test_mercadona_reads_and_searches_are_served_from_memory() -> None:
    recorder = Recorder(HARNESSES["mercadona"].handler)
    cache = CacheTransport(
        ttl=60, methods=("GET", "POST"), transport=httpx.MockTransport(recorder)
    )
    with Mercadona("mad3", transport=cache) as mercadona:
        assert mercadona.get_categories() == mercadona.get_categories()
        assert mercadona.get_product("1001") == mercadona.get_product("1001")
        first = mercadona.search_products("leche")
        assert mercadona.search_products("leche") == first
        mercadona.search_products("pan")
    assert [request.method for request in recorder.requests] == [
        "GET",
        "GET",
        "POST",
        "POST",
    ]


def test_plusfresc_keeps_one_answer_across_guest_tokens() -> None:
    harness = HARNESSES["plusfresc"]
    recorder = Recorder(harness.handler)
    cache = CacheTransport(ttl=60, transport=httpx.MockTransport(recorder))
    with Plusfresc(12, transport=cache, min_request_interval=0) as plusfresc:
        page = plusfresc.search_products("llet")
        assert plusfresc.search_products("llet") == page
    assert recorder.paths() == [
        "/api/loginGuest/12",
        "/api/search/languages/ca/llet/products/12",
    ]


def test_bonarea_form_posts_are_cached_when_post_is_named() -> None:
    harness = HARNESSES["bonarea"]
    recorder = Recorder(harness.handler)
    cache = CacheTransport(
        ttl=60, methods=("GET", "POST"), transport=httpx.MockTransport(recorder)
    )
    with Bonarea(transport=cache, min_request_interval=0) as bonarea:
        assert bonarea.get_product("13*5361") == bonarea.get_product("13*5361")
        assert bonarea.get_categories() == bonarea.get_categories()
        first = bonarea.search_products("leche")
        assert bonarea.search_products("leche") == first
    # the language warm-up is html, so it reached the storefront as well
    assert recorder.paths() == [
        "/es/shop/Article",
        "/es/shop/ShoppingBody",
        "/es",
        "/es/shop/search",
    ]


def test_bonpreu_mints_a_fresh_csrf_token_through_the_cache() -> None:
    harness = HARNESSES["bonpreu"]
    rejected: list[bool] = []

    def stale_once(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and not rejected:
            rejected.append(True)
            # the origin's answer to a stale csrf token
            return httpx.Response(403, request=request, headers={"requestid": "1"})
        return harness.handler(request)

    recorder = Recorder(stale_once)
    cache = CacheTransport(ttl=60, transport=httpx.MockTransport(recorder))
    with Bonpreu(transport=cache, min_request_interval=0) as bonpreu:
        try:
            similar = bonpreu.get_similar("29189")
        except AuthenticationError:
            pytest.fail("the csrf page was answered from memory")
        assert bonpreu.get_similar("29189") == similar
    # the csrf page is html and never stored, so the second mint reached the
    # origin; the similar ids are json and were; the batch put never is
    assert recorder.paths() == [
        "/api/webproductpagews/v5/products/similar",
        "/",
        "/api/webproductpagews/v6/products",
        "/",
        "/api/webproductpagews/v6/products",
        "/api/webproductpagews/v6/products",
    ]


def test_one_cache_lent_to_search_all_serves_every_store_across_calls() -> None:
    lidl, consum = HARNESSES["lidl"], HARNESSES["consum"]

    def storefronts(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("lidl.es"):
            return lidl.handler(request)
        return consum.handler(request)

    recorder = Recorder(storefronts)
    cache = CacheTransport(ttl=60, transport=httpx.MockTransport(recorder))
    first = search_all("leche", stores=["consum", "lidl"], transport=cache)
    again = search_all("leche", stores=["consum", "lidl"], transport=cache)
    cache.close()
    assert again == first
    assert sorted(request.url.host for request in recorder.requests) == [
        "tienda.consum.es",
        "www.lidl.es",
    ]
