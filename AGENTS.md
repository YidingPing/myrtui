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
  meant for humans.
- **The TUI is localised** (`en` by default, `zh_CN` for the owner): every
  visible string goes through `myrtui.i18n.t("key")`, with the text in
  `myrtui/locales/{en,zh_CN}.py`. Do not hardcode a UI string in module code,
  do not call `t()` at module level (it freezes the language at import), and
  keep both catalogs' keys and `{}` placeholders in sync —
  `tests/test_i18n.py` enforces all three.
- **Commit subjects are English.**

## Repositories and remotes

Three places, three jobs — **do not mix them up**:

| Remote | Role | What may be pushed |
|---|---|---|
| `gitea` (LAN) | **source of truth**, dev CI | `main` and every feature branch, freely |
| `origin` (GitHub) | public mirror | **`main` only**, plus release tags — never a branch |
| `docs-internal/` (nested repo) | internal design notes | pushed on its own schedule |

Two rules that exist because breaking them is unrecoverable:

- **`git push origin main` is safe only because `docs-internal/` is gitignored.**
  Never add it to this repo's index (`git add docs-internal` silently creates a
  gitlink); `tests/test_docs_policy.py` blocks that. Never push a feature branch
  to `origin` — GitHub only ever sees `main` and release tags.
- **The Gitea remote needs the *system* git.** `PATH` points at MacPorts git,
  which cannot reach the LAN (`connect() = EHOSTUNREACH` in ~1 ms, *not* a
  server failure):

  ```sh
  GIT_TERMINAL_PROMPT=0 /usr/bin/git -c credential.helper=store push gitea main
  ```

  Same for `fetch`/`ls-remote`. Anything else (`pytest`, GitHub pushes) uses the
  normal `PATH`.

## Branching

- Work happens on `dev` (or a short-lived `feat/…` branch off it). `main` only
  receives merges that are green and belong to the next release — it is the
  branch GitHub mirrors, so keep it releasable at all times.
- `git push` with no arguments pushes the current branch to its upstream, which
  is **`gitea`** for both `main` and `dev`. Publishing to GitHub is always the
  explicit `git push origin main` from the release checklist.
- Merge with `--ff-only` while the branch is a straight line; otherwise a merge
  commit is fine. Never rebase a branch after pushing it — rewriting a shared
  branch buys nothing and breaks the mirror comparison.

## Commands

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python scripts/affected_tests.py --run   # only what your change touches (~10 s)
python -m pytest -n 4                    # full suite, ~110 s
python -m pytest tests/test_v36.py -q    # one file
python -m pytest -q -k scroll            # one behaviour
python -m pytest --lf -q                 # only the failures from last run
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
- **Internal documents never enter this repository.** `docs/` is user-facing
  only; design plans, acceptance records and reviews live in `docs-internal/`
  (nested repo, ignored here, never published). If it is missing from your
  checkout, say so instead of inventing the context.
- **`docs-internal/` and this repo are separate git repositories.** Code and its
  rationale therefore land in *two* commits. Before reporting a task done, run
  `git -C docs-internal status --short` — a design note left uncommitted there
  is invisible in this repo's status.
- **Do not name internal documents in commit subjects or user-facing text**
  (README, `docs/`, CLI output). Say "internal notes (V37)" if a pointer is
  needed. Code comments may keep `docs-internal/docs/…` paths: they are the
  maintainer's memory aid and are not published.
- **Do not `git push --tags`.** Only the release tag for the version being
  published may be pushed, and only to `origin`.
- **Do not rewrite history**, force-push `main`, or touch `python -m build`
  output (`dist/`).
- **Keep the sdist whitelist honest**: `pyproject.toml` lists exactly what may
  be published to PyPI. A new top-level file needed by users must be added
  there; anything internal must not be.

## Testing expectations

**Do not run the full suite after every edit.** `scripts/affected_tests.py`
reads `git diff`, maps changed files to the tests that import them (direct, plus
one level of importers, never through the app-shell hubs) and prints or runs the
right command:

