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
| `Ctrl-a b` | collapse / expand the sidebar (panes grow, tmux resizes with it); compact layout: open / close the file-tree page |
| `Ctrl-a s` | focus the file tree (compact layout: open its full page) |
| `Ctrl-a g` | focus the git history panel (compact layout: open its full page) |
| `Ctrl-a c` | copy the current selection (drag-release already copies) |
| `Ctrl-a v` | paste: system clipboard first, internal buffer as fallback |
| `Ctrl-a ?` | toggle the shortcut overlay (same as clicking or hovering `?`) |
| `Esc` | sidebar → back to the active pane |
| `Ctrl-a` then 1 s timeout | send a literal `Ctrl-a` to the inner shell |
| anything else | passed straight to the inner shell |

In the compact layout the three-row-tall bottom bar sends `ESC`, `ENT`, `⇧ ⇥`,
`/`, `@`, `↑`, `↓`, `C-J` and `DEL` with one tap; the
file-tree and Git pages switch it to their own buttons. `KEY` toggles it for the
current run; `MYRTUI_KEYBAR=off` disables it permanently.

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

### On a phone

Below 60 columns (or 16 rows) myrtui switches to a **compact layout**: the
sidebar is hidden, only the focused pane is shown (without the 1-cell border),
the tab strip is just `1 2 3 4`, and the top bar buttons shrink to
`+ X D K B G KEY ?` (`X` closes the focused pane, `B` / `G` open the file tree /
Git history as full pages — tap the lit one again to close it — and `KEY`
toggles the key bar). Four tabs plus eight buttons need 36 cells; on a narrower
screen `B` / `G` hide as a pair rather than pushing `?` off the edge. The
top bar stays **one row tall**, and a blank row separates it from the pane so
a slightly-off tap does not land in the terminal (for example opencode's own
tab strip). The key bar is **three rows tall**, with 3×3-cell keys and 1-cell
gaps laid out from the left (the right side stays empty for future keys), so a
46×22 phone screen is one 46×17 pane instead of four 10-column ones (46×20
with the key bar hidden). Moving back to a wider
window restores the layout; the thresholds have hysteresis (enter below 60,
leave at 64×18), so a soft keyboard appearing and disappearing does not flip the
layout back and forth.

The key bar is tuned for AI coding TUIs:

| Button | Sends | Why |
|---|---|---|
| `ESC` | Esc | interrupt / close a dialog |
| `ENT` | Enter | submit |
| `⇧ ⇥` | Shift+Tab | cycle agent / permission mode |
| `/` | `/` | slash commands |
| `@` | `@` | file mention |
| `↑` `↓` | arrows | prompt history / list navigation |
| `C-J` | Ctrl-J | newline without submitting (works in every terminal) |
| `DEL` | Backspace | fix a mistyped prefix |

Each key is 3×3 cells (the whole block is the hit area) and flashes accent for
150 ms when tapped. Keys are dropped from the tail by priority on very narrow
screens (the full pane row is 35 cells including gaps; 40/49 columns show all
9, 30 columns still fit 7). `Tab`, `Ctrl-C` and the left/right arrows are
intentionally absent: they either need typing anyway or overlap with `ESC`.

In a compact **file-tree page** the bar switches to `ESC` / `↑` / `↓` / `ENT` /
`≡` (Space, opens the context menu). `ENT` keeps the keyboard semantics: a file
row selects + opens the menu, a directory row expands / collapses; `≡` opens
the menu on any row (this is the only touch path to directory actions such as
"refresh this directory"). While the context menu is open the bar becomes **the
menu's own** — `ESC` / `↑` / `↓` / `ENT` move the highlight and select, instead
of tapping "outside the menu" and closing it. In the **Git history page** it
becomes `ESC` / `↑` / `↓` / `ENT`: `ENT` shows the full commit card for the
highlighted commit (subject, body, sha, author, date), replacing the desktop
hover card that has no room on a full-width phone page. Tap `ENT` again or tap
outside the card to close it.

If your phone terminal already has a complete extra-keys row, `MYRTUI_KEYBAR=off`
disables the bar permanently (the `KEY` button is hidden too). With `auto` (the
default), `KEY` only changes the current run.

- `Ctrl-a b` opens the **file tree** as a full page (the pane area is hidden);
  press it again, press `Esc`, or click a tab to return.
- `Ctrl-a s` / `Ctrl-a g` open the file tree / git history, **each as its own
  full page** — the other block is hidden, so the tree keeps the whole height
  and context menus have room.
