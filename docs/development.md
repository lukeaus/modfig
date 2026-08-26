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

## Commit messages

Commits use [Conventional Commits](https://www.conventionalcommits.org/):

```text
feat(registry): add provider defaults
fix(factory): extend probe timeout
docs: update installation instructions
chore(deps): bump cryptography
```

`feat` creates a minor release. `fix` and `perf` create patch releases. Other
types do not create a release by themselves. Describe a breaking change with a
`BREAKING CHANGE:` footer:

```text
feat(registry): replace provider identity

BREAKING CHANGE: provider identifiers are now required to be stable
```

Install the local `commit-msg` hook once after installing the development
dependencies:

```sh
pre-commit install --hook-type commit-msg
```

The hook is a convenience; pull-request CI validates the complete commit range
again. To check a message or range manually:

```sh
cz check --message "fix: handle missing provider"
cz check --rev-range BASE_SHA..HEAD
```

## Releases

Python Semantic Release calculates the next version from commits merged to
`main`, updates `project.version` in `pyproject.toml`, creates the release
commit and `vX.Y.Z` tag, and creates the GitHub Release. Distribution builds are
not currently published by this workflow.

The repository starts at `0.1.0`. On the first push to `main` after the release
workflow is merged, no version tag exists, so Semantic Release calculates
`0.1.0`, creates the release commit, pushes the annotated `v0.1.0` tag, and
creates the GitHub Release. Because the package already contains `0.1.0`, the
first tag normally points at the baseline commit; later releases add a version
commit before tagging.

Do not create `v0.1.0` manually before that first workflow run. After the
baseline exists, merge normal Conventional Commits to `main` and let the
release workflow own subsequent tags. The project remains on the `0.x` line
while `major_on_zero = false`; change that policy deliberately when the public
API is ready for `1.0.0`.

GitHub setup must allow the release job to write repository contents. If branch
protection blocks the Actions bot from pushing its release commit, use a
dedicated GitHub App or token with the required contents permission instead of
disabling protection. Require the `commit-messages` CI job in pull requests.

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
