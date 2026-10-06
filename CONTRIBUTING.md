# contributing

supermercapy reads undocumented third-party services. normal tests must stay offline, and every supported response shape needs a committed fixture.

## development setup

use cpython 3.11 through 3.14:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev,docs]'
```

run the same checks as ci:

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
.venv/bin/mkdocs build --strict
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

run `.venv/bin/ruff format .` to apply python formatting.

## layout

`src/supermercapy/_core` holds the base client, the shared models, the coercion helpers, the html helpers, the unit normalisation, the serialisation, the capability flags, and the exceptions. one subpackage per store holds its client, its models, its constants, and nothing else. a store never edits `_core` to suit itself. it overrides a hook, declares a capability, or subclasses a model instead.

two stores on the same storefront platform share its parsers and request flow through a private module in `src/supermercapy/_platforms`. bonpreu and alcampo share `ocado.py`, and eroski and caprabo share `tapestry.py`. each store still keeps its own client class, models and `store_name`.

the modules at the top of the package work across stores and never talk to a storefront except through a store client: `_multi.py` and `search_all()` ask several stores at once, `match.py` and `_match_text.py` match products across stores, `aio.py` runs any client from asyncio, `cache.py` is the caching transport, and `cli.py` is the command line.

## tests

tests use `httpx.MockTransport`. do not add routine tests that contact a storefront. add small, scrubbed fixtures under `tests/fixtures/<store>/` for new response shapes. a test of a behavior change asserts the exact request paths, parameters, headers, and request counts.

the conformance suite in `tests/conformance` runs one contract against every client, so a new store joins it by registering a harness rather than by copying assertions. that contract includes the rule that a client declares a capability exactly when it overrides the method the capability unlocks.

the suite requires at least 90% branch coverage. tests must also confirm that reading a model performs no i/o, and that absent optional fields or unknown fields do not break an otherwise usable response.

`tests/test_public_api.py` pins every exported name, signature, dataclass field, and enum member in `tests/fixtures/public_api.json`. when it fails, read the diff. a removed or changed entry breaks callers. if the change is intended, rewrite the snapshot with `python -m tests.test_public_api`, commit it, and add a changelog entry.

## live contract tests

`tests/live/` holds one module per store that calls the real storefront. pytest skips those tests unless it gets `--live`:

```bash
.venv/bin/pytest tests/live --live --no-cov -rs
.venv/bin/pytest tests/live/test_lidl.py --live --no-cov -rs
```

they call every public method once and assert shape, not values. they take ids from live responses, and they compare each raw json response with the fixture it stands for, so a key the storefront renamed or dropped fails the run even though the model would have quietly read it as `None`. a bot block or challenge skips the test as inconclusive instead of failing it. a store that changes a response shape needs its fixture refreshed in the same pull request as the parser fix.

the `live contract` workflow runs the tier weekly and on demand, each store in its own job, and opens or updates one issue per failing store. a store that skipped some tests as inconclusive still passes, since a waf budget can run out partway through a module, but a store that refused every test gets its own issue, labelled `inconclusive`, so a store that blocks the runner completely does not stay green. several stores refuse datacenter ip ranges, so the workflow runs on the runner labels in the `LIVE_RUNNER_LABELS` repository variable, such as `["self-hosted", "live"]` for a self-hosted runner on a residential or fixed vps address. it falls back to `ubuntu-latest` when the variable is unset. a run there that skips most tests as inconclusive is a sign the address is refused, not that the stores are broken.

## adding a store

- read the storefront first and write down what it publishes.
- implement the four required methods, and only those optional methods the store can answer honestly. declare the matching capabilities.
- prefer a declared reduction over a method that returns something invented. a capability left out is documented, testable behavior. a fabricated field is not.
- extensions are plain extra methods. an extension must not shadow a standard method name with a different signature. standard methods may take extra typed keywords after the standard ones.
- add `docs/stores/<store>.md` with the binding, the capability table, the extensions, the quirks, and the non-goals, and add the store to the matrix in `docs/api.md` and to the capability table in `README.md`. tests compare both with the code.

## documentation

write headings and prose in lowercase, and put each paragraph and each list item on one line. preserve the exact spelling of code, identifiers, commands, paths, urls, and quoted output. use short examples that run as written, and state request counts for examples that make more than one request. every claim must be true of the code as it stands. when the two disagree, the code wins and the documentation is wrong.

add a changelog entry for every user-visible change.

## supported python policy

supermercapy supports cpython 3.11, 3.12, 3.13, and 3.14. ci runs the suite on all four. removing a supported version requires a new major release. a minor release may add a stable cpython version once its tests and the clean-wheel import check pass.

## pull requests

keep each pull request focused. include tests for behavior changes, and do not commit build output, virtual environments, credentials, or live api responses carrying data the fixtures do not need.
