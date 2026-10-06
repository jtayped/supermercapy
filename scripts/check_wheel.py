"""check an installed wheel from outside the repository.

usage: ``python -I scripts/check_wheel.py``, run with the interpreter of a fresh
virtual environment that has only the built wheel installed. it imports the
package, checks the version against the installed metadata, builds and closes
every client without letting one perform a request, and lists the stores
through the command line.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import version
from io import StringIO

import httpx

import supermercapy
from supermercapy.cli import main as cli_main

# every client that needs a positional binding, so the loop below can construct
# all of them the same way
POSITIONAL: dict[str, tuple[str, ...]] = {"mercadona": ("mad3",)}


def handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"building a client requested {request.url}")


def check(condition: bool, message: str) -> None:
    """stop with ``message`` when ``condition`` does not hold."""

    if not condition:
        raise SystemExit(f"wheel check failed: {message}")


def main() -> int:
    """construct every client against a transport that refuses all requests."""

    installed = version("supermercapy")
    check(supermercapy.__version__ == installed, f"version {supermercapy.__version__}")
    check(bool(supermercapy.ALL_CLIENTS), "the package registers no clients")
    for client_type in supermercapy.ALL_CLIENTS:
        name = client_type.store_name
        positional = POSITIONAL.get(name, ())
        client = client_type(*positional, transport=httpx.MockTransport(handler))
        check(client.store_name == name, f"{name} reports another store name")
        check(isinstance(client.capabilities, frozenset), f"{name} capabilities")
        check(
            client.default_language in client.supported_languages,
            f"{name} default language is unsupported",
        )
        check(not client.is_closed, f"{name} starts closed")
        client.close()
        check(client.is_closed, f"{name} did not close")
    listing = StringIO()
    with redirect_stdout(listing):
        status = cli_main(["stores"])
    check(status == 0, f"the cli exited {status} listing stores")
    for client_type in supermercapy.ALL_CLIENTS:
        check(client_type.store_name in listing.getvalue(), "the cli lost a store")
    print(
        f"wheel ok: {len(supermercapy.ALL_CLIENTS)} clients, {supermercapy.__version__}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