| You changed | Run | Local |
|---|---|---|
| only `*.md` (README / docs / AGENTS) | `pytest -q tests/test_docs_policy.py` | ~2 s |
| `myrtui/locales/*` | `pytest -q tests/test_i18n.py tests/test_docs_policy.py` | ~3 s |
| `myrtui/i18n.py`, `cli.py`, `tmux_backend.py`, `layout_state.py`, `conftest.py` | full suite (shared surface) | ~110 s |
| one feature module (`mdpreview`, `instances`, `mathimg`, …) | `scripts/affected_tests.py --run` | 10–20 s |
| `client/app.py` / `pane_widget.py` | full suite (everything goes through them) | ~110 s |
| before pushing `dev`, merging `main`, or tagging | full suite **once** | ~110 s |

CI still runs everything, so a missed file costs one CI round — not a broken
`main`. A failure that passes three times in isolation is environment jitter:
fix the test's condition, do not change product code.

- Every behaviour change needs a regression test that fails before the fix.
- Tests assert on **ids/state**, not on translated labels, wherever practical.
  `tests/conftest.py` pins `MYRTUI_LANG=zh_CN` so the existing Chinese
  assertions keep working; new assertions should compare against `t("key")`
  (or a stable `id`), and language-detection tests take the `no_lang_pin`
  marker.
- Textual timing is not deterministic: wait for a condition
  (`await pilot.pause()` in a loop / helper), never a single fixed `sleep`.
  `tests/test_v36.py::_scroll_preview` is the reference pattern.
- Real-tool smoke tests (Sixel, Chrome, `termtex`) stay manual: CI skips them
  unless `MYRTUI_REAL_TOOLS=1`, and unit tests must pass without them.

## Where things go

| You are writing… | Put it in… |
|---|---|
| user docs (install, usage, architecture) | `docs/` in this repo (allowed list enforced by `tests/test_docs_policy.py`) |
| design notes / plans / acceptance records for a **scheduled** release | `docs-internal/docs/<minor>/` (nested repo) — **not** `docs/`. One directory per minor version, one file per patch release: `0.6/0.6.1-PLAN.md`, or `0.6/0.6.0-<topic>-PLAN.md` for a single feature. Older `V37-…` / `P0-…` documents keep their names: code comments reference them by path |
| ideas, candidates, anything **not yet scheduled** | `docs-internal/docs/BACKLOG.md` (nested repo) — one pool across all versions. Never park a candidate inside a version plan: a plan is a commitment with criteria, and mixing the two turns it into a ledger. When an item is scheduled, copy it into the version's `PLAN.md`; when it ships, leave one line in the backlog's "done" table instead of deleting it |
| release notes | GitHub Releases (generated at tag time) |
| a module's rationale | the module's header docstring (Chinese) |

## CI

- **Development CI runs on Gitea** (`.gitea/workflows/ci.yml`, self-hosted
  `ubuntu-latest` + `macos-latest` runners, `builtin:checkout` so nothing is
  fetched from github.com). It has no Python version matrix — each runner has a
  single interpreter.
- **GitHub runs only `.github/workflows/release.yml`**, gated on tags. Its jobs
  carry an `if: github.server_url == 'https://github.com'` guard so the same
  file is a no-op if Gitea ever reads it.

## Release (maintainer only)

Bump `pyproject.toml` **and** `myrtui/__init__.py`, commit on `main`, push to
`gitea`, wait for green CI there, then push `main` to `origin` and push the tag
**only to GitHub**:

```sh
GIT_TERMINAL_PROMPT=0 /usr/bin/git -c credential.helper=store push gitea main
git push origin main
git tag v<version> && git push origin v<version>
```

The tag runs `release.yml`: macOS verification → build → PyPI (Trusted
Publishing) → GitHub Release. Never re-upload an existing version.

## Code style

- Python ≥ 3.10, `from __future__ import annotations`, type hints on public
  functions.
- No `print` in library code; use the module logger.
- Keep UI-thread work O(1): decoding, git, image and tmux work belong in workers.
