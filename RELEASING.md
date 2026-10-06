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

in the release workflow, the publish job alone receives `id-token: write`, and the other jobs receive `contents: read`. see the [pypa trusted publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/) for the identity exchange the publish job uses.

github pages must take its source from github actions (settings → pages → build and deployment). the `docs` workflow builds the site on every push to `main` that touches it, and deploys it only while the repository is public.

the `live contract` workflow runs on a self-hosted runner, as [contributing](CONTRIBUTING.md) explains. on a public repository, a pull request from a fork can edit any workflow to run on that runner, so actions must require approval for every outside contributor's workflows (settings → actions → general → approval for running fork pull request workflows from contributors → require approval for all external contributors). github offers that setting on public repositories only, so set it as soon as the repository goes public, or take the runner offline first.

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
