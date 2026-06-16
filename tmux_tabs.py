#!/usr/bin/env python3.12
"""
tmux-tabs — vertical window sidebar for tmux.

Shows all tmux windows in a narrow left pane with bell highlights and terminal
titles. Click any window to switch to it. Automatically follows focus when a
new window is opened (e.g. via F2 in byobu).

Usage:
  Run `tmux-tabs` in any tmux pane — it splits off a 25%-wide left sidebar and
  hands the original pane back as the content pane.

  Quit: click the (X) button, then (Y) to confirm.

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
from textual.widget import Widget
from textual.widgets import Label


# Pin the sidebar width via the main-vertical layout (tmux >= 3.6). Flipped off the
# first time select-layout main-vertical fails, after which we fall back to resize-pane.
USE_MAIN_VERTICAL = True
_APP_CLOSE = object()  # sentinel: confirming_close == _APP_CLOSE means close the app, not a tab


def tmux(*args, check=False):
    # Print the failing command + stderr (the client otherwise only shows a bare
    # "server exited unexpectedly"); check=True re-raises after printing.
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"tmux {' '.join(args)}: rc={e.returncode} stdout={e.stdout.strip()!r} "
              f"stderr={e.stderr.strip()!r}", file=sys.stderr, flush=True)
        if check:
            raise
        return ""


def pane_var(pane, fmt):
    return tmux("display-message", "-p", "-t", pane, fmt)


PANE_FIELDS = 11  # fields per pane in the dump below; free-text name/title kept last (newline-safe)


def tmux_panes(session):
    # ONE call dumping every pane of every window IN OUR SESSION (-s, not -a server-wide, which
    # would manage other sessions' windows) — the sole tmux read per refresh. Newline per field
    # because window_name/pane_title are free text that can hold any delimiter (tmux collapses
    # newlines in them). Fixed groups; name and title come last.
    f = tmux("list-panes", "-s", "-t", session, "-F", "#{window_id}\n#{window_index}\n#{window_flags}\n"
             "#{window_bell_flag}\n#{pane_id}\n#{pane_active}\n#{pane_dead}\n#{pane_width}\n#{window_width}\n"
             "#{window_name}\n#{pane_title}").splitlines()
    return [
        {"win": wid, "index": idx, "active": "*" in flags, "last": "-" in flags,
         "bell": bell == "1", "pane": pid, "pane_active": pa == "1", "pane_dead": dead == "1",
         "width": int(width), "win_width": int(ww), "name": name, "title": title}
        for wid, idx, flags, bell, pid, pa, dead, width, ww, name, title in zip(*[iter(f)] * PANE_FIELDS)
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


class Button(Label):
    def __init__(self, label, app_ref, action, *args, classes="btn"):
        self._app_ref = app_ref
        self._action = action
        self._args = args
        # markup=False: "[X]"/"[Y]"/"[N]" must render literally, not be parsed as Rich tags.
        super().__init__(label, classes=classes, markup=False)

    def on_mouse_down(self, event):
        # Stop the press bubbling to the enclosing WindowItem, whose own on_mouse_down would otherwise
        # switch to the window when the user only meant to click this button.
        event.stop()
        getattr(self._app_ref, self._action)(*self._args)

    def on_mouse_up(self, event):
        # Also stop the release: WindowItem reacts on mouse-up too, so without this a button click
        # would still bubble up and switch to the window.
        event.stop()


class WindowItem(Horizontal):
    # A window row: a clickable text label that switches to the window, plus an (X) close button that
    # appears on hover and hides 1s after the mouse leaves the row (Textual's :hover can stick when the
    # mouse jumps to another tmux pane, so we drive visibility from on_enter + a self-rescheduling timer).
    def __init__(self, win, app_ref):
        self.win_index = win["index"]
        self._app_ref = app_ref
        icon = {"dead": "☠️", "active": "▶", "bell": "🔔"}.get(win["status"], " ")
        title = win["title"].lstrip("✳").strip()
        self._label = (f"{icon} [b]{win['name']}[/b]\n    [dim]{title}[/dim]" if title
                       else f"{icon} [b]{win['name']}[/b]")
        super().__init__(classes=f"window {win['status']}".strip())

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="window-text", markup=True)
        yield Button("(X)", self._app_ref, "ask_close_window", self.win_index, classes="row-x")

    def on_enter(self):
        x = self.query_one(".row-x")
        x.display = True
        def hide_when_mouse_gone():
            if not self.is_attached:
                return
            if self.region.contains(*self.app.mouse_position):
                self.set_timer(1.0, hide_when_mouse_gone)  # still hovered — check again
            else:
                x.display = False
        self.set_timer(1.0, hide_when_mouse_gone)

    def on_mouse_down(self):
        self._app_ref.switch_to(self.win_index)

    def on_mouse_up(self):
        # React on BOTH press and release: when our pane is unfocused, tmux spends one of the two on
        # the focus-grab, and which one it eats varies by tmux version. switch_to to the already-current
        # window is a cheap no-op, so the surviving event drives the switch and the other does nothing.
        self._app_ref.switch_to(self.win_index)


class ModalPrompt(Widget):
    # Floating overlay: a prompt phrase above a row of labeled buttons, mounted over the widget it
    # confirms. callback(response) gets the clicked button's bare label (e.g. "Y"); an outside click
    # is handled by the app, not here.
    DEFAULT_CSS = """
    ModalPrompt {
        layer: overlay;
        border: solid yellow;
        background: #4b0082;
        color: yellow;
        text-style: bold;
        height: 2;
        width: 100%;
        layout: vertical;
        align: center middle;
    }
    ModalPrompt Label { width: 100%; text-align: center; }
    ModalPrompt Horizontal { width: 100%; height: 1; align: center middle; }
    ModalPrompt .btn { padding: 0 1; width: auto; background: yellow; color: #4b0082; text-style: bold; }
    ModalPrompt .btn:hover { text-style: reverse; }
    """

    def __init__(self, phrase, callback, buttons="Y,N"):
        self._phrase = phrase
        self._callback = callback
        self._buttons = [b.strip() for b in buttons.split(",")]
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label(self._phrase)
        yield Horizontal(*[Button(f"({btn})", self, "_respond", btn) for btn in self._buttons])

    def _respond(self, label):
        self._callback(label)


class TmuxTabs(App):
    DEFAULT_CSS = ""
    windows = reactive({}, recompose=True)  # {window_id: window dict}, see _windows_from
    # Confirm target: _APP_CLOSE to close the app, a window index to close a tab, None = no modal.
    confirming_close = reactive(None)
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
    Screen {{ background: {bg}; color: {fg}; layers: base overlay; border-bottom: hkey black; }}
    #banner {{ height: auto; width: 100%; background: {bg}; border-bottom: block yellow; }}
    #title {{ padding: 0 1; width: 1fr; background: yellow; color: #4b0082; text-style: bold; }}
    .btn {{ padding: 0 1; background: yellow; color: #4b0082; }}
    .btn:hover {{ text-style: reverse; }}
    #windows {{ height: 1fr; overflow-y: auto; background: {bg}; align: left middle; }}
    .window {{ height: 2; width: 100%; layout: horizontal; {normal} }}
    .window-text {{ padding: 0 1; width: 1fr; height: 100%; }}
    .window .dim {{ color: {fg}; text-style: dim; }}
    .window:hover {{ background: {fg}; color: {bg}; }}
    .row-x {{ display: none; }}
    .bell {{ {bell} }}
    .bell:hover {{ {bell} text-style: bold reverse; }}
    .active {{ {current} }}
    .active:hover {{ {current} }}
    .dead {{ background: grey; color: {fg}; }}
    .dead .dim {{ color: {fg}; }}
    """

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Label("[b]tmux-tabs by gus pozuelo[/b]", id="title", markup=True),
            Button("(X)", self, "ask_close"),
            id="banner",
        )
        yield Vertical(*[WindowItem(w, self) for w in self.windows.values()], id="windows")

    async def watch_confirming_close(self, target):
        # Await the removal (remove() is async): the covered row only repaints once the overlay is
        # actually gone, so without the await its title flashes blank on the next frame.
        await self.query(ModalPrompt).remove()
        if target is None:
            return
        if target is _APP_CLOSE:
            self.query_one("#banner").mount(ModalPrompt(
                "close app?", lambda r: self.exit() if r == "Y" else self.cancel()))
        else:
            win = self.windows[target]
            anchor = next(w for w in self.query(WindowItem) if w.win_index == target)
            anchor.mount(ModalPrompt(f"close {win['name']}?",
                lambda r: self.close_window(target) if r == "Y" else self.cancel()))

    def cancel(self):
        self.confirming_close = None

    def on_mouse_down(self, event):
        # Click anywhere outside the modal dismisses it.
        if (ms := self.query(ModalPrompt)) and not ms.first().region.contains(event.x, event.y):
            self.cancel()

    def ask_close(self):
        self.confirming_close = _APP_CLOSE

    def ask_close_window(self, win_index):
        self.confirming_close = win_index

    def close_window(self, win_index):
        # Kill every CONTENT pane of the target window (a "(tmux-tabs)" title marks our sidebar AND our
        # placeholders, so both are spared) — we can't rely on pane_active to pick one, since focus is
        # on the sidebar when (X) is clicked. Once the window holds only our placeholder, the next
        # refresh reaps it → the window closes. We can't kill-window: that would also kill the sidebar
        # when closing the window we're in. Clearing confirming_close dismisses the modal.
        self.confirming_close = None
        for p in self.windows[win_index]["panes"]:
            if p["title"] != "(tmux-tabs)":
                tmux("kill-pane", "-t", p["pane"])
        self.refresh_windows()

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
        # Graceful quit (the [Y] confirm button): we're the only instance, so tear down what we set. Remove our
        # hooks — else they keep running run-shell on every window switch for a dead sidebar — and
        # reap our placeholders. (A crash skips this; the next launch resets hooks and reaps strays.)
        for event in ("after-select-window", "after-new-window", "pane-focus-in"):
            tmux("set-hook", "-gu", event)
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
        # Poke the FIFO so the reader re-dumps on user-driven changes. pane-focus-in catches focus
        # moves WITHIN a window (between content panes, or onto a native bar-clicked window) that
        # after-select-window misses; it also fires as an echo of our own swap/select, but that's
        # harmless now — refresh_windows is idempotent and filters the echo as "same window, no switch".
        # `echo 1<>fifo` (read-write redirect) so the write never blocks when no reader is attached —
        # the hook is global and outlives us, so a stale FIFO must not hang.
        hook = f"run-shell -b 'echo 1<>{self._fifo_path}'"
        for event in ("after-select-window", "after-new-window", "pane-focus-in"):
            tmux("set-hook", "-g", event, hook)
        # O_RDWR (not O_RDONLY): keep a write-end open ourselves so the FIFO never reaches EOF when
        # the hook's writer exits — an EOF read-end makes add_reader fire nonstop (a busy-loop).
        self._fifo_fd = os.open(self._fifo_path, os.O_RDWR | os.O_NONBLOCK)
        asyncio.get_event_loop().add_reader(self._fifo_fd, self._on_event)

    def _on_event(self):
        # A watched hook fired; drain the FIFO (one line per event) and re-dump.
        os.read(self._fifo_fd, 4096)
        self.refresh_windows()

    def on_app_focus(self):
        # Focus landed on the sidebar (the Textual app gained focus) — bounce it straight to our
        # content pane. The sidebar must never hold focus. One cheap select-pane, no dump: the storm
        # of focus events a native bar-click fires would otherwise each pay a full refresh.
        if self._selected_pane:
            tmux("select-pane", "-t", self._selected_pane)

    def refresh_windows(self):
        # ONE tmux read for the whole refresh: dump every pane, derive everything in Python, then act
        # on where focus actually landed. No re-entrancy guard needed: asyncio reader callbacks run to
        # completion one at a time, so our own select-window/swap echo arrives only AFTER this returns —
        # by which point _current_window is updated and the echo is filtered as "the window we're on".
        panes = tmux_panes(self._session)
        self.windows = self._windows_from(panes)
        mine = next((p for p in panes if p["pane"] == self._pane_id), None)
        if not mine:
            return
        # Follow the active window of our session (the dump's active flag; a bare display-message has
        # no client context and would return some other session's window).
        active_win = self.find_window("active")
        if active_win and active_win != self._current_window:
            self.switch_to(active_win)  # genuine switch to another window
            return  # switch_to changed the layout; next event/poll re-dumps cleanly
        # Same window, no switch. Track our window's active content pane as the bounce target, so a
        # focus change BETWEEN content panes (which never calls switch_to) keeps _selected_pane fresh
        # for on_app_focus. Title-following is already handled by the recompose above.
        self._selected_pane = next((p["pane"] for p in panes if p["win"] == mine["win"]
                                    and p["pane_active"] and p["pane"] != self._pane_id), None)
        # Full window width means our content pane was closed and we're alone — flee.
        if mine["width"] == mine["win_width"]:
            if target := self.find_window("last") or self.find_window("active", False):
                self.switch_to(target)
            return
        self._sync_placeholders(max(10, min(mine["width"], mine["win_width"] // 2)), mine["index"])

    def find_window(self, key, value=True):
        return next((i for i, w in self.windows.items() if w[key] == value), None)

    @staticmethod
    def _windows_from(panes):
        # Aggregate the flat pane dump into one dict per window, keyed by tmux window index (the tab
        # number tmux commands target). Each entry carries a "panes" list of its raw pane dicts; the
        # placeholder is the "(tmux-tabs)" pane within it. Title is the active CONTENT pane's;
        # content_panes counts live non-placeholder panes — zero means dead. len(panes) lets the
        # reaper distinguish a lone placeholder (reap) from a placeholder beside a dead zombie (keep, ☠️).
        windows = {}
        for p in panes:
            if (w := windows.get(p["index"])) is None:
                w = windows[p["index"]] = {"index": p["index"], "name": p["name"],
                    "bell": p["bell"], "active": p["active"], "last": p["last"],
                    "content_panes": 0, "title": "", "panes": []}
            w["panes"].append(p)
            if p["title"] != "(tmux-tabs)":
                if not p["pane_dead"]:
                    w["content_panes"] += 1
                if p["pane_active"]:
                    w["title"] = p["title"]
        for w in windows.values():
            if not w["content_panes"]:
                w["status"] = "dead"
            elif w["active"]:
                w["status"] = "active"
            elif w["bell"]:
                w["status"] = "bell"
            else:
                w["status"] = ""
        return windows

    def _sync_placeholders(self, width, current):
        # Every window except `current` (the one holding the sidebar) gets a thin placeholder in the
        # sidebar's slot, so content panes keep a constant width whether the sidebar or a placeholder
        # sits beside them. Each window's placeholder (if any) is the "(tmux-tabs)" pane in its own
        # pane list — no hand-maintained dict to drift after swaps.
        resize = width != self._width
        self._width = width
        # On a width change, settle the sidebar's own window first; main-pane-width is global so set
        # it once here (see _pin_pane for the per-window layout and the resize-pane fallback).
        if resize and USE_MAIN_VERTICAL:
            tmux("set-window-option", "-g", "main-pane-width", str(width))
            self._pin_pane(current, self._pane_id, width)
        for index, w in self.windows.items():
            if index == current:
                continue
            ph = next((p for p in w["panes"] if p["title"] == "(tmux-tabs)" and p["pane"] != self._pane_id), None)
            if ph and len(w["panes"]) == 1:
                # Reap: our placeholder is the SOLE pane (content all gone) — let the window close.
                # A dead zombie pane still counts, so those windows survive to show ☠️.
                tmux("kill-pane", "-t", ph["pane"])
            elif not ph:
                self._make_placeholder(index, str(width))
            elif width != ph["width"]:
                # Re-pin any placeholder whose actual width drifted from target — not just on global
                # resize. Placeholders made during switch_to (or before main-pane-width settled) can
                # be stuck at a stale width; the dump already carries each one's width, so this is free.
                self._pin_pane(index, ph["pane"], width)

    def _pin_pane(self, win_index, pane, width):
        # Pin the sidebar to a fixed width: main-vertical (sidebar = left main pane, content stacks
        # right) holds it against tmux's proportional float on terminal resize. If main-vertical
        # fails (older tmux), disable it for the rest of the run and resize the pane directly.
        global USE_MAIN_VERTICAL
        if USE_MAIN_VERTICAL:
            try:
                tmux("select-layout", "-t", win_index, "main-vertical", check=True)
                return
            except subprocess.CalledProcessError:
                USE_MAIN_VERTICAL = False
        tmux("resize-pane", "-t", pane, "-x", str(width))

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
        self.windows = {i: {**w, "active": i == win_index, "last": i == self._current_window}
                        for i, w in self.windows.items()}
        self._current_window = win_index


def main():
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
