#!/usr/bin/env python3.12
"""
tmux-tabs — vertical window sidebar for tmux.

Shows all tmux windows in a narrow left pane with bell highlights and terminal
titles. Click any window to switch to it. Automatically follows focus when a
new window is opened (e.g. via F2 in byobu).

Usage:
  Run `tmux-tabs` in any tmux pane — it splits off a 25%-wide left sidebar and
  hands the original pane back as the content pane.

  Quit: click the (X) button, or press q inside the sidebar.

Requirements:
  pip install --user textual   (python3.12)

Colors are read live from the tmux theme (window-status-style, etc.) so the
sidebar matches your current byobu/tmux color scheme automatically.
"""

import asyncio
import os
import subprocess
import sys
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Label


def tmux(*args, check=False):
    """Run tmux, returning stripped stdout. check=True raises CalledProcessError on failure."""
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check).stdout.strip()


def pane_var(pane, fmt):
    return tmux("display-message", "-p", "-t", pane, fmt)


PANE_FIELDS = 10  # fields per pane in the dump below; free-text name/title kept last (newline-safe)


def tmux_panes(session):
    # ONE call dumping every pane of every window IN OUR SESSION (-s, not -a server-wide, which
    # would manage other sessions' windows) — the sole tmux read per refresh. Newline per field
    # because window_name/pane_title are free text that can hold any delimiter (tmux collapses
    # newlines in them). Fixed groups; name and title come last.
    f = tmux("list-panes", "-s", "-t", session, "-F", "#{window_id}\n#{window_index}\n#{window_flags}\n"
             "#{window_bell_flag}\n#{pane_id}\n#{pane_active}\n#{pane_width}\n#{window_width}\n"
             "#{window_name}\n#{pane_title}").splitlines()
    return [
        {"win": wid, "index": idx, "active": "*" in flags, "last": "-" in flags,
         "bell": bell == "1", "pane": pid, "pane_active": pa == "1", "width": int(width),
         "win_width": int(ww), "name": name, "title": title}
        for wid, idx, flags, bell, pid, pa, width, ww, name, title in zip(*[iter(f)] * PANE_FIELDS)
    ]


def parse_style(style):
    return {k: v or True for k, _, v in (p.strip().partition("=") for p in style.split(","))}


def tmux_styles():
    keys = {"normal": "window-status-style", "current": "window-status-current-style",
            "bell": "window-status-bell-style", "status": "status-style"}
    return {name: parse_style(tmux("show-options", "-gv", key)) for name, key in keys.items()}


def style_to_css(s, base=None):
    bg = s.get("bg", base.get("bg", "default") if base else "default")
    fg = s.get("fg", base.get("fg", "default") if base else "default")
    if s.get("reverse"):
        bg, fg = fg, bg
    bold = "\n        text-style: bold;" if s.get("bold") else ""
    return f"background: {bg};\n        color: {fg};{bold}"


class WindowItem(Label):
    def __init__(self, win, app_ref):
        self.win_index = win["index"]
        self._app_ref = app_ref
        icon = "🔔" if win["bell"] else ("▶" if win["active"] else " ")
        title = win["title"].lstrip("✳").strip()
        label = f"{icon} [b]{win['name']}[/b]\n    [dim]{title}[/dim]" if title else f"{icon} [b]{win['name']}[/b]"
        classes = "window" + (" bell" if win["bell"] else "") + (" active" if win["active"] else "")
        super().__init__(label, classes=classes, markup=True)

    def on_mouse_down(self):
        # MouseDown (not Click): the forwarded press arrives on the first click even when our
        # pane is unfocused, whereas Click needs a MouseUp pair that the focus-grab click drops.
        self._app_ref.switch_to(self.win_index)


class Button(Label):
    """A clickable banner button that calls a named app method on mouse-down."""

    def __init__(self, label, app_ref, action):
        self._app_ref = app_ref
        self._action = action
        # markup=False: "[X]"/"[Y]"/"[N]" must render literally, not be parsed as Rich tags.
        super().__init__(label, classes="btn", markup=False)

    def on_mouse_down(self):
        getattr(self._app_ref, self._action)()