- `Ctrl-a 1..4` or the tabs switch the visible pane. The hidden panes keep
  their tmux size until you switch back, then reflow once.
- Splitter handles are hidden while compact; the ratios in `layout.json` are
  left untouched and apply again on a wide screen.

`MYRTUI_COMPACT=on` forces the compact layout at any size; `off` restores the
old layout immediately (no code rollback needed).

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
already opened. The tree follows the disk itself, not just git: paths git cannot
see — ignored directories, the inside of a nested repository, a directory that
is not in any repository — are picked up the same way. Only `.git` is excluded.
A single directory with more than 2000 entries is skipped to keep the poll
cheap; use **Refresh this directory** there.

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
text can be dragged and copied with `Ctrl-a c`). In the compact **Git page**,
where there is no room for a side card and no hover, the bottom-bar `ENT`
key opens the same card over the list; tap it again or tap outside to close.
Selecting a file switches to
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
| `p` | export the whole document as an **A4 PDF** (see below) |
| `m` | export a **phone-reading PDF** (A4 at 12 pt, 6 mm margins) |
| arrows / `PgUp` / `PgDn` / `Home` / `End` / wheel | scroll |

Rendering: GFM headings, paragraphs, lists, tables, quotes, code blocks
(Textual's own highlighter, no Pygments). Inline math `$...$` / `\(...\)` and
block math `$$...$$` / `\[...\]` are rendered by `termtex` when installed
(fractions, matrices, `cases`); without it the LaTeX source is shown. Currency
like `$100 and $200` is not mistaken for math. Links do not navigate; the file
is not auto-refreshed — if it changes on disk while open, the footer says so and
`r` reloads it. Files above 512 KiB are refused with a
message; binary/missing/empty files show a readable reason.

While a preview (Markdown, LaTeX page view, image) is open, the panes behind it
are **not repainted**: a repaint of a hidden pane would be copied onto the
overlay and re-send a full-screen bitmap, which is exactly what makes the
LaTeX page flicker. The screen a preview was opened from is frozen the same
way. Closing the preview repaints panes, tabs and the sidebar immediately; tmux
itself never stops.

**LaTeX page view** (`v`): the whole document becomes self-contained HTML with
KaTeX, Chrome prints it to PDF at exactly the terminal viewport size, and
`pdftocairo` rasterises each page 1:1. One page = one screen, and navigation is
**page by page** (one keypress or one wheel notch per screen). The default theme
is sepia (`#E6DABE` page, `#3D3427` text, ~8.8:1);
`MYRTUI_PAGE_THEME` switches between `sepia` / `paper` / `gray` / `white` /
`dim` / `dark`. The trade-off: no text selection or search in this view, and
resizing re-typesets (1–2 s). Requires Chrome/Chromium + `pdftocairo` and a
Sixel/TGP terminal; missing pieces are reported instead of showing a blank page.
Rendered pages are cached under `<cache>/myrtui/pages/<sha1>/p-001.png…`.

> Why page snaps instead of free scrolling: a page is a full-screen bitmap, and
> `textual-image` keys its sixel cache on the visible crop — scrolling one line
> re-encodes both partially visible pages (measured ~380 ms each for a
> 2520×1260 page, on the UI thread), and key scrolling used to animate (a dozen
> such frames per press). That is what made this view flicker and feel sluggish.
> Snapping to page boundaries keeps exactly one complete page on screen, so the
> cache always hits; the cost is that there is no half-page transition.

**Export as A4 PDF** (`p`): the whole document runs through the **same**
md → HTML pipeline as the page view, but prints onto A4 paper with a white
background and black text — a vector PDF you can send around or print, not a
bitmap. The file lands **next to the source** (`README.md` → `README.pdf`); an
existing file is replaced only after Chrome succeeds, so a failed export never
leaves a half-written PDF behind. Math goes through KaTeX (same delimiters as
the preview), local images are embedded as data URIs, and remote (`http(s)`)
images are dropped — export never touches the network. Long code lines wrap
rather than being clipped. Requires Chrome/Chromium (same as `v`;
`MYRTUI_CHROME` overrides the binary). There are **no page numbers**: Chrome
cannot stamp them without an extra PDF library, and this repo does not add
runtime dependencies for that.

**Phone-reading PDF** (`m`): the same document and the same pipeline, but
typeset for a phone screen: still **A4** (a wide page keeps formulas and tables
from being squeezed into a narrow column), body text **12 pt** (vs 10.5 pt) and
**6 mm** margins (vs 16/14 mm). The file lands next to the source as
`<stem>.phone.pdf`, so it never overwrites the A4 export. Over-wide formulas,
code blocks and tables are scaled down to the page width instead of being
clipped — rotate the phone or zoom in to read those. Like `p`, it needs
Chrome/Chromium and writes the file atomically; unlike `p`, the 6 mm margin is
for on-screen reading, not for physical printers.

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
support you still get the metadata and a hint. **Inside an outer tmux, bitmaps
are off**: tmux replaces a Sixel image with a `SIXEL IMAGE (WxH)` placeholder and
a wall of `+` cells (measured on tmux 3.8; adding `sixel` to `terminal-features`
does not help). Kitty TGP can pass through with `set -g allow-passthrough on`,
but only if the terminal implements Unicode placeholders (`U=1`) — kitty and
Ghostty do, WezTerm and Konsole do not. `MYRTUI_GRAPHICS=unicode` forces the
text fallback. Which terminals have been measured is in the
README under [Terminal support](../README.md#terminal-support).

A terminal that draws images but **not** Unicode placeholders (some mobile SSH
clients) still gets the `v` page view and the image preview: myrtui probes Kitty's
direct display (`a=T`, one still image per screen) and uses it when placeholders
are unavailable; inside tmux that path also needs
`set -g allow-passthrough on`. Scrolling views keep the text fallbacks — a
direct-display image does not move with the text.

**Sixel is used for those two views only.** An encoded sixel cannot be re-placed
(Kitty has `a=p`, sixel has nothing), so images in the scrolling Markdown body
keep their `alt` text. The page view and the image preview write the whole DCS
after the frame (`ESC7` + `CSI r;cH` + DCS + `ESC8`) instead of handing it to
Textual's renderer, which crops escape sequences by cell width and cuts off the
`ST` terminator.

## Splitters

All splitters are one cell thick; press within ±1 cell of the line to grab it
(the line brightens on hover). Dragging uses the **left button**; a double click
resets that dimension. If a left drag does nothing at all, an enclosing tmux is
probably capturing it — see [Inside another tmux](#inside-another-tmux-uu-remote-a-local-mirror).

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

Dragging selects and copies, but **which side owns the drag follows the terminal
convention** (`MYRTUI_DRAG_SELECT=smart`, the default): when the pane program
asked for mouse (vim, htop, opencode), the drag is forwarded to it — its own
selection UI works, and whatever it copies is forwarded back to you (below).
**Shift+drag** selects locally instead. When the program did not ask for mouse,
the drag is myrtui's: releasing copies automatically (OSC 52 plus a native
clipboard command) and the selection stays highlighted for 600 ms so you can see
what was copied. `MYRTUI_DRAG_SELECT=always` takes every drag back (v0.35).

- Copied text: trailing blank cells trimmed, CJK/wide characters aligned per
  cell, and only the pane where the press started contributes content.
- Channels: OSC 52 (works in iTerm2/WezTerm/Windows Terminal) plus
  `pbcopy` / `wl-copy` / `xclip` / `clip.exe`. macOS Terminal.app does not
  support OSC 52 and relies on the command. **Both are used by default**; narrow
  them with `MYRTUI_CLIPBOARD=osc52` or `=system`. Over SSH the system clipboard
  is the *remote* machine's one: harmless, and what you want when the
  remote-desktop client syncs clipboards (UU Remote, ToDesk, …).
- **Copies made by the program inside a pane are forwarded.** Select-to-copy in
  opencode, codex's `/copy`, `vim`'s `"+y`: the program emits OSC 52 into the
  pane, where tmux either stashes it in a paste buffer (bare form) or drops it
  entirely (the `ESC Ptmux;` form every `$TMUX`-aware TUI sends). myrtui reads it
  from the control channel and forwards it to your terminal and system clipboard,
  showing `Copied N chars from pane k`. Turn it off with
  `MYRTUI_CLIPBOARD_PASSTHROUGH=0`; a pane program can then still change your
  clipboard only through its own native command. One forwarded copy is capped at
  256 KiB (decoded); anything larger is ignored and only logged.
- `MYRTUI_COPY_ON_SELECT=0` disables auto-copy: the selection stays for 10 s,
  `Ctrl-a c` copies it, anything else cancels.
- `MYRTUI_DRAG_SELECT`: `smart` (default) — program gets the drag, Shift+drag
  selects locally; `always` — every drag selects locally (v0.35 behaviour, the
  program never sees a drag); `app` — the drag goes to the program whenever it
  asked for mouse, without a Shift escape hatch. Clicks, wheel and right/middle
  click keep working in all three modes, and myrtui stops its own highlight
  while a drag is forwarded, so you never see two selections at once.
- **Right-click pastes**, always — the terminal convention (Windows Terminal
  does the same), and the only way to paste at all once mouse reporting is on.
  **Shift+right-click** hands the button back to the program, so `vim` and
  `opencode` keep whatever they do with it (none of them use it by default).
- Pasting (`Ctrl-a v`) prefers the system clipboard and falls back to the
  internal buffer. With `MYRTUI_CLIPBOARD=osc52` there is no clipboard to read,
  so myrtui only tells you which terminal shortcut to use (macOS `Cmd+V`,
  Windows Terminal `Ctrl+V`, Tabby `Ctrl+Shift+V`). Content is pasted verbatim,
  including trailing newlines.

### Inside another tmux (UU Remote, a local mirror)

myrtui can itself run inside another multiplexer — **UU Remote's terminal is a
tmux 3.5a session**, and its app bundle ships a `MouseDrag1Pane` binding that
sends *every* drag to copy-mode, ignoring `mouse_any_flag`:

```sh
bind -Troot MouseDrag1Pane if-shell -F "#{pane_in_mode}" "send -X begin-selection" "copy-mode -M"
```

The press still reaches myrtui, every motion in between is eaten by copy-mode,
and the release copies the outer selection instead. The symptom is *"dragging
selects nothing, but something does land in the clipboard"* — and it is also why
`opencode` never sees its own drag-to-copy: its selection UI needs the motion, so
it never draws a selection and never emits the OSC 52 that myrtui forwards.

At startup myrtui reads the **live** binding of the outer server (the socket is
found by walking the parent process tree and verified by pid, so a different tmux
on the same machine is never mistaken for it). If the binding is the
unconditional one, the top bar carries the exact fix command — on a wide
terminal as-is (the command is ~181 characters, so it needs roughly 230 columns;
medium widths keep the command with a shorter prefix),
otherwise a one-liner plus the full command in `myrtui.log`. A binding that
already forwards the drag is left alone, silently.

Restore tmux's default check (forward to panes that asked for the mouse, keep
copy-mode for the rest):

```sh
UU="/Applications/UURemote.app/Contents/Helpers/tmux/uuyc-mux"
S="$HOME/Library/Application Support/UURemote/tmux.sock"
"$UU" -S "$S" bind -Troot MouseDrag1Pane \
  if-shell -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' \
  'send-keys -M' 'copy-mode -M'
```

Any tmux takes the same command (`tmux bind -Troot MouseDrag1Pane …`); check it
with `list-keys -T root | grep MouseDrag1Pane`. Afterwards a drag over a program
that requested the mouse is forwarded (myrtui then applies `MYRTUI_DRAG_SELECT`),
while dragging plain shell output still copies through the outer tmux.

The binding lives in the outer server's memory (it is server-wide, so running the
command once fixes every window attached to that server), and **UU reloads its own
copy from the app bundle every time that server starts** — so the fix has to be
re-applied per server lifetime. A shell function in your `~/.zshrc` is the least
annoying way (it is a no-op when the UU socket is absent):

