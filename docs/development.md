# Development

ModFig requires Python 3.11 or later.

## Install

```sh
python3 -m pip install -e ".[dev]"
```

The package exposes the `modfig` command and can also be run as a module:

```sh
python3 -m modfig --help
```

## Checks

```sh
pytest -q
mypy --platform darwin src/modfig
ruff format --check src tests
ruff check src tests
```

The packaging tests build a wheel and source distribution, verify packaged
specification assets, install a wheel into a temporary target, and load
resources without the source tree.

## Repository conventions

- `spec/` is canonical for the registry specification, schema, capability
  matrix, and conformance fixtures.
- `src/modfig/` contains the implementation.
- `docs/` contains maintained user and architecture documentation.
- `docs/superpowers/plans/` contains historical implementation plans and is not
  current product documentation.

Do not put secrets, resolved API keys, response bodies, or personal absolute
paths in source, tests, documentation, manifests, or diagnostics.
