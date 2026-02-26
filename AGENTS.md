# AGENTS.md

Operational guide for coding agents working in this repository.

## 1) Repository Snapshot

- Language: Python 3.12+
- Package manager / runner: `uv`
- Main entrypoint: `main.py`
- Data models: `entities.py` (Pydantic v2 models and constrained types)
- Lock file: `uv.lock`
- CI workflow: `.github/workflows/sync.yml`
- Generated output location: `workspace/lipr/` (index and manifests)

## 2) What This Project Does

- Discovers GitHub repositories with `tooth.json` manifests via `gh search code`
- Fetches manifest files from raw GitHub URLs
- Validates/manages schemas with Pydantic models
- Migrates older manifests using the external `lip migrate` CLI when validation fails
- Collects repo metadata (stars, updated date) and version tags
- Writes package index JSON to `workspace/lipr/index.json`

## 3) Required Tooling

- Python `>=3.12` (from `pyproject.toml`)
- `uv` installed and available on PATH
- `gh` (GitHub CLI) installed and authenticated
- `git` installed
- `lip` CLI available (workflow installs it via shell installer)

## 4) Setup Commands

Run from repository root:

```bash
uv sync
```

If you need to execute without activating a venv, prefer:

```bash
uv run python main.py
```

## 5) Build / Lint / Test Commands

This repo currently has no dedicated build system, lint config, or test suite checked in.
Use the commands below as the operational baseline for agents.

### Build / Run

```bash
uv run python main.py
```

### Lint / Format (recommended, not currently configured in repo)

Use ad-hoc checks if needed during PR work:

```bash
uv run ruff check .
uv run ruff format --check .
```

If `ruff` is not available in the environment, either:

- install/add it in a separate change, or
- skip linting and explicitly report that lint tooling is not configured.

### Tests (current state)

- No `tests/` directory and no `pytest` config are present.
- If you add tests in a PR, standard command should be:

```bash
uv run pytest
```

### Run a Single Test (important)

When `pytest` tests exist, run exactly one test with node-id syntax:

```bash
uv run pytest tests/test_file.py::test_name
```

For a single test method in a class:

```bash
uv run pytest tests/test_file.py::TestClass::test_name
```

Use `-q` for less noise when appropriate.

## 6) CI Behavior

From `.github/workflows/sync.yml`:

- Triggers: push to `main`, nightly cron, manual dispatch
- Installs dependencies with `uv sync`
- Runs index sync via `uv run python main.py`
- Commits/pushes changes under `workspace/lipr` to `pages` branch when diff exists

Agent implication: changes that affect `main.py`/`entities.py` should preserve non-interactive CI execution.

## 7) Code Style Guidelines (Repository-Specific)

Follow existing conventions in `main.py` and `entities.py`.

### Imports

- Group imports in this order:
  1. stdlib
  2. third-party
  3. local modules
- Prefer explicit imports over wildcard imports.
- Keep import lists stable and minimally scoped.

### Formatting

- Use PEP 8-compatible formatting.
- Keep line length readable (Black/Ruff default style is acceptable).
- Prefer multi-line call formatting with trailing commas when arguments span lines.
- Preserve existing quote style and logging style within touched files.

### Types and Typing Discipline

- Use modern Python typing syntax (`list[str]`, `dict[str, X]`, `A | B`).
- Add type annotations for all new function signatures.
- Prefer concrete return types over `Any`; use `Any` only where unavoidable.
- Use `Final`, `NamedTuple`, and constrained types where semantically useful.

### Pydantic Models

- Use `BaseModel` for schema-bound structures.
- Encode schema constraints with `Annotated` + `StringConstraints` when needed.
- Keep wire-format fields explicit and aligned with schema expectations.
- Preserve `SemanticVersion` usage for version data.

### Naming Conventions

- `snake_case` for variables/functions.
- `PascalCase` for classes and Pydantic models.
- `UPPER_SNAKE_CASE` for module-level constants (e.g., `BASE_DIR`).
- Use descriptive names reflecting domain terms: manifest, variants, index, repo.

### Error Handling

- Prefer narrow exception handling where practical.
- Log contextual error messages that include repo/version identifiers.
- Re-raise when failures should bubble up; continue iteration only when intentionally tolerant.
- For external command failures (`subprocess`), surface stderr where available.

### Logging

- Use `logging` (not `print`) for operational output.
- Keep log messages concise and action-oriented.
- Include enough identifiers to debug batch processing failures.

### Subprocess and External Commands

- Use `subprocess.run(..., check=True)` for command reliability.
- Capture stdout/stderr when outputs are consumed or useful for diagnostics.
- Avoid shell=True unless absolutely required.

### File and Path Handling

- Use `pathlib.Path` for filesystem paths.
- Ensure parent dirs exist before writing files.
- Keep generated artifacts under `workspace/lipr/` unless requirements change.

## 8) Agent Workflow Expectations

- Before editing, inspect existing patterns in touched files and follow them.
- Make minimal, targeted changes; avoid unrelated refactors.
- Do not introduce new heavy dependencies without clear need.
- If adding tooling (ruff/pytest/mypy), include config and update this file.
- When tests do not exist, validate via focused dry-runs and static inspection.

## 9) Cursor / Copilot Rules Check

Searched locations:

- `.cursor/rules/`
- `.cursorrules`
- `.github/copilot-instructions.md`

Current status:

- No Cursor rules found.
- No Copilot instruction file found.

If any of these files are added later, agents must treat them as higher-priority repository instructions and update this `AGENTS.md` summary accordingly.

## 10) Safe-Change Notes for This Repo

- `main.py` currently clears `workspace/lipr/github.com` at startup; do not change this behavior casually.
- Network and CLI dependencies (`gh`, `git`, `lip`) are runtime-critical.
- Schema compatibility is important; migration fallback in `download_manifest` is intentional.

## 11) Quick Command Reference

```bash
# install deps
uv sync

# run sync job locally
uv run python main.py

# run full tests (if tests are added)
uv run pytest

# run one test (if tests are added)
uv run pytest tests/test_file.py::test_name

# optional lint/format checks
uv run ruff check .
uv run ruff format --check .
```
