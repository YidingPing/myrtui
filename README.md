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
  scroll, left-drag splitters to resize (ratios persisted per project),
  right-click to paste (Shift+right-click is forwarded to the program).
- **File tree with git status colours** (VS Code conventions), lazy-loaded,
  context menu for open / preview / copy path.
- **Git history panel** — repo-wide or per file, hover a commit for details.
- **Markdown preview** with GFM, `$math$` via `termtex`, optional full-page
  LaTeX typesetting through KaTeX on a sepia page (`v`), and a one-key **A4 PDF
  export** (`p`) for printing or sending.
- **Image preview** with Sixel / Kitty graphics where the terminal supports it.
- **Selection and clipboard** that actually work inside a TUI: drag to select,
  release to copy (OSC 52 + native clipboard commands).

## Terminal support

Bitmaps — the image preview and the `v` page view — need a terminal that speaks
**Sixel** or the **Kitty graphics protocol**. Everything else (panes, tree, git
log, Markdown, math) is plain cells and works anywhere.

| Terminal | Bitmap protocol | Status |
|---|---|---|
| Tabby | Sixel | ✅ measured (macOS) |

Only measured rows are listed: a terminal missing from this table is
**untested**, not unsupported. Without a bitmap protocol the previews show
metadata and a hint instead of an image; nothing breaks and no pane is affected.
Inside an outer tmux, Sixel additionally needs
`set -ga terminal-features ',*:sixel'` — see
[Inside another tmux](docs/USAGE.md#inside-another-tmux-uu-remote-a-local-mirror).

### Going mobile

What a phone or tablet terminal costs you is the **bitmaps**, not the preview:

| Capability | Needs | On a terminal without bitmaps |
|---|---|---|
| Markdown body | nothing (Textual draws cells) | ✅ |
| Inline / block math | `termtex` (optional) | ✅ LaTeX source when it is missing |
| Image preview | Sixel / TGP | ❌ metadata and a hint |
| `v` page view | Chrome + `pdftocairo` + bitmaps | ❌ |

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
| `MYRTUI_IDLE_INTERVAL` | idle capture interval in seconds (default `0.15`, range `0.05–5.0`); polling speeds up automatically (~17 Hz) while pane content keeps changing; raise it for background instances |
| `MYRTUI_TRANSPORT` | tmux transport: `auto` (default: resident `tmux -C` client, per-call processes as fallback) / `exec` (one process per call) / `control` (force the resident client) |
| `MYRTUI_LOG` | where warnings go once the TUI owns the terminal (default: the platform log dir, e.g. `~/Library/Logs/myrtui/myrtui.log`); empty disables it |
| `MYRTUI_COPY_ON_SELECT` | auto-copy on drag release (default `1`; `0` keeps the selection for `Ctrl-a c`) |
| `MYRTUI_DRAG_SELECT` | `smart` (default) = the program gets the drag when it asked for mouse (its own selection UI works), Shift+drag selects locally; `always` = drag always selects; `app` = forward every drag to the program |
| `MYRTUI_CLIPBOARD` | copy channels: `auto` (default: OSC 52 + native clipboard command) / `osc52` / `system` |
| `MYRTUI_CLIPBOARD_PASSTHROUGH` | forward copies made by programs inside panes (opencode's select-to-copy, `vim "+y`) to your clipboard (default on; `0` disables) |
| `MYRTUI_OUTER_MUX_HINT` | show the startup hint when myrtui itself runs inside another tmux/screen (default on; `0` disables) |
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
- **Clicks inside panes are delayed by 50–150 ms** — when the drag belongs to
  myrtui (the program did not ask for mouse, or `MYRTUI_DRAG_SELECT=always`), a
  click is injected on release so a drag can always become a selection. Programs
  that requested mouse get the press immediately.
- **No bitmap inside panes** — images render in the preview layer only.
- **Soft-wrapped lines copy with extra newlines** (capture works line by line).
- **A pane program's copy only reaches you through myrtui** — the OSC 52 it emits is
  forwarded from the tmux control channel (default on). A program that writes the
  clipboard through its own native command bypasses this, which is fine locally and
  usually wrong over SSH.
- **An outer tmux can steal the drag** — if myrtui itself runs inside a multiplexer
  (UU Remote's terminal is one), that server's mouse bindings run first and a drag may
  never arrive. myrtui shows a startup hint when it detects one; the one-line fix is in
  USAGE under "Inside another tmux".
- **Echo latency ~150 ms when idle** — tmux capture is polled; typing raises the
  rate to ~30 ms.

## Docs & contributing

- [docs/USAGE.md](docs/USAGE.md) — the full manual (keys, previews, splitters,
  instance management, all environment variables). Also available in
  [Chinese](docs/USAGE.zh-CN.md).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module map and design decisions.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to run tests, the language policy,
  and how the project is built.

Code comments and docstrings are written in Chinese by project convention;
user-facing text (this README, the UI, `docs/`) is available in English.
Issues and pull requests in either language are welcome.

## License

MIT — see [LICENSE](LICENSE).
