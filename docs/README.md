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
(`*-REVIEW.md`), release playbooks and migration notes are maintained in a
**private repository** (`myrtui-internal`) and are not published. Code comments
that need to point at them use `myrtui-internal/docs/…` and always degrade
gracefully if a reader cannot open the link.

`tests/test_docs_policy.py` enforces the allowed list above: adding any other
file here fails CI with an explanation. The sdist whitelist in `pyproject.toml`
independently limits what gets uploaded to PyPI.

## Writing style

- English for new user-facing documentation; the Chinese manual is kept in sync
  when behaviour changes.
- Prefer concrete commands and measured numbers over marketing prose.
- Keep the README short (it is a landing page); details belong in `USAGE.md`.
