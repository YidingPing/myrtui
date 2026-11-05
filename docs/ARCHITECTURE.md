# Architecture

myrtui is a Textual front end that **does not emulate a terminal**. Every pane
is a real tmux pane; the UI polls `capture-pane` and paints the text into a
Textual widget. tmux owns PTYs, scrollback, mouse modes and the inner shell;
Textual owns layout, focus, input routing and the preview layers. The single
exception to "paint only" is OSC 52: copies made by the program inside a pane are
read off `%output` and forwarded to the real clipboard (`myrtui/osc52.py`), still
without interpreting a single cell.

```
             ┌──────────────────── Textual app (myrtui/client) ───────────────────┐
 keystrokes  │  app.py ── focus / prefix keys / polling loop                       │
 mouse  ───► │   ├─ topbar.py      pane tabs, Detach/Kill/⌨/?, status line        │
             │   ├─ keybar.py      context-aware touch bar (compact only)         │
             │   ├─ sidebar.py ── file_tree.py (lazy dirs, git colours)            │
             │   │              └ git_panel.py (log, per-file, hover card)          │
             │   ├─ separators.py  draggable 1-cell splitters (ratios persisted)   │
             │   ├─ pane_widget.py captured text per pane (focus border)           │
             │   └─ preview layers: docpage.py / mdpreview.py / imagepreview.py    │
             └───────────────┬─────────────────────────────────────────────────────┘
                             │  send-keys / paste-buffer / capture-pane / resize
             ┌───────────────▼───────────── tmux_backend.py ───────────────────────┐
             │  tmux -L myrtui-<hash16> … (one isolated server per project)        │
             └───────────────┬─────────────────────────────────────────────────────┘
                             │
                    real PTYs / shells / whatever you run in them
```

