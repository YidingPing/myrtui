# Documentation

This directory holds **user-facing documentation only**. It is published both on
GitHub and inside the PyPI sdist, so anything here is public forever.

| File | Contents |
|---|---|
| `USAGE.md` | full English manual: keys, previews, splitters, instances, env vars |
| `USAGE.zh-CN.md` | same manual in Chinese |
| `ARCHITECTURE.md` | module map, data flow and the invariants to preserve |
| `demo.gif` | recording used by the README (added once `scripts/demo.tape` is run) |

## What does not belong here

Design plans (`*-PLAN.md`), acceptance records (`*-ACCEPTANCE.md`), reviews
(`*-REVIEW.md`), release playbooks and migration notes live in `docs-internal/`
at the repository root — a **nested git repository** (its own history, pushed
only to the maintainer's private Gitea) that `.gitignore` keeps out of this
one. Nothing under it is ever published, and a fresh clone of this repository
does not contain it.

Code comments that need to point at such a note use `docs-internal/docs/…` and
always degrade gracefully when a reader cannot open the link.

`tests/test_docs_policy.py` enforces the boundary: the allow-list above, no
internal document names anywhere in the tree, no private network addresses, no
embedded-repository git links, and `docs-internal/` staying untracked. The sdist
whitelist in `pyproject.toml` independently limits what gets uploaded to PyPI.

## Writing style

- English for new user-facing documentation; the Chinese manual is kept in sync
  when behaviour changes.
- Prefer concrete commands and measured numbers over marketing prose.
- Keep the README short (it is a landing page); details belong in `USAGE.md`.