class TmuxTabs(App):
    DEFAULT_CSS = ""
    BINDINGS = [("q", "quit", "Quit")]
    windows = reactive([], recompose=True)
    confirming = reactive(False, recompose=True)
    _busy = False           # reentrancy guard: our own select-window refires the FIFO hook
    _session = None         # our session id; placeholder management is scoped to it
    _pane_id = None
    _current_window = None  # window index the sidebar currently sits in
    _selected_pane = None   # content pane beside us; bounce focus here, None when none exists
    _width = None           # width placeholders were last sized to; skip resize when unchanged
    _fifo_path = None
    _fifo_fd = None

    @classmethod
    def build_css(cls):
        s = tmux_styles()
        base = s["status"]
        bg = s["normal"].get("bg", base.get("bg", "#94cdba"))
        fg = s["normal"].get("fg", base.get("fg", "black"))
        normal, bell, current = (style_to_css(s[k], base) for k in ("normal", "bell", "current"))
        return f"""
    * {{ scrollbar-background: {bg}; scrollbar-color: {fg}; scrollbar-size: 0 0; }}
    Screen {{ background: {bg}; color: {fg}; layers: base; }}
    #banner {{ height: auto; width: 100%; background: {bg}; border-bottom: block yellow; }}
    #title {{ padding: 0 1; width: 1fr; background: yellow; color: #4b0082; text-style: bold; }}
    .btn {{ padding: 0 1; background: yellow; color: #4b0082; }}
    .btn:hover {{ text-style: reverse; }}
    #windows {{ height: 1fr; overflow-y: auto; background: {bg}; align: left middle; }}
    .window {{ padding: 0 1; height: 2; width: 100%; {normal} }}
    .window .dim {{ color: {fg}; text-style: dim; }}
    .window:hover {{ background: {fg}; color: {bg}; }}
    .bell {{ {bell} }}
    .bell:hover {{ {bell} text-style: bold reverse; }}
    .active {{ {current} }}
    .active:hover {{ {current} }}
    """

    def compose(self) -> ComposeResult:
        if self.confirming:
            banner = [Label("close?", id="title"), Button("(Y)", self, "exit"),
                      Button("(N)", self, "cancel_close")]
        else:
            banner = [Label("[b]tmux-tabs by gus pozuelo[/b]", id="title", markup=True),
                      Button("(X)", self, "ask_close")]
        yield Horizontal(*banner, id="banner")
        yield Vertical(*[WindowItem(w, self) for w in self.windows], id="windows")

    def ask_close(self):
        self.confirming = True

    def cancel_close(self):
        self.confirming = False

    def on_mount(self):
        self._pane_id = os.environ["TMUX_PANE"]
        self._session = pane_var(self._pane_id, "#{session_id}")
        self._current_window = pane_var(self._pane_id, "#{window_index}")
        tmux("select-pane", "-t", self._pane_id, "-T", "(tmux-tabs)")
        self._setup_fifo()
        # Events (hooks below) drive updates; the poll is just a slow safety net, so keep it light.
        self.set_interval(3.0, self.refresh_windows)
        self.refresh_windows()  # initial dump + provision placeholders for all other windows

    def on_unmount(self):
        # Graceful quit (q / [Y]): we're the only instance, so tear down what we set. Remove our
        # hooks — else they keep running run-shell on every window switch for a dead sidebar — and
        # reap our placeholders. (A crash skips this; the next launch resets hooks and reaps strays.)
        tmux("set-hook", "-gu", "after-select-window")
        tmux("set-hook", "-gu", "after-new-window")
        f = tmux("list-panes", "-s", "-t", self._session, "-F", "#{pane_id}\n#{pane_title}").splitlines()
        for pid, title in zip(f[::2], f[1::2]):
            if title == "(tmux-tabs)" and pid != self._pane_id:
                tmux("kill-pane", "-t", pid)

    def _setup_fifo(self):
        # No teardown on quit: the hooks are global and shared, so a dying instance must not remove
        # them (it would break a relaunched one). We deliberately leave the FIFO in /tmp — re-created
        # fresh on every launch below, and the OS clears /tmp in due time. See: never add on_unmount.
        # session_id is like "$1"; strip the $ so it can't be shell-expanded inside the hook
        session_id = pane_var(self._pane_id, "#{session_id}").lstrip("$")
        self._fifo_path = f"/tmp/tmux-tabs-{session_id}"
        if os.path.exists(self._fifo_path):
            os.unlink(self._fifo_path)
        os.mkfifo(self._fifo_path)
        # Poke the FIFO so the reader re-dumps on user-driven window changes. Only window-level hooks
        # that our OWN pane ops can't trigger — pane-level hooks would recurse off our split/swap/
        # kill. Other changes (title/bell/resize) are caught by the poll. `echo 1<>fifo` (read-write
        # redirect) so the write never blocks when no reader is attached — the hook is global and
        # outlives us, so a stale FIFO must not hang.
        hook = f"run-shell -b 'echo 1<>{self._fifo_path}'"
        for event in ("after-select-window", "after-new-window"):
            tmux("set-hook", "-g", event, hook)
        # O_RDWR (not O_RDONLY): keep a write-end open ourselves so the FIFO never reaches EOF when
        # the hook's writer exits — an EOF read-end makes add_reader fire nonstop (a busy-loop).
        self._fifo_fd = os.open(self._fifo_path, os.O_RDWR | os.O_NONBLOCK)
        asyncio.get_event_loop().add_reader(self._fifo_fd, self._on_event)

    def _on_event(self):
        # A watched hook fired; drain the FIFO and re-dump. (switch_to follows the active window.)
        os.read(self._fifo_fd, 4096)
        self.refresh_windows()

    def on_app_focus(self):
        # The sidebar never holds focus (buttons react on mouse-down regardless). A refresh re-reads
        # state and flees if we ended up alone; otherwise bounce focus back to our content pane.
        self.refresh_windows()
        if self._selected_pane and self._current_window in {w["index"] for w in self.windows}:
            tmux("select-pane", "-t", self._selected_pane)

    def refresh_windows(self):
        # ONE tmux read for the whole refresh: dump every pane, then derive everything in Python.
        # Reentrancy guard: our own select-window/swap inside switch_to refires hooks → _on_event →
        # back here; without this it recurses and piles up placeholders.
        if self._busy:
            return
        self._busy = True
        try:
            panes = tmux_panes(self._session)
            self.windows = self._windows_from(panes)
            mine = next((p for p in panes if p["pane"] == self._pane_id), None)
            if not mine:
                return
            # Follow OUR session's active window, taken from the dump (a bare display-message has no
            # client context and returns some other session's window → endless switch_to spin).
            active = self.find_window("active")
            if active and active != self._current_window:
                self.switch_to(active)
                return  # switch_to changed the layout; next event/poll re-dumps cleanly
            # Full window width means our content pane was closed and we're alone — flee.
            if mine["width"] == mine["win_width"]:
                if target := self.find_window("last") or self.find_window("active", False):
                    self.switch_to(target)
                return
            self._sync_placeholders(panes, max(10, min(mine["width"], mine["win_width"] // 2)), mine["win"])
        finally:
            self._busy = False

    def find_window(self, key, value=True):
        """Index of the first window whose `key` flag equals `value`, or None."""
        return next((w["index"] for w in self.windows if w[key] == value), None)

    @staticmethod
    def _windows_from(panes):
        # Aggregate the flat pane dump into one row per window. Title is the active CONTENT pane's
        # (not our "(tmux-tabs)" sidebar/placeholder); panes is the count, used to detect orphans.
        windows = {}
        for p in panes:
            w = windows.setdefault(p["win"], {"id": p["win"], "index": p["index"], "name": p["name"],
                "bell": p["bell"], "active": p["active"], "last": p["last"], "panes": 0, "title": ""})
            w["panes"] += 1
            if p["pane_active"] and p["title"] != "(tmux-tabs)":
                w["title"] = p["title"]
        return list(windows.values())

    def _sync_placeholders(self, panes, width, current):
        # Every window except `current` (the one holding the sidebar) gets a thin placeholder in the
        # sidebar's slot, so content panes keep a constant width whether the sidebar or a placeholder
        # sits beside them. Existing placeholders are read straight from the dump (a "(tmux-tabs)"
        # pane that isn't us) — no hand-maintained dict to drift after swaps.
        existing = {p["win"]: p["pane"] for p in panes
                    if p["title"] == "(tmux-tabs)" and p["pane"] != self._pane_id}
        resize = width != self._width
        self._width = width
        for w in self.windows:
            if w["id"] == current:
                continue
            ph = existing.get(w["id"])
            if ph and w["panes"] == 1:
                # Reap: our placeholder is the SOLE pane (content all gone) — let the window close.
                tmux("kill-pane", "-t", ph)
            elif ph:
                if resize:
                    tmux("resize-pane", "-t", ph, "-x", str(width))
            else:
                self._make_placeholder(w["index"], str(width))

    def _sidebar_width(self):
        # Our current width, clamped to [10, half the window].
        win = int(pane_var(self._pane_id, "#{window_width}"))
        return max(10, min(int(pane_var(self._pane_id, "#{pane_width}")), win // 2))

    def _make_placeholder(self, win_index, width):
        # A thin left pane running bare `cat` (minimal no-op process). Titled "(tmux-tabs)" like the
        # sidebar itself, so a fresh bare launch reaps any leftover sidebar+placeholders in one sweep
        # before splitting. The dict is the source of truth for which pane is which during a run.
        content = tmux("list-panes", "-t", win_index, "-f", "#{pane_active}", "-F", "#{pane_id}")
        # -f: span the full window height on the left edge, so a multi-pane window still gets the
        # placeholder as a single leftmost column (not split off just one content pane).
        ph = tmux("split-window", "-t", content, "-fhb", "-l", width, "-d", "-P", "-F", "#{pane_id}", "cat")
        tmux("select-pane", "-t", ph, "-T", "(tmux-tabs)")
        return ph

    def switch_to(self, win_index):
        # Resolve target window's active pane (window-number target is ambiguous from client context)
        target_pane = tmux("list-panes", "-t", win_index, "-f", "#{pane_active}", "-F", "#{pane_id}")
        # The content pane to bounce focus to; None if the only pane there is us.
        self._selected_pane = target_pane if target_pane != self._pane_id else None
        if win_index == self._current_window:
            if self._selected_pane:  # already here; just drop focus onto the content pane
                tmux("select-pane", "-t", self._selected_pane)
            return
        # Find the target window's placeholder (a "(tmux-tabs)" pane) to swap with, or make one.
        # No dict to maintain — the placeholder is identified by title straight from tmux.
        tgt = tmux("list-panes", "-t", win_index, "-F", "#{pane_id}\n#{pane_title}").splitlines()
        placeholder = next((tgt[i] for i in range(0, len(tgt), 2) if tgt[i + 1] == "(tmux-tabs)"), None) \
            or self._make_placeholder(win_index, str(self._sidebar_width()))
        # Swap the sidebar with that placeholder: both windows keep their slot and width, so no
        # content reflows. -d so the displaced placeholder doesn't become active in the window we
        # leave (its "(tmux-tabs)" title would otherwise show as that window's title in the list).
        tmux("swap-pane", "-d", "-s", self._pane_id, "-t", placeholder)
        # The placeholder now sits where the sidebar was in the window we left; activate that
        # window's content pane so its title (not "(tmux-tabs)") shows in the list.
        old_panes = tmux("list-panes", "-t", self._current_window, "-F", "#{pane_id}").splitlines()
        content = next((p for p in old_panes if p != placeholder), None)
        if content:
            tmux("select-pane", "-t", content)
        tmux("select-window", "-t", win_index)
        if self._selected_pane:
            tmux("select-pane", "-t", self._selected_pane)
        # Repaint the highlight in-memory (no tmux re-query): old window loses active, new gains it.
        self.windows = [{**w, "active": w["index"] == win_index, "last": w["index"] == self._current_window}
                        for w in self.windows]
        self._current_window = win_index


def main():
    """Console entry point. Bare invocation splits off the sidebar; --sidebar runs the TUI."""
    if "--sidebar" in sys.argv:
        os.environ.setdefault("TEXTUAL_THEME", "")
        TmuxTabs.CSS = TmuxTabs.build_css()
        TmuxTabs().run(inline=False)
        return
    # Reap leftover (tmux-tabs) panes from a dead instance in THIS session (-s, the calling
    # client's session) BEFORE splitting, so the new sidebar isn't placed beside a stray.
    for line in tmux("list-panes", "-s", "-F", "#{pane_id}|#{pane_title}").splitlines():
        pid, _, title = line.partition("|")
        if title == "(tmux-tabs)":
            tmux("kill-pane", "-t", pid)
    # Re-invoke ourselves (the installed console script, sys.argv[0]) for the sidebar pane.
    tmux("split-window", "-fhb", "-l", "25%", f"{sys.argv[0]} --sidebar")


if __name__ == "__main__":
    main()
