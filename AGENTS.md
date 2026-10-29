# AGENTS.md

Instructions for AI coding agents (Codex, Claude Code, OpenCode, Cursor, …)
working on **myrtui**. Read this before changing code.

## What this project is

A Textual front end that drives tmux. Each pane is a **real tmux pane**; the UI
polls `capture-pane` and paints text. Do not add terminal emulation to this
repo — that is tmux's job. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the module map and the invariants (no fake panes, every external tool optional,
blocking work off the UI thread, click injected on release).

## Language policy

- **Code comments and docstrings are Chinese.** Do not translate them; new code
  follows the same convention. Explain *why* (measured behaviour, past bugs),
  not *what*.
- **User-facing text is English**: README, `docs/`, CLI output, log messages
  meant for humans, and (over time) the TUI itself.
- **Commit subjects are English.**

## Commands

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -n 4                    # full suite, ~100 s
python -m pytest tests/test_v36.py -q    # one area
python -m pytest -q -k scroll            # one behaviour
```

`tmux >= 3.1` must be on `PATH`; tests start isolated tmux servers (uuid socket
names) and clean up. The suite is parallel-safe.

**Read [tests/README.md](tests/README.md) before debugging a flaky test.** It
documents the known traps: shell-readiness races, stdout capture inside Textual,
`$TMPDIR` as a project root, event-loop timing.

## Hard rules

- **Do not add runtime dependencies** beyond `textual`, `platformdirs`,
  `mdit-py-plugins` (and the conditional `textual-image`/`pillow` extras).
  External tools (`termtex`, `ov`, `micro`, Chrome, `poppler`, clipboard
  commands) are always optional and must degrade with a readable message.
- **Do not put internal documents in this repo.** `docs/` is user-facing only;
  design plans, acceptance records and reviews live in a private repo that you
  probably cannot read. If you need that context, ask the maintainer.
- **Do not `git push --tags`.** Historical tags point at a pre-public history;
  only a release tag for the version being published may be pushed.
- **Do not rewrite history**, force-push `main`, or touch `python -m build`
  output (`dist/`).
- **Keep the sdist whitelist honest**: `pyproject.toml` lists exactly what may
  be published to PyPI. A new top-level file needed by users must be added
  there; anything internal must not be.

## Testing expectations

- Every behaviour change needs a regression test that fails before the fix.
- Tests assert on **ids/state**, not on translated labels, wherever practical.
- Textual timing is not deterministic: wait for a condition
  (`await pilot.pause()` in a loop / helper), never a single fixed `sleep`.
  `tests/test_v36.py::_scroll_preview` is the reference pattern.
- Real-tool smoke tests (Sixel, Chrome, `termtex`) stay manual; unit tests must
  pass without them.

## Where things go

| You are writing… | Put it in… |
|---|---|
| user docs (install, usage, architecture) | `docs/` in this repo (allowed list enforced by `tests/test_docs_policy.py`) |
| design notes / plans / acceptance records | the private repo — **not here** |
| release notes | GitHub Releases (generated at tag time) |
| a module's rationale | the module's header docstring (Chinese) |

## Release (maintainer only)

Bump `pyproject.toml` **and** `myrtui/__init__.py`, push `main`, wait for green
CI, then `git tag v<version> && git push origin v<version>`. The tag runs
`release.yml`: macOS verification → build → PyPI (Trusted Publishing) → GitHub
Release. Never re-upload an existing version.

## Code style

- Python ≥ 3.10, `from __future__ import annotations`, type hints on public
  functions.
- No `print` in library code; use the module logger.
- Keep UI-thread work O(1): decoding, git, image and tmux work belong in workers.
