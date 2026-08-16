# Contributing to KINETIC

Thank you for contributing to KINETIC. This document covers the practical
development workflow. It is intentionally short — see `README.md` for
architecture and `AGENTS.md` for the detailed phase-by-phase memory.

## Development setup

### Prerequisites

- **Python ≥ 3.11** (the project declares `requires-python = ">=3.11"`; CI
  tests 3.11 and 3.13).
- **[uv](https://docs.astral.sh/uv/)** for dependency + environment management
  (the repo ships a `uv.lock`).
- **Git**.
- **Docker** (optional) — only needed to run the Docker sandbox integration
  tests; they skip cleanly when the daemon is absent.
- **`ANTHROPIC_API_KEY`** (optional) — only needed for live model runs.
  Without it, tests still pass and the CLI/web console report a clear error
  instead of running a live agent.

### Install

```bash
git clone <repo-url> KINETIC
cd KINETIC
uv sync --extra dev        # installs runtime + dev deps (pytest, ruff, mypy, pytest-cov)
```

## Running the checks

KINETIC uses `uv run` so commands always execute in the project virtualenv.

### Tests

```bash
uv run pytest                       # full suite
uv run pytest -q                    # quiet
uv run pytest tests/test_phase7_security.py   # one file
uv run pytest -k environment        # by keyword
```

The suite is async-first (`asyncio_mode = "auto"`). Docker integration tests
and live-SDK tests skip when their prerequisites are absent — a green run
reports `N passed, ~17 skipped` in this environment.

### Coverage

```bash
uv run pytest --cov                 # measure + print report
uv run pytest --cov --cov-report=html   # browse htmlcov/index.html
```

Configuration lives in `[tool.coverage.*]` in `pyproject.toml`. The
conservative `fail_under = 75` guard catches major regressions; the baseline
is **82%**. **Do not write tests whose only purpose is inflating coverage** —
coverage should measure meaningful behavior. Never lower `fail_under` to hide
a drop; raise it only when real tests are added.

### Lint

```bash
uv run ruff check .
uv run ruff check . --fix       # auto-fix where safe
```

Ruff is configured in `[tool.ruff]` (`select = ["E", "F", "I", "UP", "B", "SIM"]`).
The tree must be ruff-clean before a commit.

### Type checking

```bash
uv run mypy agent cli config context dependencies environment events \
    intelligence memory observability project security store tasks tools \
    web errors.py lifecycle.py paths.py
```

Mypy is configured in `[tool.mypy]`. It is a **baseline / local guard**, not
yet a CI hard-fail: the baseline is 77 errors, concentrated in the Claude
Agent SDK lazy-import adapter (`agent/adapter.py`) and a few pydantic-dynamic
surfaces. When you touch a file, leave it no worse than you found it, and fix
straightforward type errors rather than adding `# type: ignore`. Do not
perform a giant type-system rewrite — see the P7.4 notes in `CHANGELOG.md`.

### Build the wheel

```bash
uv build              # produces dist/kinetic-0.1.0-*.whl + .tar.gz
```

### Clean install verification

```bash
uv build
uv venv /tmp/kinetic-test --python 3.13
uv pip install --python /tmp/kinetic-test dist/kinetic-0.1.0-*.whl
/tmp/kinetic-test/bin/kinetic --help
```

## Running the web console

```bash
uv run kinetic web --workspace . --host 127.0.0.1 --port 12000
# without an API key (inspect-only / dry-run):
uv run kinetic web --workspace . --allow-no-key
```

The web console binds to `127.0.0.1` by default (localhost only). Open
`http://127.0.0.1:12000`. See `README.md` → "Web Agent Test Console".

## CLI

```bash
uv run kinetic --help
uv run kinetic run "inspect this repo" --workspace .
uv run kinetic task status <id>
uv run kinetic task inspect <id>
```

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and
push to `main`: **Ruff → pytest+coverage → wheel build**, on Python 3.11 and
3.13. All steps must pass. There is no deployment or publishing automation.

**Before pushing a PR, run locally:**

```bash
uv run ruff check . && uv run pytest --cov && uv build
```

## PR expectations

- Keep changes **minimal and focused**. KINETIC is built in phases; do not
  start unscoped work (e.g. do not begin Phase 8 in a Phase-7 fix PR).
- Ruff-clean, tests green, wheel builds.
- Do not weaken security tests or bypass the permission/audit boundary.
- Do not add `# type: ignore` without a clear, commented reason.
- Do not commit `dist/`, `.venv/`, `.env`, `.kinetic/`, or build artifacts
  (all are gitignored).
- Write a clear commit message; one logical change per commit.
- Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.

## Security expectations

KINETIC enforces security at the **runtime/tool layer**, never via prompt
instructions. The single execution path is:

```
Agent → AgentSession → PermissionPolicy → ToolRegistry → Tool
      → Environment → Runtime
```

- **No** `os.system`, `shell=True`, `eval`, `pickle.load`, or unsafe `yaml.load`.
- **No** direct subprocess or filesystem mutation from the `web/` layer — the
  web layer delegates to `store/` and the existing backend.
- **Secrets** are masked at three layers: structured logging, the EventBus,
  and `web.serialize`. The API key is held in-memory only; it is never
  persisted and never appears in any HTTP response.
- **Permission policy** is least-privilege by default (git write, dependency
  install, environment network/admin, memory write/delete are all off by
  default).
- The web console binds to `127.0.0.1` by default. Exposing it on
  `0.0.0.0`/a public interface is **not** supported as a safe configuration
  (no rate limiting, no auth, no TLS). See `README.md` → "Production
  considerations".

If your change touches security boundaries, call it out explicitly in the PR
description.

## Repository layout

Application source lives as **top-level packages** at the repo root
(`agent/`, `cli/`, `config/`, …, `web/`, plus `errors.py`, `lifecycle.py`,
`paths.py`). The former `kinetic/` namespace was removed in Phase 7.2 —
imports are `from agent import ...`, **not** `from kinetic.agent import ...`.

## Need help?

- `README.md` — overview, architecture, layout, security boundaries.
- `AGENTS.md` — detailed phase-by-phase memory (verified facts + gotchas).
- `CHANGELOG.md` — what changed when.
