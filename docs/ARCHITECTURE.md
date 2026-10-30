# Architecture

myrtui is a Textual front end that **does not emulate a terminal**. Every pane
is a real tmux pane; the UI polls `capture-pane` and paints the text into a
Textual widget. tmux owns PTYs, scrollback, mouse modes and the inner shell;
Textual owns layout, focus, input routing and the preview layers.

```
             ┌──────────────────── Textual app (myrtui/client) ───────────────────┐
 keystrokes  │  app.py ── focus / prefix keys / polling loop                       │
 mouse  ───► │   ├─ topbar.py      pane tabs, Detach/Kill/?, status line           │
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
| `tmux_backend.py` | every tmux invocation: session creation, pane split/close/respawn, `capture-pane`, `send-keys`, `paste-buffer`, resize, `set-clipboard` |
| `i18n.py` / `locales/` | UI language resolution (`--lang` > `MYRTUI_LANG` > `config.json` > locale > `en`), flat `key -> text` catalogs, `t()` with `en` fallback; no Textual import, no module-level `t()` |
| `layout_state.py` | splitter ratios: `0..1` per dimension, atomic JSON write, format version 2, corrupt file → defaults |
| `gitlog.py` / `gitstat.py` | `git log` (repo-wide and `--follow` per file) and `git status --porcelain -z -uall` parsing, in worker threads |
| `fileview.py` | decoding a preview (UTF-8-SIG → UTF-8 → locale → replace), size/line limits, binary detection |
| `mouse.py` | mouse event decoding and routing (SGR/legacy), drag selection, click-vs-drag arbitration |
| `client/app.py` | the App: CSS, focus model, `Ctrl-a` prefix state machine, pane layouts, polling loop, clipboard actions |
| `client/pane_widget.py` | one widget per pane: renders captured text, dead-pane styling, focus border |
| `client/separators.py` | splitter hit areas (±1 cell), drag/double-click reset, ratio persistence |
| `client/sidebar.py` / `file_tree.py` / `git_panel.py` | left column: tree with git colours + commit history with hover details |
| `client/context_menu.py` | right-click / keyboard menu over the tree (open, preview, copy path) |
| `client/docpage.py` / `pageview.py` | paged Markdown reader and the LaTeX page view (KaTeX → Chrome → PDF → PNG) |
| `client/mdpreview.py` / `mathtext.py` / `mathrender.py` / `mathimg.py` / `katex.py` | Markdown rendering, `$math$` handling, `termtex` bridge, KaTeX asset pipeline |
| `client/imagepreview.py` / `imageprep.py` / `graphics.py` | image preview, decode/resize/compose in workers, Sixel / TGP detection |
| `client/clipboard.py` | OSC 52 + platform clipboard commands, SSH detection, internal buffer fallback |

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
   for a few ticks after input. Backend calls run off the event loop, so a slow
   tmux never blocks the UI thread.
4. Paint is *display only*: keys, paste and mouse are injected back into tmux;
   myrtui never interprets the pane's output.

Consequences worth knowing: echo waits for the next poll (~150 ms idle), and
soft-wrapped lines appear as separate captured lines. Both are recorded in
`README.md` under limitations.

## Preview layers

Previews are overlay screens, not panes — that is why they work while a pane is
busy running `vim` or `ssh`.

- **Markdown**: GFM via Textual's own Markdown widgets; inline/block math is
  rendered by the optional `termtex` binary and degrades to LaTeX source.
- **LaTeX page view** (`v`): the document becomes a self-contained HTML page
  (KaTeX auto-render), Chrome prints it to PDF at exactly the terminal viewport
  size, `pdftocairo` rasterises each page 1:1, and pages are shown as bitmaps.
  A page = a screen; the price is no text selection in that view.
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
