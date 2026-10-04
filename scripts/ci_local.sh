#!/usr/bin/env bash
# run everything .github/workflows/ci.yml runs, locally, before anything is
# pushed. hosted ci then runs once on the stacked pull request instead of once
# per change.
#
# usage: scripts/ci_local.sh [--quick]
#   --quick  lint, types, docs, and tests on the newest python only
#
# needs uv. virtual environments are cached under
# ${XDG_CACHE_HOME:-~/.cache}/supermercapy-ci and refreshed on every run.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CACHE=${XDG_CACHE_HOME:-${HOME}/.cache}/supermercapy-ci
PYTHONS=(3.11 3.12 3.13 3.14)
NEWEST=3.14
QUICK=false

for argument in "$@"; do
  case "${argument}" in
    --quick) QUICK=true ;;
    *) echo "unknown option: ${argument}" >&2; exit 2 ;;
  esac
done

cd "${ROOT}"
mkdir -p "${CACHE}"

step() { printf '\n== %s\n' "$*"; }

venv() {
  # venv <name> <python> <extras> [extra requirements...]
  local name=$1 python=$2 extras=$3
  shift 3
  local path="${CACHE}/${name}"
  uv venv --quiet --allow-existing --python "${python}" "${path}"
  uv pip install --quiet --python "${path}/bin/python" -e ".[${extras}]" "$@"
  printf '%s' "${path}"
}

step "quality (python ${NEWEST})"
quality=$(venv quality "${NEWEST}" dev,docs)
"${quality}/bin/ruff" format --check .
"${quality}/bin/ruff" check .
"${quality}/bin/mypy"
"${quality}/bin/mkdocs" build --strict --quiet --site-dir "${CACHE}/site"
if command -v actionlint >/dev/null; then
  actionlint
else
  uvx --quiet --from actionlint-py actionlint
fi

if "${QUICK}"; then
  step "tests (python ${NEWEST})"
  "${quality}/bin/pytest" -q -p no:cacheprovider
  printf '\nquick checks passed; run without --quick before pushing\n'
  exit 0
fi

for python in "${PYTHONS[@]}"; do
  step "tests (python ${python})"
  env=$(venv "tests-${python}" "${python}" dev)
  "${env}/bin/pytest" -q -p no:cacheprovider
done

step "minimum dependencies (python 3.11, httpx 0.27.0)"
minimum=$(venv minimum 3.11 dev "httpx==0.27.0")
uv pip check --python "${minimum}/bin/python"
"${minimum}/bin/pytest" -q -p no:cacheprovider

step "package"
dist="${CACHE}/dist"
rm -rf "${dist}" "${CACHE}/wheel"
"${quality}/bin/python" -m build --outdir "${dist}" >/dev/null
"${quality}/bin/twine" check "${dist}"/*
uv venv --quiet --python "${NEWEST}" "${CACHE}/wheel"
uv pip install --quiet --python "${CACHE}/wheel/bin/python" "${dist}"/*.whl
uv pip check --python "${CACHE}/wheel/bin/python"
(cd "${CACHE}" && "${CACHE}/wheel/bin/python" -I "${ROOT}/scripts/check_wheel.py")

printf '\nall ci checks passed locally\n'
