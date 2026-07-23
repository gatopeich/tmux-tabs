# AGENTS.md

Guidance for AI agents working in this repo. `CLAUDE.md` is a symlink to this file, so Claude Code
loads the same content automatically.

## What this is

See `README.md` for what the tool does and how it works (single-file Textual app, `--sidebar`
re-invoke, FIFO/hooks, placeholder panes). This file adds only what an agent needs beyond that.

## tmux server & safe testing

- The tool derives its tmux server socket from `$TMUX` and passes `-S <path>` on every call, so it
  always targets the spawning server. `-L`/`--socket` is an optional override, mainly for testing.
- **Never test against the live server** — the many `select-layout`/`swap-pane`/`kill-pane` calls will
  disrupt the user's real session. Run against a private server instead:
  `tmux -L ttabs-test new-session -d -x 120 -y 40 -s test`, then
  `tmux -L ttabs-test split-window -t test:0 -fhb -l 32 "python3.12 tmux_tabs.py --sidebar"`.
  Tear down with `tmux -L ttabs-test kill-server`.
- `textual` is installed under `python3.12` (not the default `python3`). Run the module with
  `python3.12 tmux_tabs.py`.
- To faithfully test **terminal resize** you need an *attached* client whose pty size you control
  (fork a pty running `tmux -L ttabs-test attach` and `ioctl(TIOCSWINSZ)`). A detached
  `resize-window` under `window-size manual` diverges from real behavior and can crash the server when
  adding windows.

## Fixed-width sidebar: the load-bearing invariants

The sidebar holds a **fixed character width**; it must not float proportionally when the terminal
resizes, and content panes must not reflow as the sidebar swaps between windows.

- **Resize vs. drag** is told apart by the total window width (`win_width`): a terminal resize changes
  it, a border drag never does. Comparing placeholder widths does NOT work under `window-size latest`.
- **`window-size latest` (the common default): background windows do not reflow on a terminal resize.**
  tmux only lays out the window currently shown to the attached client; other windows keep their old
  size until switched to the foreground, then float. This is why the resize/drag discriminator can't
  rely on background placeholders having floated yet.
- **`select-layout main-vertical` only resizes the currently displayed window.** To re-pin a
  placeholder in a *background* window use `resize-pane -x`, which takes effect on an unshown window.
- Any sidebar/placeholder width must be clamped window-safe (`< window_width`, `>= 1`) or tmux can
  wedge — see `_safe_width`.

### Reflow guarantee (under `window-size latest`)

A background agent session's content pane reflows **exactly once and only when unavoidable**: at the
moment you switch to its window, where tmux re-lays-out that window to the current terminal size. There
is no needless reflow, because:

- A terminal resize under `latest` does **not** touch background windows at all (verified: their
  `window_width` and content-pane width are unchanged until foregrounded). So there is nothing to keep
  consistent in the background — the content column width a running agent sees is already stable.
- On switch, `switch_to` resizes the target placeholder to the stored width *before* the `swap-pane`,
  so the content pane lands at its final width in one step instead of reflowing to a stale width and
  then again on the post-switch re-pin.

Do NOT add proactive "resize all background placeholders on `client-resized`" logic: under `latest` it
would reflow content panes whose windows have not changed size — the exact needless reflow this design
avoids. It would only be warranted under `window-size manual`/`largest` or `aggressive-resize on`,
where tmux resizes every window on a terminal change; that is not the target configuration.

## House rules

- Do NOT add complexity speculatively. Run the code, observe the exact failure, then fix that cause.
- Code-specific facts belong in code comments next to the code, not in this file.
- Do not commit unless asked.
