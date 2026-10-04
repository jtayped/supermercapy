# release process

the repository publishes through pypi trusted publishing. the release workflow builds the distributions once, stores them as a github artifact, then publishes that artifact once a reviewer approves the protected `pypi` environment.

## repository setup

the pypi `supermercapy` project must define a trusted publisher with these values:

| setting | value |
| --- | --- |
| owner | `jtayped` |
| repository | `supermercapy` |
| workflow | `python-publish.yml` |
| environment | `pypi` |

github must have a protected `pypi` environment with a required reviewer. the repository and environment must not contain a `PYPI_API_TOKEN` secret.

the publish job alone receives `id-token: write`. every other workflow job receives `contents: read`. see the [pypa trusted publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/) for the identity exchange the publish job uses.

## release checklist

- update `src/supermercapy/_version.py`.
- move the pending changelog entries under a version and release date.
- run ruff formatting and linting, strict mypy, pytest with branch coverage, a package build, and `twine check`.
- install the wheel into a clean virtual environment outside the repository, import `supermercapy` there, and construct every client in `ALL_CLIENTS`.
- run the readme examples against that installed wheel.
- run `pytest tests/live --live --no-cov -rs`, or start the `live contract` workflow manually and read its per-store jobs. record the result, including any store skipped as inconclusive, in the changelog entry. `python scripts/live_smoke.py <store>` is the quicker three-call check.
- confirm the public api snapshot test passes, or that every difference it reported is a deliberate change with a changelog entry.
- confirm the capability matrix test passes, which is what proves the documentation still matches the clients.
- create a github release. its tag must equal `v` followed by the package version, such as `v0.1.0`.
- wait for the release workflow's quality and build jobs.
- review and approve the protected `pypi` deployment.
- confirm the release files, metadata, provenance, and attestations on pypi.

the release workflow rejects a tag that does not match the package version. it does not rebuild in the privileged publish job.

## a failing store is not a failing release

the live contract talks to services supermercapy does not control, and each store runs in its own job so one outage does not mask another. a store that fails there needs a look before release, but the release decision is yours. record what failed and why in the changelog instead of shipping around it without a word.
