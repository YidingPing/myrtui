# myrtui

**English** | [简体中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/myrtui)](https://pypi.org/project/myrtui/)
[![Python](https://img.shields.io/pypi/pyversions/myrtui)](https://pypi.org/project/myrtui/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

A **project-scoped terminal workspace**: a file tree and git log on the left,
up to four real shell panes on the right — one session per project, restored
where you left it.

> It is **not** a tmux replacement. myrtui drives tmux (via a per-project
> socket) and adds the things tmux deliberately leaves out.

<!-- Record it with: vhs scripts/demo.tape -> docs/demo.gif, then uncomment. -->
<!-- ![demo](docs/demo.gif) -->

## Why not just tmux

| | tmux / zellij | tmuxinator / sesh | myrtui |
|---|---|---|---|
| One session per project, named and isolated automatically | manual | needs a config file | `cd project && myrtui` |
| File tree + git history next to your shells | — | — | Yes |
| Layout ratios remembered per project | — | re-edit config | Yes |
| Open / preview Markdown, images, LaTeX | — | — | Yes |
| Detach / re-attach, shells survive | Yes | Yes | Yes |

## Features

- **One directory = one instance.** Session identity is derived from the
  realpath, so two projects never collide; running `myrtui` again attaches to
  the same session with your panes, layout and scrollback intact.
- **Up to 4 real tmux panes** with mouse support: click to focus, wheel to
  scroll, drag splitters to resize (ratios persisted per project).
- **File tree with git status colours** (VS Code conventions), lazy-loaded,
  context menu for open / preview / copy path.
- **Git history panel** — repo-wide or per file, hover a commit for details.
- **Markdown preview** with GFM, `$math$` via `termtex`, optional full-page
  LaTeX typesetting through KaTeX on a sepia page (`v`).
- **Image preview** with Sixel / Kitty graphics where the terminal supports it.
- **Selection and clipboard** that actually work inside a TUI: drag to select,
  release to copy (OSC 52 + native clipboard commands).

## Install

Requires `tmux >= 3.1` and Python `>= 3.10` (macOS and Linux; Windows is not
supported — `fcntl` and no native tmux).

```sh
brew install tmux              # Linux: sudo apt install tmux
pipx install myrtui
myrtui --version
```

Or use the installer that also checks dependencies and offers the optional
tools (`micro`, `ov`, `termtex`):

```sh
curl -fsSL https://raw.githubusercontent.com/YidingPing/myrtui/main/scripts/install.sh | sh
```

Upgrade with `pipx upgrade myrtui`. Annotations: Python ≥ 3.12 automatically
pulls the bitmap extras (`textual-image` + `Pillow`); on 3.10/3.11 previews fall
back to text, everything else works.

## Usage

```sh
cd some-project
myrtui                 # first run creates the session, later runs attach
```

- `myrtui ls` — list live instances (add `--all` for dead leftovers, `--json`);
- `myrtui attach [target]` — target = index, hash prefix, or path;
- `myrtui stop [target]` — cooperative shutdown (never kills panes);
- `myrtui --root /path/to/other/project` — work on a second project without
  `cd`; `--shell` and `--scrollback` set the pane shell / history limit.

## Keys (Ctrl-a prefix)

| Key | Action |
|---|---|
| `Ctrl-a d` | detach — quit the UI, keep every shell running |
| `Ctrl-a k` | kill the session (asks first) |
| `Ctrl-a t` / `w` | open / close a pane (up to 4) |
| `Ctrl-a 1..4` | focus pane N |
| `Ctrl-a r` | re-open a dead pane |
| `Ctrl-a b` | collapse / expand the sidebar |
| `Ctrl-a s` / `g` | focus the file tree / git history |
| `Ctrl-a c` / `v` | copy selection / paste |

Everything else goes straight to the inner shell. `Esc` returns from the
sidebar to the active pane; `Ctrl-a ?` (or hovering `?` in the top bar) shows
the full cheat sheet. Full tables: [docs/USAGE.md](docs/USAGE.md#key-bindings).

## Configuration

<details>
<summary>Environment variables</summary>

| Variable | Meaning |
|---|---|
| `MYRTUI_LANG` | UI language: `en` (default) / `zh_CN`; `--lang` overrides it for one run |
| `MYRTUI_EDITOR` | editor for "Open" (falls back to `$EDITOR` / `$VISUAL` / `micro` → `vim` → `vi`) |
| `MYRTUI_IDLE_INTERVAL` | idle capture interval in seconds (default `0.15`, range `0.05–5.0`); raise it for background instances |
| `MYRTUI_COPY_ON_SELECT` | auto-copy on drag release (default `1`; `0` keeps the selection for `Ctrl-a c`) |
| `MYRTUI_DRAG_SELECT` | `always` (default) = drag always selects; `app` = when the pane program requested mouse, drag is forwarded to it |
| `MYRTUI_GRAPHICS` | bitmap protocol: `auto` (default) / `sixel` / `tgp` / `unicode` / `off` |
| `MYRTUI_PAGE_THEME` | LaTeX page theme: `sepia` (default) / `paper` / `gray` / `white` / `dim` / `dark` |
| `MYRTUI_MATH_BACKEND` | math backend for Markdown preview: `auto` (default) / `plain` / `termtex` |
| `MYRTUI_CELL_PX` | override the terminal cell size (`10x20`) when it is not reported |
| `MYRTUI_CHROME` | Chrome/Chromium binary for the LaTeX page view |
| `MYRTUI_PDFTOCAIRO` | PDF → PNG converter (`pdftocairo` preferred, then `pdftoppm`) |
| `MYRTUI_LOOP_DEBUG` / `MYRTUI_MOUSE_DEBUG` | write timing / mouse event diagnostics to stderr or a temp log |

</details>

## Limitations

- **macOS / Linux only** — Windows has no native tmux and `cli.py` needs `fcntl`.
- **Clicks inside panes are delayed by 50–150 ms** — dragging always belongs to
  selection, so a click is injected on release (use `MYRTUI_DRAG_SELECT=app` to
  give drag back to the program).
- **No bitmap inside panes** — images render in the preview layer only.
- **Soft-wrapped lines copy with extra newlines** (capture works line by line).
- **Echo latency ~150 ms when idle** — tmux capture is polled; typing raises the
  rate to ~30 ms.

## Docs & contributing

- [docs/USAGE.md](docs/USAGE.md) — the full manual (keys, previews, splitters,
  instance management, all environment variables). Also available in
  [Chinese](docs/USAGE.zh-CN.md).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map and design decisions.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to run tests, plus the language policy.

Code comments and docstrings are written in Chinese by project convention;
user-facing text (this README, the UI, `docs/`) is available in English.
Issues and pull requests in either language are welcome.

## License

MIT — see [LICENSE](LICENSE).