## Module map

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, instance identity, lock file + takeover protocol, `--root/--shell/--scrollback/--lang`, `ls` / `attach` / `stop` wiring, layout file paths |
| `instances.py` | instance discovery and lifecycle: lock probing, `ls` table/JSON, attach/stop target resolution, cooperative shutdown marks |
| `tmux_backend.py` | every tmux invocation: session creation, pane split/close/respawn, `capture-pane`, `send-keys`, `paste-buffer`, resize, `set-clipboard`; the two transports (`exec` / resident `control`), command quoting and framing; the `%output` clipboard watch |
| `termenv.py` | detects an enclosing tmux/screen (UU Remote's terminal is one); its mouse bindings can take a drag before it reaches myrtui, so the app only warns |
| `osc52.py` | byte-level extraction of OSC 52 (bare and `ESC Ptmux;` wrapped) from `%output` payloads, including tmux's octal escaping, size caps and query rejection; reassembles sequences split across the ~1 KiB `%output` frames |
| `i18n.py` / `locales/` | UI language resolution (`--lang` > `MYRTUI_LANG` > `config.json` > locale > `en`), flat `key -> text` catalogs, `t()` with `en` fallback; no Textual import, no module-level `t()` |
| `layout_state.py` | splitter ratios: `0..1` per dimension, atomic JSON write, format version 2, corrupt file → defaults |
| `gitlog.py` / `gitstat.py` | `git log` (repo-wide and `--follow` per file) and `git status --porcelain -z -uall` parsing, in worker threads |
| `fileview.py` | decoding a preview (UTF-8-SIG → UTF-8 → locale → replace), size/line limits, binary detection |
| `mouse.py` | mouse event decoding and routing (SGR/legacy), drag selection, click-vs-drag arbitration |
| `client/app.py` | the App: CSS, focus model, `Ctrl-a` prefix state machine, pane layouts, compact (narrow-screen) mode, polling loop, clipboard actions |
| `client/keybar.py` | compact layout's one-row touch key strip: 3-cell keys + 1-cell gaps, context sets (AI-coding keys / tree navigation / Git details via Enter), width/priority fitting, `MYRTUI_KEYBAR=off`; delegates to `app._dispatch_key` / `_dispatch_page_action` |
| `client/pane_widget.py` | one widget per pane: renders captured text, dead-pane styling, focus border |
| `client/separators.py` | splitter hit areas (±1 cell), drag/double-click reset, ratio persistence |
| `client/sidebar.py` / `file_tree.py` / `git_panel.py` | left column: tree with git colours + commit history with hover details |
| `client/context_menu.py` | right-click / keyboard menu over the tree (open, preview, copy path) |
| `client/docpage.py` / `pageview.py` | paged Markdown reader and the LaTeX page view (KaTeX → Chrome → PDF → PNG, page-snapped scrolling) |
| `client/pdfexport.py` | A4 PDF export (`p` in the Markdown preview): print stylesheet + Chrome print → `<source>.pdf`, atomic replace |
| `client/overlay.py` | covered screens stop their update timer, so a full-screen bitmap is never re-sent under an overlay |
| `client/mdpreview.py` / `mathtext.py` / `mathrender.py` / `mathimg.py` / `katex.py` | Markdown rendering, `$math$` handling, `termtex` bridge, KaTeX asset pipeline |
| `client/imagepreview.py` / `imageprep.py` / `graphics.py` | image preview, decode/resize/compose in workers, Sixel / TGP detection |
| `client/clipboard.py` | copy channels (`MYRTUI_CLIPBOARD`: OSC 52 + platform clipboard command), read path and internal-buffer fallback |

## Identities and state

- **Instance identity** = `sha256(realpath(project_root))[:16]`. It names the
  tmux socket (`myrtui-<hash16>`), the lock file, the layout file and the
  takeover marker. Symlinks are normalised first, so the same directory always
  resolves to the same instance.
- **One front end per instance**: an exclusive `flock` guarantees it. A second
  `myrtui` in the same directory starts a *takeover* handshake — the old front
  end exits cleanly, the shells stay alive.
- **State on disk is optional and disposable**: `<user_data_dir>/myrtui/<hash>.layout.json`
  (splitter ratios) and lock markers. Delete them and myrtui starts with
  defaults; a corrupt layout file is ignored rather than fatal.
- **No state inside the project directory.** Nothing is written next to your
  code.

## Rendering loop

1. `capture-pane -p` (with `-N` to keep trailing spaces) for each pane.
2. Text is turned into Textual renderables; unchanged panes are skipped.
3. Idle cadence is `MYRTUI_IDLE_INTERVAL` (default 0.15 s), raised to ~30 ms
   for a few ticks after input, and raised to ~17 Hz while captures keep
   changing (a `tail -f` or an animating image) until 0.3 s of stillness.
   Backend calls run off the event loop, so a slow tmux never blocks the UI
   thread.
4. Paint is *display only*: keys, paste and mouse are injected back into tmux;
   myrtui never interprets the pane's output, except for OSC 52 clipboard
   requests (above), which are forwarded rather than rendered.

Consequences worth knowing: echo waits for the next poll (~150 ms idle), and
soft-wrapped lines appear as separate captured lines. Both are recorded in
`README.md` under limitations.

## Transport

Two ways to talk to the tmux server, chosen by `MYRTUI_TRANSPORT` (default
`auto`):

- **`exec`** — one `tmux` process per call. Simple, and the fallback: every
  keystroke and every capture costs a process launch (4.5–5 ms each on an idle
  box, 8–16 ms inside the app once rendering competes for the GIL).
- **`control`** — one resident `tmux -C attach` client for the whole session.
  Commands are written as a single line, output is framed by random START/END
  sentinels (`%begin`/`%end` block markers are matched on the exact triple the
  block opened with, because pane content may itself start with `%`), and one
  round trip costs 0.06–0.3 ms. `auto` uses it when `attach` succeeds and falls
  back to `exec` with a warning otherwise.

Both paths share the command builder and parser, so `check=False` semantics and
`stdout` line counts are identical; the transport only decides who executes.

`%output` is the one thing read from the stream, and only for OSC 52: a program
inside a pane that copies text (opencode's select-to-copy, `vim "+y`) writes that
sequence into the pane, where tmux either stashes it in a paste buffer (bare
form) or drops it entirely (the `ESC Ptmux;` wrapper every `$TMUX`-aware TUI
sends — measured on tmux 3.8). The control client hands every matching line to
the backend, which reassembles per pane before decoding: one `%output` frame
carries roughly 1 KiB of pane output (1024 bytes measured on tmux 3.8 / macOS),
so a longer copy arrives in several frames and only the first one has the
`ESC ]52;` header. Decoded copies are capped at 256 KiB; larger ones are dropped
with a log line. `exec` transport falls back to diffing `list-buffers` once a
second, which covers the bare form only. Nothing else is interpreted: myrtui
still paints captured text and never emulates a terminal. Things the line
protocol cannot carry — stdin (`load-buffer`/paste), arguments containing
newlines, `kill-server`, the pre-client lifecycle commands — always go through
`exec`. A dropped control
client is closed, the current command runs on `exec`, and reconnection is
attempted in the background (1 s / 2 s / 5 s) without surfacing a new UI state.

Warnings never go to stderr once Textual owns the terminal (that would paint on
the cursor); they are written to the platform log dir (`MYRTUI_LOG` overrides,
empty disables).

## Preview layers

Previews are overlay screens, not panes — that is why they work while a pane is
busy running `vim` or `ssh`.

While an overlay is on top, the app **does not paint the main screen at all**
(panes, tabs, top bar, sidebar, git log). Textual copies a background screen's
dirty regions onto the top screen, so a single pane update would force the
overlay to repaint the same rows — and if the overlay shows a full-screen Sixel
page, that re-sends the whole bitmap (measured: 34 frames / 3.2 MB in 2 s, i.e.
a visible flicker even while "idle"). Polling keeps running so topology, dead
panes and mouse modes stay current; the diff caches are *not* advanced, so the
first poll after closing the overlay repaints everything (`ScreenResume` also
triggers that poll immediately).

Covered screens also **stop their own update timer** (`client/overlay.py`,
`QuietWhenCovered`). Textual's propagation path re-marks the background screen
as needing a repaint, which makes "background dirty → top screen repaints →
background dirty again" a self-sustaining loop: with the page view open over the
Markdown preview that loop alone emitted two full-screen bitmaps per second.
The timer resumes when the screen is uncovered; `ScreenResume` repaints it.

- **Markdown**: GFM via Textual's own Markdown widgets; inline/block math is
  rendered by the optional `termtex` binary and degrades to LaTeX source.
- **LaTeX page view** (`v`): the document becomes a self-contained HTML page
  (KaTeX auto-render), Chrome prints it to PDF at exactly the terminal viewport
  size, `pdftocairo` rasterises each page 1:1, and pages are shown as bitmaps.
  A page = a screen; navigation snaps to page boundaries because
  `textual-image` keys its sixel cache on the visible crop — a partially visible
  page is re-encoded in full on every scroll step (~380 ms for a 2520×1260
  page), which is what makes free scrolling flicker and stall. The price is no
  text selection in that view.
- **Images**: decoded and resized in a worker, composited against the theme
  background (terminals do not do alpha), then drawn with Sixel or Kitty TGP
  when available; otherwise metadata plus a text placeholder.

## Invariants to keep

- A pane is never faked: if tmux has no pane, the widget says so (`N:dead`)
  instead of showing stale text.
- Every external tool (`termtex`, `ov`, `micro`, Chrome, `poppler`, clipboard
  commands) is optional; missing tools degrade to a documented fallback and
  never crash or print garbage.
- Anything that can block (git, decoding, image work, tmux) runs in a worker
  thread; the UI thread only reacts to results.
- A click inside a pane is injected on release so that dragging can always
  belong to selection (`MYRTUI_DRAG_SELECT=app` opts out).
