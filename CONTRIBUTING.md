# Contributing

Thanks for taking a look. This is a small, opinionated project; the notes below
are what you need to run it, test it, and not fight its conventions.

## Language policy

- **Code comments and docstrings are in Chinese** by project convention. This
  is deliberate: the maintainer thinks and iterates in Chinese, and a partial
  English translation would rot into "stale English + live Chinese".
- **User-facing text is English**: this file, `README.md`, `docs/`, code that
  is printed to the terminal, and (eventually) the TUI itself.
- **New commit messages use English subjects.** History before the public
  release is Chinese and is not being rewritten.
- Issues and pull requests in **English or Chinese** are both welcome.

## Development setup

```sh
git clone https://github.com/YidingPing/myrtui
cd myrtui
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

`tmux >= 3.1` must be on `PATH`; the test suite starts real, isolated tmux
servers (each with its own socket name) and cleans them up.

## Tests

```sh
python -m pytest -n 4        # ~1 minute on 8 cores
python -m pytest tests/test_v22.py -q     # a single area
```

- The suite is parallel-safe: sockets carry a uuid, project roots come from
  `tmp_path`, and each instance gets its own data directory.
- Do **not** point tests at `$TMPDIR` as a project root; see the note in
  `tests/conftest.py` for why that is pathologically slow.
- Tests that need a real terminal feature (Sixel, `termtex`, Chrome) are
  skipped or simulated; real-tool smoke tests are manual only.
- `tests/README.md` documents the fragile spots (shell-readiness races, stdout
  capture inside Textual, etc.) — read the relevant section before debugging a
  flaky case.

## Style

- Python ≥ 3.10, `from __future__ import annotations`, type hints on public
  functions.
- No new runtime dependencies without a strong reason: the runtime is
  Python + tmux, plus optional external tools (`micro`, `ov`, `termtex`,
  `poppler`, Chrome) that must always degrade gracefully.
- Comments explain *why* (constraints, measured behaviour, past bugs), not
  *what*. Several files carry long header comments recording exactly that —
  keep the habit.

## Release (maintainers)

1. Bump the version in **two** places: `pyproject.toml` and
   `myrtui/__init__.py`.
2. Push `main`; CI must be green.
3. `git tag v<version> && git push origin v<version>` — `release.yml` then runs
   the macOS verification, builds wheel + sdist, publishes to PyPI via Trusted
   Publishing, and creates the GitHub Release.

Publishing requires `pypi` as the Trusted Publisher environment in the repo
settings; see the header of `.github/workflows/release.yml`.
