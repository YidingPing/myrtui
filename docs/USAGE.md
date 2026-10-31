# Usage manual

[English](USAGE.md) | [简体中文](USAGE.zh-CN.md)

Complete reference for myrtui. The [README](../README.md) is the short version.

- [Install and upgrade](#install-and-upgrade)
- [Running](#running)
- [Instances (`ls` / `attach` / `stop`)](#instances-ls--attach--stop)
- [Key bindings](#key-bindings)
- [Panes, layouts and focus](#panes-layouts-and-focus)
- [Sidebar: file tree and git history](#sidebar-file-tree-and-git-history)
- [Open and preview](#open-and-preview)
- [Splitters](#splitters)
- [Selection and clipboard](#selection-and-clipboard)
- [Environment variables](#environment-variables)
- [Known limitations](#known-limitations)

## Install and upgrade

Runtime requirements: **Python ≥ 3.10** and **tmux ≥ 3.1** (3.1 introduced
`capture-pane -N`). macOS and Linux only.

```sh
pipx install myrtui            # or: uv tool install myrtui
pipx upgrade myrtui            # upgrade
```

The optional installer wraps the above and additionally checks tmux and offers
the optional tools:

```sh
curl -fsSL https://raw.githubusercontent.com/YidingPing/myrtui/main/scripts/install.sh | sh
```

| Variable | Effect |
|---|---|
| `MYRTUI_VERSION` | pin a version (`0.4.4` → `myrtui==0.4.4`) |
| `MYRTUI_PREFIX` | location of the venv fallback (default `~/.local/opt/myrtui`) |
| `MYRTUI_DEPS` | `ask` (default) / `yes` / `no` — whether to auto-install tmux and optional tools |

The installer detects an existing channel (uv tool / pipx / venv fallback) and
upgrades **in place**; it never overwrites an existing shim with the venv
fallback. Declining optional tools never blocks installing myrtui itself.

Bitmap previews need `textual-image` + `Pillow`, which are pulled automatically
on Python ≥ 3.12. On 3.10/3.11 (or terminals without Sixel/TGP) images fall back
to a placeholder and everything else still works. Manual injection for old
environments: `pipx inject myrtui textual-image pillow`.

## Running

```sh
cd some-project
myrtui
```

| Flag | Meaning |
|---|---|
| `--root PATH` | project root override (realpath → identity / lock / socket / sidebar) |
| `--shell PATH` | shell for new panes (default `$SHELL`, then `/bin/zsh`) |
| `--scrollback N` | tmux `history-limit` (default 5000, range 1..1000000) |
| `--lang {en,zh_CN}` | UI language for this run (overrides `MYRTUI_LANG` / config / locale) |

`--shell` and `--scrollback` only affect **panes created afterwards**: tmux
snapshots the options when a pane/window is created, and `Ctrl-a r` replays the
command recorded at creation time. myrtui prints a note when that applies.

## Instances (`ls` / `attach` / `stop`)

```sh
myrtui ls              # live instances (running / detached)
myrtui ls --all        # include dead leftovers (lock file only, no session)
myrtui ls --json       # index / state / root / short_hash / pid
myrtui attach [target] # target: index, hash prefix, or project path
myrtui stop [target]   # ask the front end to leave, then kill the session
```

- **Indices are per invocation**: `attach` / `stop` rescan with the same rule
  (paths sorted lexicographically; indices assigned among live instances only).
  A bare number is always read as an index.
- Attaching to a **running** instance triggers the takeover handshake: the
  other front end exits, the shells survive. This is not shared attachment.
  Attaching to a **detached** instance restores the previous layout ratios.
- `stop` writes a cooperative stop marker; if the front end does not respond
  within 3 s it reports an error and **does not force-kill**. After acquiring
  the lock it re-checks that the target has not been taken over meanwhile.
- Dead instances never consume an index and are hidden by default; lock files
  are intentionally never cleaned up. `attach`/`stop` on a dead target prints
  "no live instance" and exits 2.
- `attach` with no argument attaches directly when exactly one live instance
  exists; with zero or several it prints the list and exits 2.

## Key bindings

| Key | Action |
|---|---|
| `Ctrl-a d` | detach: quit the UI, keep the session and all shells alive |
| `Ctrl-a k` | kill the tmux server (asks first); all shells end |
| `Ctrl-a t` | open a pane (advances to the next layout, max 4) |
| `Ctrl-a 1..4` | focus pane N (out-of-range is ignored) |
| `Ctrl-a w` | close the focused pane; asks first when a program is running (no-op when only one is left) |
| `Ctrl-a r` | re-open a dead pane (focused pane first, else most recent death) |
| `Ctrl-a b` | collapse / expand the sidebar (panes grow, tmux resizes with it) |
| `Ctrl-a s` | focus the file tree |
| `Ctrl-a g` | focus the git history panel |
| `Ctrl-a c` | copy the current selection (drag-release already copies) |
| `Ctrl-a v` | paste: system clipboard first, internal buffer as fallback |
| `Ctrl-a ?` | toggle the shortcut overlay (same as hovering `?`) |
| `Esc` | sidebar → back to the active pane |
| `Ctrl-a` then 1 s timeout | send a literal `Ctrl-a` to the inner shell |
| anything else | passed straight to the inner shell |

`Ctrl-a c` / `v` / `g` take three letters away from the shell; `Ctrl-a o` is
free and passes `o` through like any undefined prefix key.

## Panes, layouts and focus

Layouts are column-first:

| Panes | Layout |
|---|---|
| 1 | full width |
| 2 | stacked vertically (pane 2 below pane 1) |
| 3 | left column 2 (stacked), right column 1 (full height) |
| 4 | 2×2 (panes 1/2 left, 3/4 right) |

Focus rules:

- Focus inside a pane: every key and paste goes to the inner shell.
- Focus in the sidebar: keys and paste stay in the tree/history, `Esc` returns.
- `Ctrl-a …` works from anywhere.
- The focused pane gets a heavy green border; the focused sidebar half gets a
  background tint (upper = tree, lower = history).
- When a pane's shell exits, the pane is dimmed, the tab shows `N:dead`, typing
  is no longer forwarded, and `Ctrl-a r` offers to restart it. Five consecutive
  capture failures show "lost connection to the session… reconnecting…" and
  "reconnected" when it recovers.

Mouse inside a pane: click to focus, wheel scrolls the scrollback (ignored on
the alternate screen). If the program requested mouse reporting (`vim` with
`mouse=a`, opencode's 1003), clicks, wheel, right/middle click and hover are
injected with the encoding the program asked for (SGR 1006, otherwise the legacy
3-byte form). `Shift`+wheel always scrolls the scrollback regardless.

## Sidebar: file tree and git history

**File tree** — starts at the project root with its first level. Clicking a
directory expands/collapses it (scanned in a worker thread, so huge directories
never freeze the UI). Clicking a file only selects it; all file actions live in
the context menu. `.git` is hidden; other hidden entries and dependency
directories are listed normally.

**Git colours** follow VS Code: modified = gold, added/untracked = green,
deleted = red, renamed = cyan, conflict = red; directories inherit the colour of
their children (added wins over modified). Status refreshes every 3 s in a
worker thread (`git status --porcelain -z -uall`), and that same poll keeps the
tree honest about the disk: files created or deleted outside the app appear or
disappear within one interval, without expanding or collapsing anything you
already opened. Paths git cannot see (ignored files, a directory outside any
repository) are only refreshed manually.

**Context menu** (right click, or `Enter` on a file row / `Space` on any row):

- file row: **Open (editor)** / **Preview Markdown** (`.md` only) /
  **Preview image** (image files; they have no "Open") / **Copy relative path** /
  **Copy absolute path**;
- directory row: **Refresh this directory** / **Copy relative path** /
  **Copy absolute path**.

Relative paths are from the project root (the root itself copies `.`). Right
clicking only highlights that row; it never expands a directory or switches the
git history. Opening or previewing *does* switch the history to that file.
Opening injects the command into the **current pane**; previewing opens an
in-app read-only screen and does not consume a pane. `Esc` closes the menu and
returns focus to the tree.

**Refresh this directory** re-scans the directory in place, keeping other
expanded subtrees and the cursor. If the directory was deleted externally, the
parent is refreshed instead; if the project root is gone, the whole tree reloads.

**Git history** — repo-wide recent commits (date + subject, newest first) by
default. The date grows from `MM-DD` to `YYYY-MM-DD` to `YYYY-MM-DD HH:MM` as the
sidebar widens. Hovering a commit for 1 s opens a details card (subject, body,
short sha, author, date; scroll inside the card when the body is long; the card's
text can be dragged and copied with `Ctrl-a c`). Selecting a file switches to
that file's history (`git log --follow`), and the header reads
`GIT <branch> · <relative path>`; selecting a directory returns to repo-level.
Non-git directories show "not a git repository", missing git shows
"git unavailable", and an initialised-but-empty repo shows "no commits on this
branch".

## Open and preview

Select a file row in the tree (right click, or `Enter`/`Space`) and choose an
action.

**Open** injects the editor command into the **current pane**. The editor is
resolved in this order (the menu shows the winner, e.g. "Open (micro)"):

1. `$MYRTUI_EDITOR` (may contain arguments, e.g. `code --wait`);
2. `$EDITOR`;
3. `$VISUAL`;
4. `micro` → `vim` → `vi`.

`helix` / `nvim` are never auto-detected; set one of the variables to use them,
e.g. `export MYRTUI_EDITOR=nvim`. If no editor exists, the action degrades to a
read-only pager (`ov`, else `less`). Preconditions: the pane must sit at a shell
prompt (not running a full-screen program, not on the alternate screen, not
dead); myrtui sends `C-u` first, which discards any half-typed command. Preview
is exempt from all of this.

**Preview Markdown** works for `.md` / `.markdown` / `.mdown` / `.mkd` and opens
a full-screen read-only screen:

| Key | Action |
|---|---|
| `Esc` / `q` | close, focus returns to the tree |
| `r` | re-read from disk and re-render (scroll position is kept when possible) |
| `v` | LaTeX typesetting view (KaTeX page bitmaps, see below) |
| arrows / `PgUp` / `PgDn` / `Home` / `End` / wheel | scroll |

Rendering: GFM headings, paragraphs, lists, tables, quotes, code blocks
(Textual's own highlighter, no Pygments). Inline math `$...$` / `\(...\)` and
block math `$$...$$` / `\[...\]` are rendered by `termtex` when installed
(fractions, matrices, `cases`); without it the LaTeX source is shown. Currency
like `$100 and $200` is not mistaken for math. Links do not navigate; the file
is not auto-refreshed — if it changes on disk while open, the footer says so and
`r` reloads it. Files above 512 KiB are refused with a
message; binary/missing/empty files show a readable reason.

**LaTeX page view** (`v`): the whole document becomes self-contained HTML with
KaTeX, Chrome prints it to PDF at exactly the terminal viewport size, and
`pdftocairo` rasterises each page 1:1. One page = one screen, seamless
scrolling. The default theme is sepia (`#E6DABE` page, `#3D3427` text, ~8.8:1);
`MYRTUI_PAGE_THEME` switches between `sepia` / `paper` / `gray` / `white` /
`dim` / `dark`. The trade-off: no text selection or search in this view, and
resizing re-typesets (1–2 s). Requires Chrome/Chromium + `pdftocairo` and a
Sixel/TGP terminal; missing pieces are reported instead of showing a blank page.
Rendered pages are cached under `<cache>/myrtui/pages/<sha1>/p-001.png…`.

**Preview image** works for `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`,
`.tif`, `.tiff`, `.ico`, `.avif`:

| Key | Action |
|---|---|
| `→` / `n` | next image in the same directory (wraps) |
| `←` / `p` | previous image (wraps) |
| `r` | rescan the directory and reload |
| `Esc` / `q` | close; the cursor stays on the last image viewed |

Images are fitted to the viewport, never upscaled or modified. The header shows
`cat.png · 3/12 · 1200×800 · PNG · 1.2 MiB`. Decoding, resizing and alpha
compositing happen in worker threads (transparent areas are composited against
the theme background — terminals do not blend alpha). Limits: ≤ 10 MB,
≤ 4000×4000, first frame only for animated GIFs, no SVG. Without Sixel/Kitty
support you still get the metadata and a hint. When running inside an outer
tmux, Sixel needs `set -ga terminal-features ',*:sixel'` (and usually
`set -g allow-passthrough on`).

## Splitters

All splitters are one cell thick; press within ±1 cell of the line to grab it
(the line brightens on hover). Dragging works with either mouse button and a
double click resets that dimension.

| Splitter | Position | Adjusts |
|---|---|---|
| sidebar ↔ panes | sidebar's right edge | sidebar width |
| tree ↔ git panel | inside the sidebar | tree height |
| left column ↔ right column | middle of the pane area | column widths |
| within a column | middle of each column | vertical split (shared by both columns, so 2×2 stays aligned) |

Ratios are written immediately to
`<user_data_dir>/myrtui/<hash>.layout.json` and restored on the next attach;
missing/corrupt files fall back to defaults. The format is versioned — version 1
files (row-first layout) are discarded rather than reinterpreted. Because the
sidebar/pane splitter sits behind the sidebar's padding, it can only be grabbed
from the pane side.

## Selection and clipboard

Dragging with the left button **always** selects text, regardless of whether the
pane program requested mouse reporting. Releasing copies automatically (OSC 52
plus a native clipboard command); the selection stays highlighted for 600 ms so
you can see what was copied.

- Copied text: trailing blank cells trimmed, CJK/wide characters aligned per
  cell, and only the pane where the press started contributes content.
- Channels: OSC 52 (works in iTerm2/WezTerm/Windows Terminal) plus
  `pbcopy` / `wl-copy` / `xclip` / `clip.exe`. macOS Terminal.app does not
  support OSC 52 and relies on the command. **SSH sessions send OSC 52 only** —
  the remote clipboard is not yours.
- `MYRTUI_COPY_ON_SELECT=0` disables auto-copy: the selection stays for 10 s,
  `Ctrl-a c` copies it, anything else cancels.
- Want the program's own drag behaviour (vim visual selection, `ov`)? Set
  `MYRTUI_DRAG_SELECT=app`. Clicks, wheel and right/middle click keep working.
- Pasting (`Ctrl-a v`) prefers the system clipboard and falls back to the
  internal buffer. In SSH sessions myrtui only tells you which terminal shortcut
  to use (macOS `Cmd+V`, Windows Terminal `Ctrl+V`, Tabby `Ctrl+Shift+V`),
  because the remote clipboard is the wrong one. Content is pasted verbatim,
  including trailing newlines.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MYRTUI_LANG` | `en` | UI language: `en` / `zh_CN`. Also read from `config.json`'s `lang` and from a `zh*` locale; `--lang` wins for one run |
| `MYRTUI_EDITOR` | — | editor for "Open" (see resolution order above) |
| `MYRTUI_IDLE_INTERVAL` | `0.15` | idle capture interval in seconds (clamped to `0.05–5.0`); raise it for secondary instances (`MYRTUI_IDLE_INTERVAL=0.5 myrtui`) |
| `MYRTUI_COPY_ON_SELECT` | `1` | auto-copy on drag release (`0/false/no/off` disables) |
| `MYRTUI_DRAG_SELECT` | `always` | `always` = drag always selects; `app` = forward drag when the program asked for mouse |
| `MYRTUI_GRAPHICS` | `auto` | `auto` / `sixel` / `tgp` / `unicode` / `off` (preview bitmaps only) |
| `MYRTUI_CELL_PX` | — | override cell size in pixels (`10x20`) when the terminal does not report it |
| `MYRTUI_PAGE_THEME` | `sepia` | `sepia` / `paper` / `gray` / `white` / `dim` / `dark` for the LaTeX page view |
| `MYRTUI_MATH_BACKEND` | `auto` | `auto` (use `termtex` when found) / `plain` / `termtex` |
| `MYRTUI_CHROME` | — | Chrome/Chromium binary for the LaTeX page view |
| `MYRTUI_PDFTOCAIRO` | — | PDF → PNG converter (`pdftocairo` preferred, then `pdftoppm`) |
| `MYRTUI_LOOP_DEBUG` | — | `=1` logs event-loop delays > 30 ms to stderr (profiling) |
| `MYRTUI_MOUSE_DEBUG` | — | `=1` logs every mouse event to `<tmp>/myrtui-mouse-debug-<pid>.log` |

Idle throttling is manual on purpose: there is no portable way to know whether
the terminal window is focused, so myrtui does not guess. Input bursts still run
at ~30 ms and mouse throttling is unaffected.

## Known limitations

- **TERM override risk**: pane true-colour support comes from
  `set -g default-terminal tmux-256color`. If your shell startup files reassign
  `TERM`, that wins — a documented tmux hazard myrtui does not fight.
- **Echo waits for the next poll** (~150 ms idle). This is polling latency, not
  a freeze; backend calls themselves are off the event loop.
- **Clicks in panes are delayed by the press duration** (typically 50–150 ms) so
  that dragging always belongs to selection.
- **Soft-wrapped paragraphs gain extra newlines** when copied across the wrap
  (capture is line-based).
- **The terminal's own selection is unavailable** — Textual enables mouse
  reporting, so drag belongs to myrtui. Some terminals let you bypass with
  `Shift`+drag.
- **pane left edge (1 cell) is inside the splitter hit area**; pressing there
  starts a splitter drag, not a selection.
- **No "copy whole screen"** — `Ctrl-a c` copies the selection only. For history,
  use the tmux backend directly:
  `tmux -L myrtui-<hash16> capture-pane -p -t myrtui-<hash16> -S - | pbcopy`.
- **The internal buffer is not persisted** — it disappears after detach.
- **Double click does not select** (Textual's default is disabled); double click
  only focuses a pane.
- **Uploads are not supported** — use sftp/scp. Dropping a file into the
  terminal is just a path paste.