```sh
myrtui-outer-fix() {
  local UU="/Applications/UURemote.app/Contents/Helpers/tmux/uuyc-mux"
  local S="$HOME/Library/Application Support/UURemote/tmux.sock"
  [ -S "$S" ] || return 0
  "$UU" -S "$S" bind -Troot MouseDrag1Pane \
    if-shell -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' \
    'send-keys -M' 'copy-mode -M'
}
myrtui-outer-fix
```

Do not edit the config inside the UU app bundle: upgrades and code signing will
fight you. `tmux set -g mouse off` is the blunt alternative — every mouse event
goes to the program, but the outer tmux's own drag-to-copy disappears with it.

While the outer tmux is still eating drags, **its own copy-mode is the way to copy
anything off myrtui's screen** — including the fix command in the top bar: drag
over it, release, and the rendered line (command and all) lands in the clipboard.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MYRTUI_LANG` | `en` | UI language: `en` / `zh_CN`. Also read from `config.json`'s `lang` and from a `zh*` locale; `--lang` wins for one run |
| `MYRTUI_COMPACT` | `auto` | narrow-screen layout: `auto` enters the compact layout below 60 columns / 16 rows and leaves at 64×18 (hysteresis); `on` forces it, `off` keeps the old layout |
| `MYRTUI_KEYBAR` | `auto` | compact-layout touch bar (3 rows tall, 3×3-cell keys with 1-cell gaps, left-aligned): AI-coding keys in a pane, navigation / `≡` / `ENT` in the tree and Git pages; keys flash for 150 ms; `KEY` toggles it for the current run; `off` disables it permanently (and hides `KEY`) |
| `MYRTUI_EDITOR` | — | editor for "Open" (see resolution order above) |
| `MYRTUI_IDLE_INTERVAL` | `0.15` | idle capture interval in seconds (clamped to `0.05–5.0`); while content keeps changing the loop speeds up to ~17 Hz by itself; raise it for secondary instances (`MYRTUI_IDLE_INTERVAL=0.5 myrtui`) |
| `MYRTUI_TRANSPORT` | `auto` | tmux transport: `auto` = a resident `tmux -C` client, falling back to one process per call when attach fails; `exec` = one process per call (old path); `control` = require the resident client (errors out if it cannot start) |
| `MYRTUI_COPY_ON_SELECT` | `1` | auto-copy on drag release (`0/false/no/off` disables) |
| `MYRTUI_CLIPBOARD` | `auto` | copy channels: `auto` = OSC 52 + system command, `osc52` = terminal only, `system` = native clipboard command only |
| `MYRTUI_CLIPBOARD_PASSTHROUGH` | `1` | forward copies made by programs inside panes (OSC 52) to the terminal / system clipboard (`0/false/no/off` disables) |
| `MYRTUI_OUTER_MUX_HINT` | `1` | show the "outer tmux detected" hint at startup when myrtui runs inside another tmux/screen: it reads the *live* `MouseDrag1Pane` binding and, if that binding swallows drags, prints the fix command on the top bar (or in `myrtui.log` when it does not fit); `0/false/no/off` disables the probe and the hint |
| `MYRTUI_OUTER_TMUX_SOCKET` | — | explicit outer tmux socket, when the automatic detection (parent process tree) cannot find it |
| `MYRTUI_DRAG_SELECT` | `smart` | `smart` = the program gets the drag when it asked for mouse, Shift+drag selects locally; `always` = drag always selects; `app` = forward every drag to the program |
| `MYRTUI_GRAPHICS` | `auto` | `auto` / `sixel` / `tgp` / `unicode` / `off` (preview bitmaps only) |
| `MYRTUI_CELL_PX` | — | override cell size in pixels (`10x20`) when the terminal does not report it |
| `MYRTUI_PAGE_THEME` | `sepia` | `sepia` / `paper` / `gray` / `white` / `dim` / `dark` for the LaTeX page view |
| `MYRTUI_MATH_BACKEND` | `auto` | `auto` (use `termtex` when found) / `plain` / `termtex` |
| `MYRTUI_CHROME` | — | Chrome/Chromium binary for the LaTeX page view |
| `MYRTUI_PDFTOCAIRO` | — | PDF → PNG converter (`pdftocairo` preferred, then `pdftoppm`) |
| `MYRTUI_LOOP_DEBUG` | — | `=1` logs event-loop delays > 30 ms to stderr (profiling) |
| `MYRTUI_LOG` | platform log dir | where warnings go once the TUI owns the terminal (`~/Library/Logs/myrtui/myrtui.log` on macOS, `~/.local/state/myrtui/log/` on Linux); empty disables |
| `MYRTUI_MOUSE_DEBUG` | — | `=1` logs every mouse event to `<tmp>/myrtui-mouse-debug-<pid>.log` |

Idle throttling is manual on purpose: there is no portable way to know whether
the terminal window is focused, so myrtui does not guess. Input bursts still run
at ~30 ms and mouse throttling is unaffected. The one exception is content:
while captures keep changing (a `tail -f`, an animating image) the loop speeds
itself up to ~17 Hz and drops back to the idle interval 0.3 s after the last
change — that does not depend on focus.

## Known limitations

- **TERM override risk**: pane true-colour support comes from
  `set -g default-terminal tmux-256color`. If your shell startup files reassign
  `TERM`, that wins — a documented tmux hazard myrtui does not fight.
- **Echo waits for the next poll** (~150 ms idle). This is polling latency, not
  a freeze; backend calls themselves are off the event loop.
- **Clicks in panes are delayed by the press duration** (typically 50–150 ms)
  when the drag belongs to myrtui (the program did not ask for mouse, or
  `MYRTUI_DRAG_SELECT=always`); programs that requested mouse get the press
  immediately.
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
