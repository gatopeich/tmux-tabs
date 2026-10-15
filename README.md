# tmux-tabs

A vertical window sidebar for tmux — like browser tabs down the left edge, with bell highlights,
live terminal titles, and one-click switching. Built with [Textual](https://textual.textualize.io/).

Originally *"like Claude Squad, without fuss"*: a sidebar to track all your agent/terminal sessions
at a glance.

## What it does

- Lists every tmux window in a narrow left pane, each with its name and live pane title.
- Click a window to switch to it — a single click works even when the sidebar isn't focused.
- Highlights the active window and flags bells (🔔).
- Follows you automatically: switching windows by keyboard, or opening a new one, moves the sidebar
  along (event-driven via tmux hooks, so it's instant — not polled).
- Keeps every content pane at a constant width by parking a thin placeholder pane in each window's
  sidebar slot and swapping it with the real sidebar, so panes never reflow as you move around.
- A yellow title bar with a `(X)` close button (with a `(Y)`/`(N)` confirm) — no need to focus the
  pane to quit.
- Keeps a basic history of every panel in `~/.cache/tmux-tabs/`: one file per pane, recording its
  name, title, working directory, running command, and (best-effort) Claude session id. A
  `<history>` button opens a `HISTORY` window listing them all, newest-changed first.

Colors are read live from your tmux/byobu theme (`window-status-style`, etc.), so the sidebar
matches your color scheme automatically.

## Install

```sh
pip install --user .
```

Requires Python ≥ 3.12 and tmux. `textual` is pulled in automatically.

## Usage

Run it inside any tmux pane:

```sh
tmux-tabs
```

It splits off a 25%-wide sidebar on the left and hands your original pane back as the content pane.

To quit: click the `(X)` button, then `(Y)` to confirm.

## How it works

`tmux-tabs` is a single Textual app. A bare launch splits off the sidebar pane and re-invokes itself
with `--sidebar` to run the TUI there. Window changes are delivered through a per-session FIFO poked
by global `after-select-window` / `after-new-window` hooks, with a slow poll as a safety net. The
sidebar and its placeholders all carry the pane title `(tmux-tabs)` so a fresh launch can reap any
panes a crashed instance left behind.
