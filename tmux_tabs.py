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
import faulthandler
import glob
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from rich.markup import escape
from textual.app import App
from textual.color import Color, ColorParseError
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label


_APP_CLOSE = object()  # sentinel: confirming_close == _APP_CLOSE means close the app, not a tab

HISTORY_DIR = Path.home() / ".cache" / "tmux-tabs"
TRACEBACK_LOG = HISTORY_DIR / "traceback.log"  # where SIGUSR1 dumps stacks; see _enable_traceback_dump


def tmux(*args, check=False):
    # Print the failing command + stderr (the client otherwise only shows a bare
    # "server exited unexpectedly"); check=True re-raises after printing.
    try:
        return subprocess.run(["tmux", *args],
                              capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"tmux {' '.join(args)}: rc={e.returncode} stdout={e.stdout.strip()!r} "
              f"stderr={e.stderr.strip()!r}", file=sys.stderr, flush=True)
        if check:
            raise
        return ""


def pane_var(pane, fmt):
    return tmux("display-message", "-p", "-t", pane, fmt)


PANE_FIELDS = 13  # fields per pane in the dump below; free-text name/title kept last (newline-safe)


def tmux_panes(session):
    # ONE call dumping every pane of every window IN OUR SESSION (-s, not -a server-wide, which
    # would manage other sessions' windows) — the sole tmux read per refresh. Newline per field
    # because window_name/pane_title are free text that can hold any delimiter (tmux collapses
    # newlines in them). Fixed groups; name and title come last.
    f = tmux("list-panes", "-s", "-t", session, "-F", "#{window_id}\n#{window_index}\n#{window_flags}\n"
             "#{window_bell_flag}\n#{pane_id}\n#{pane_active}\n#{pane_dead}\n#{pane_width}\n#{window_width}\n"
             "#{pane_current_command}\n#{pane_current_path}\n"
             "#{window_name}\n#{pane_title}").splitlines()
    return [
        {"win": wid, "index": idx, "active": "*" in flags, "last": "-" in flags,
         "bell": bell == "1", "pane": pid, "pane_active": pa == "1", "pane_dead": dead == "1",
         "width": int(width), "win_width": int(ww), "command": command, "cwd": cwd,
         "name": name, "title": title}
        for wid, idx, flags, bell, pid, pa, dead, width, ww, command, cwd, name, title
        in zip(*[iter(f)] * PANE_FIELDS)
    ]


def claude_session_for(cwd, title):
    # Claude Code names each project dir after its cwd with "/" -> "-", and writes a {"type":
    # "ai-title", "aiTitle": ..., "sessionId": ...} record into a session's own file whenever it sets
    # the pane title — so the pane's live title (minus the leading bell marker) uniquely identifies
    # which session file is actually running in this pane, no recency guessing needed.
    project_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
    needle = f'"aiTitle":{json.dumps(title.lstrip("✳").strip())}'
    matches = [p for p in glob.glob(str(project_dir / "*.jsonl")) if needle in Path(p).read_text()]
    if not matches:
        return None
    return Path(max(matches, key=os.path.getmtime)).stem


def write_pane_history(pane_id, name, title, cwd, command):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = [name, title, cwd, command]
    if command == "claude" and (session := claude_session_for(cwd, title)):
        lines.append(session)
    (HISTORY_DIR / f"tmux-tab-{pane_id.lstrip('%')}").write_text("\n".join(lines) + "\n")


def parse_style(style):
    return {k: v or True for k, _, v in (p.strip().partition("=") for p in style.split(","))}


def tmux_styles():
    keys = {"normal": "window-status-style", "current": "window-status-current-style",
            "bell": "window-status-bell-style", "status": "status-style"}
    return {name: parse_style(tmux("show-options", "-gv", key)) for name, key in keys.items()}


def style_to_css(s, base):
    bg = s.get("bg", base.get("bg", "default"))
    fg = s.get("fg", base.get("fg", "default"))
    if s.get("reverse"):
        bg, fg = fg, bg
    bold = "\n        text-style: bold;" if s.get("bold") else ""
    return f"background: {bg};\n        color: {fg};{bold}", bg


def hover_nudge(color, amount=0.2):
    # A visibly shifted variant of a row's own background for :hover. Nudge AWAY from the row's
    # brightness rather than always darkening: the current-window style is usually reverse, making its
    # effective background black, and darkening black leaves it black.
    try:
        col = Color.parse(color)
    except ColorParseError:
        return color  # tmux's "default" (and colour4/brightblack) have no RGB to shift
    return (col.lighten(amount) if col.brightness < 0.5 else col.darken(amount)).hex


class Button(Label):
    def __init__(self, label, callback, *args, classes="btn"):
        self._callback = callback
        self._args = args
        # markup=False: "[X]"/"[Y]"/"[N]" must render literally, not be parsed as Rich tags.
        super().__init__(label, classes=classes, markup=False)

    def on_click(self, event):
        # Click, not mouse down, to match WindowItem: App synthesizes Click from the pressed widget
        # regardless of whether MouseDown was stopped, so stopping the press would no longer keep the
        # enclosing row from also switching windows. Stopping the Click itself does.
        event.stop()
        self._callback(*self._args)


CLEAR_TAG = "❌"  # last choice in the tag menu: removes the window's tag instead of setting one
EMOJI_CHOICES = ["🚩", "⏳", "📌", "⭐", "🧊", CLEAR_TAG]  # urgent, waiting, pinned, important, stale


class WindowItem(Horizontal):
    # A window row: a clickable text label that switches to the window, plus an (X) close button that
    # appears on hover and hides 1s after the mouse leaves the row (Textual's :hover can stick when the
    # mouse jumps to another tmux pane, so we drive visibility from on_enter + a self-rescheduling timer).
    def __init__(self, win):
        self.win_index = win["index"]
        # The trailing "!" is an external convention marking a hand-set name that scripts must not
        # auto-rename over; it is bookkeeping, so strip it from what we show.
        name = escape(win["name"].rstrip("!"))
        title = escape(win["title"].lstrip("✳").strip())
        self._label = f"[b]{name}[/b]\n    [dim]{title}[/dim]" if title else f"[b]{name}[/b]"
        marker = {"dead": "☠️", "bell": "🔔"}.get(win["status"], " ")
        self._icon_col = f"{marker}\n{win.get('emoji') or ' '}"  # status marker, user's tag below it
        super().__init__(classes=f"window {win['status']}".strip())

    def compose(self):
        yield Label(self._icon_col, classes="window-icon", markup=False)
        yield Label(self._label, classes="window-text", markup=True)
        yield Button("(X)", self.app.ask_close_window, self.win_index, classes="row-x")
        # Always rendered on both of the row's lines, pointing at the content pane to our right; CSS
        # reveals it only on the .active row, so switching is a class toggle and never a re-render.
        yield Label("▶\n▶", classes="window-arrow", markup=False)

    def on_enter(self):
        try:
            x = self.query_one(".row-x")
        except NoMatches:
            return
        x.display = True
        def hide_when_mouse_gone():
            if not self.region:
                return
            if self.region.contains(*self.app.mouse_position):
                self.set_timer(1.0, hide_when_mouse_gone)  # still hovered — check again
            else:
                x.display = False
        self.set_timer(1.0, hide_when_mouse_gone)

    def on_click(self, event):
        # Click, not mouse down/up: Textual raises it only for a press and release on the SAME widget,
        # which is the click-vs-drag distinction we'd otherwise hand-roll. It follows that switch_to
        # must not recompose these rows — a rebuilt row is a different object, and the identity check
        # in App's Click synthesis would then never match. See switch_to.
        if event.button == 3:
            self.app.ask_tag_window(self.win_index)
        else:
            self.app.switch_to(self.win_index)


class ModalPrompt(Widget):
    # Floating overlay: a prompt phrase above a row of labeled buttons, mounted over the widget it
    # confirms. callback(response) gets the clicked button's bare label (e.g. "Y"); an outside click
    # is handled by the app, not here.
    DEFAULT_CSS = """
    ModalPrompt {
        layer: overlay;
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

    def compose(self):
        yield Label(self._phrase)
        yield Horizontal(*[Button(f"({btn})", lambda label=btn: self._callback(label)) for btn in self._buttons])


class TmuxTabs(App):
    DEFAULT_CSS = ""
    windows = reactive({}, recompose=True)  # {window_index: window dict}, see _windows_from
    # Confirm target: _APP_CLOSE to close the app, a window index to close a tab, None = no modal.
    confirming_close = reactive(None)
    # Global tmux hooks that poke our FIFO to trigger a refresh. client-resized fires the instant the
    # terminal is resized so the sidebar re-pins to its fixed char width immediately, not on the slow
    # poll (during which tmux's proportional float leaves the sidebar and placeholders visibly shrunk).
    HOOKS = ("after-select-window", "after-new-window", "pane-focus-in", "client-resized")
    _session = None         # our session id; placeholder management is scoped to it
    _pane_id = None
    _current_window = None  # window index the sidebar currently sits in
    _selected_pane = None   # content pane beside us; bounce focus here, None when none exists
    _width = None           # the user's chosen fixed sidebar char count; only changes on a manual drag
    _term_width = None      # last seen total window width; single-tab tiebreak for resize-vs-drag
    _pinned = None           # the clamped effective width we last pinned to; skip re-pin when unchanged
    _fifo_fd = None
    _history_seen = None    # {pane_id: (name, title)} last written to history — skip rewrite when unchanged
    _saved_scroll = None     # #windows scroll_y carried across a recompose, see recompose()
    _bell_latch = None       # {window_id: True} bell seen since last viewed — survives tmux's index-based
                             # window_bell_flag losing track of the window across swap-window/move-window
    _emoji = None            # {window_id: emoji} user-assigned tag, right-click menu; in-memory only

    @classmethod
    def build_css(cls):
        s = tmux_styles()
        base = s["status"]
        bg = s["normal"].get("bg", base.get("bg", "#94cdba"))
        fg = s["normal"].get("fg", base.get("fg", "black"))
        (normal, normal_bg), (bell, bell_bg), (current, current_bg) = (
            style_to_css(s[k], base) for k in ("normal", "bell", "current"))
        normal_hover, bell_hover, current_hover = (hover_nudge(b) for b in (normal_bg, bell_bg, current_bg))
        return f"""
    * {{ scrollbar-background: {bg}; scrollbar-color: {fg}; scrollbar-size: 0 0; }}
    Screen {{ background: {bg}; color: {fg}; layers: base overlay; border-bottom: hkey black; }}
    #banner {{ height: auto; width: 100%; background: {bg}; border-bottom: block yellow; }}
    #title {{ padding: 0 1; width: 1fr; background: yellow; color: #4b0082; text-style: bold; }}
    #footer {{ height: auto; width: 100%; background: {bg}; align: center middle; }}
    .btn {{ padding: 0 1; background: yellow; color: #4b0082; }}
    .btn:hover {{ text-style: reverse; }}
    #windows {{ height: 1fr; overflow-y: auto; background: {bg}; align: left middle;
                scrollbar-size-vertical: 1; scrollbar-background: {bg}; scrollbar-background-hover: {bg};
                scrollbar-background-active: {bg}; scrollbar-color: {fg} 60%; scrollbar-color-hover: {fg} 80%;
                scrollbar-color-active: {fg}; }}
    .window {{ height: 2; width: 100%; layout: horizontal; {normal} }}
    .window-icon {{ width: 2; height: 100%; text-align: center; }}
    .window-text {{ padding: 0 1; width: 1fr; height: 100%; }}
    .window-arrow {{ width: 1; height: 100%; visibility: hidden; }}
    .active .window-arrow {{ visibility: visible; }}
    .window .dim {{ color: {fg}; text-style: dim; }}
    .window:hover {{ background: {normal_hover}; }}
    .row-x {{ display: none; }}
    .bell {{ {bell} }}
    .bell:hover {{ {bell} background: {bell_hover}; }}
    .active {{ {current} }}
    .active:hover {{ {current} background: {current_hover}; }}
    .dead {{ background: grey; color: {fg}; }}
    .dead .dim {{ color: {fg}; }}
    """

    def compose(self):
        yield Horizontal(
            Label("[b]tmux-tabs by gus pozuelo[/b]", id="title", markup=True),
            Button("(X)", self.ask_close),
            id="banner",
        )
        yield Vertical(*[WindowItem(w) for w in self.windows.values()], id="windows")
        yield Horizontal(Button("<history>", self.show_history), id="footer")

    async def recompose(self):
        # Recomposing remounts #windows, which resets its scroll to the top. Restore the position once
        # the new rows are laid out (setting it earlier gets clamped to an empty container's 0).
        # Recomposes can overlap (a switch fires several refreshes), and a later one would read the
        # already-reset 0, so keep the first position seen until a restore has actually run.
        if self._saved_scroll is None:
            self._saved_scroll = self.query_one("#windows").scroll_y
        await super().recompose()
        self.call_after_refresh(self._restore_scroll)

    def _restore_scroll(self):
        y, self._saved_scroll = self._saved_scroll, None
        self.query_one("#windows").scroll_to(y=y, animate=False, immediate=True)

    async def on_event(self, event):
        try:
            await super().on_event(event)
        except Exception as e:
            self.query_one("#banner").mount(ModalPrompt(
                f"error: {e}", self.cancel, buttons="OK"))
            self.refresh_windows()

    async def watch_confirming_close(self, target):
        # Await the removal (remove() is async): the covered row only repaints once the overlay is
        # actually gone, so without the await its title flashes blank on the next frame.
        await self.query(ModalPrompt).remove()
        if target is None:
            return
        if target is _APP_CLOSE:
            self.query_one("#banner").mount(ModalPrompt(
                "close app?", lambda r: self.exit() if r == "Y" else self.cancel()))
        elif (win := self.windows.get(target)) and (
                anchor := next((w for w in self.query(WindowItem) if w.win_index == target), None)):
            anchor.mount(ModalPrompt(f"close {win['name']}?",
                lambda r: self.close_window(target) if r == "Y" else self.cancel()))

    def cancel(self):
        self.confirming_close = None

    def on_mouse_down(self, event):
        # Click anywhere outside the modal dismisses it.
        if (ms := self.query(ModalPrompt)) and (r := ms.first().region) and not r.contains(event.x, event.y):
            self.cancel()

    def ask_close(self):
        self.confirming_close = _APP_CLOSE

    def show_history(self):
        # New window (not a pane here) so the fixed-width sidebar layout is untouched. -F "%T %p":
        # mtime-seconds then path, so `sort -rn` orders files newest-first without a subshell per file.
        cmd = (f"cd {HISTORY_DIR} && "
               "find . -maxdepth 1 -name 'tmux-tab-*' -printf '%T@ %p\\n' | sort -rn | cut -d' ' -f2- "
               "| xargs -I{} sh -c 'echo ==== {} ====; cat {}; echo' | less")
        tmux("new-window", "-t", self._session, "-n", "HISTORY", cmd)

    def ask_close_window(self, win_index):
        self.confirming_close = win_index

    def ask_tag_window(self, win_index):
        # Reuses the confirm overlay: same shape (a phrase over a row of buttons), so the tag menu is
        # one mount. Keyed by window_id, not index, so a tag follows its window across a tmux reorder.
        window_id = self.windows[win_index]["win"]
        def pick(emoji):
            if emoji == CLEAR_TAG:
                self._emoji.pop(window_id, None)
            else:
                self._emoji[window_id] = emoji
            self.refresh_windows()  # recomposes the rows, which takes the overlay with them
        anchor = next(w for w in self.query(WindowItem) if w.win_index == win_index)
        anchor.mount(ModalPrompt("tag window", pick, buttons=",".join(EMOJI_CHOICES)))

    def close_window(self, win_index):
        # Kill every CONTENT pane of the target window (a "(tmux-tabs)" title marks our sidebar AND our
        # placeholders, so both are spared) — we can't rely on pane_active to pick one, since focus is
        # on the sidebar when (X) is clicked. Once the window holds only our placeholder, the next
        # refresh reaps it → the window closes. We can't kill-window: that would also kill the sidebar
        # when closing the window we're in. Clearing confirming_close dismisses the modal.
        self.confirming_close = None
        if (win := self.windows.get(win_index)) is None:
            return
        for p in win["panes"]:
            if p["title"] != "(tmux-tabs)":
                tmux("kill-pane", "-t", p["pane"])
        self.refresh_windows()

    def on_mount(self):
        self._pane_id = os.environ["TMUX_PANE"]
        self._session = pane_var(self._pane_id, "#{session_id}")
        self._current_window = pane_var(self._pane_id, "#{window_index}")
        # TODO: fold these per-field pane_var calls (session_id, window_index, window width) into a
        # single tmux display-message dumping all fields at once, like tmux_panes does for the refresh.
        # Seed the fixed width to 32 chars (the width main() splits the sidebar off at — fits the
        # " tmux-tabs by gus pozuelo  (X) " banner), clamped to half the window so a narrow terminal
        # still leaves room for content, floored at 10.
        self._width = max(10, min(32, int(pane_var(self._pane_id, "#{window_width}")) // 2))
        tmux("select-pane", "-t", self._pane_id, "-T", "(tmux-tabs)")
        self._history_seen = {}
        self._bell_latch = {}
        self._emoji = {}
        self._setup_fifo()
        # Events (hooks below) drive updates; the poll is just a slow safety net, so keep it light.
        self.set_interval(3.0, self.refresh_windows)
        self.refresh_windows()  # initial dump + provision placeholders for all other windows

    def on_unmount(self):
        # Graceful quit (the [Y] confirm button): we're the only instance, so tear down what we set. Remove our
        # hooks — else they keep running run-shell on every window switch for a dead sidebar — and
        # reap our placeholders. (A crash skips this; the next launch resets hooks and reaps strays.)
        for event in self.HOOKS:
            tmux("set-hook", "-gu", event)
        f = tmux("list-panes", "-s", "-t", self._session, "-F", "#{pane_id}\n#{pane_title}").splitlines()
        for pid, title in zip(f[::2], f[1::2]):
            if title == "(tmux-tabs)" and pid != self._pane_id:
                tmux("kill-pane", "-t", pid)

    def _setup_fifo(self):
        # One FIFO per tmux server, named after its socket ("default", "-L" name): a FIFO has a single
        # consumer, session ids repeat across servers, and a stable name leaves no orphans behind.
        fifo_path = f"/tmp/tmux-tabs-{os.path.basename(os.environ['TMUX'].split(',')[0])}"
        if os.path.exists(fifo_path):
            os.unlink(fifo_path)
        os.mkfifo(fifo_path)
        # Poke the FIFO so the reader re-dumps on user-driven changes. pane-focus-in catches focus
        # moves WITHIN a window (between content panes, or onto a native bar-clicked window) that
        # after-select-window misses; it also fires as an echo of our own swap/select, but that's
        # harmless now — refresh_windows is idempotent and filters the echo as "same window, no switch".
        # `echo 1<>fifo` (read-write redirect) so the write never blocks when no reader is attached —
        # the hook is global and outlives us, so a stale FIFO must not hang.
        hook = f"run-shell -b 'echo 1<>{fifo_path}'"
        for event in self.HOOKS:
            tmux("set-hook", "-g", event, hook)
        # O_RDWR (not O_RDONLY): keep a write-end open ourselves so the FIFO never reaches EOF when
        # the hook's writer exits — an EOF read-end makes add_reader fire nonstop (a busy-loop).
        self._fifo_fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
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

    def _record_history(self, panes):
        # One history file per content pane, rewritten only when its name or title actually changed
        # (avoids a disk write every 3s poll for idle panes). Sidebar/placeholder panes carry no
        # useful name/title of their own, so they're skipped.
        for p in panes:
            if p["title"] == "(tmux-tabs)" or p["pane_dead"]:
                continue
            key = (p["name"], p["title"])
            if self._history_seen.get(p["pane"]) == key:
                continue
            self._history_seen[p["pane"]] = key
            write_pane_history(p["pane"], p["name"], p["title"], p["cwd"], p["command"])

    def refresh_windows(self):
        # ONE tmux read for the whole refresh: dump every pane, derive everything in Python, then act
        # on where focus actually landed. No re-entrancy guard needed: asyncio reader callbacks run to
        # completion one at a time, so our own select-window/swap echo arrives only AFTER this returns —
        # by which point _current_window is updated and the echo is filtered as "the window we're on".
        panes = tmux_panes(self._session)
        windows = _windows_from(panes)
        for w in windows.values():
            # Latch bell by window_id, not index: tmux's own window_bell_flag stays attached to the
            # index slot across swap-window/move-window, so a moved window can lose its bell (or a
            # window swapped INTO that slot can wrongly inherit one) if we trust the raw flag alone.
            if w["bell"]:
                self._bell_latch[w["win"]] = True
            elif w["status"] == "active":
                self._bell_latch.pop(w["win"], None)  # viewing the window clears its latch
            if self._bell_latch.get(w["win"]) and w["status"] not in ("dead", "active"):
                w["status"] = "bell"
            w["emoji"] = self._emoji.get(w["win"])
        self.windows = windows
        self._record_history(panes)
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
        # The sidebar keeps a FIXED char width so content panes never reflow as we swap it between
        # windows. When its live width drifts from what we pinned (_pinned), either the user dragged the
        # border or the terminal resized — and tmux floats the sidebar proportionally on a resize, which
        # we must NOT adopt as the new width. Distinguish by the total window width: a resize changes it,
        # a drag never does. (Comparing placeholder widths instead would fail under window-size=latest,
        # where background windows don't float until displayed.) Adopt only on a drag; then clear _pinned
        # so _sync_placeholders re-pins the sidebar and all placeholders back to the fixed width — it
        # skips the pin while _width == _pinned, which would leave a floated sidebar stuck.
        if self._pinned is not None and mine["width"] != self._pinned:
            if mine["win_width"] == self._term_width:  # window unchanged -> border drag
                self._width = max(10, mine["width"])
            self._pinned = None
        self._term_width = mine["win_width"]
        self._sync_placeholders(self._width, mine["index"])

    def find_window(self, key, value=True):
        return next((i for i, w in self.windows.items() if w[key] == value), None)

    def _sync_placeholders(self, width, current):
        # Every window except `current` (the one holding the sidebar) gets a thin placeholder in the
        # sidebar's slot, so content panes keep a constant width whether the sidebar or a placeholder
        # sits beside them. Each window's placeholder (if any) is the "(tmux-tabs)" pane in its own
        # pane list — no hand-maintained dict to drift after swaps.
        resize = width != self._pinned
        # Pin widths ONLY when the effective width actually changed (a drag or terminal resize), never
        # on every refresh. The sidebar's own window needs the main-vertical layout to hold it as the
        # left pane; settle it first.
        if resize:
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
                self._make_placeholder(index, width)
            elif resize and width != ph["width"]:
                # Re-pin any placeholder still at the old width. select-layout main-vertical only
                # resizes the CURRENTLY displayed window (tmux lays out a window at its own size, which
                # a background window doesn't recompute until shown), so a background placeholder is
                # resized directly with resize-pane -x — which does take on an unshown window.
                tmux("resize-pane", "-t", ph["pane"], "-x", str(_safe_width(index, width)))
        self._pinned = width

    def _pin_pane(self, win_index, pane, width):
        try:
            tmux("set-window-option", "-t", win_index, "main-pane-width", str(_safe_width(win_index, width)))
            tmux("select-layout", "-t", win_index, "main-vertical", check=True)
        except subprocess.CalledProcessError:
            tmux("resize-pane", "-t", pane, "-x", str(max(1, width)))

    def _make_placeholder(self, win_index, width):
        # A thin left pane running bare `cat` (minimal no-op process). Titled "(tmux-tabs)" like the
        # sidebar itself, so a fresh bare launch reaps any leftover sidebar+placeholders in one sweep
        # before splitting.
        content = tmux("list-panes", "-t", win_index, "-f", "#{pane_active}", "-F", "#{pane_id}")
        # -f: span the full window height on the left edge, so a multi-pane window still gets the
        # placeholder as a single leftmost column (not split off just one content pane). Width is clamped
        # window-safe (self._width may exceed a narrower target window) — see _safe_width.
        ph = tmux("split-window", "-t", content, "-fhb", "-l", str(_safe_width(content, width)),
                  "-d", "-P", "-F", "#{pane_id}", "cat")
        tmux("select-pane", "-t", ph, "-T", "(tmux-tabs)")
        return ph

    def switch_to(self, win_index):
        # Resolve target window's active pane (window-number target is ambiguous from client context).
        # The content pane to bounce focus to; None if the active pane there is our own sidebar or a
        # bare placeholder (e.g. a window whose content just died, leaving the placeholder as the sole
        # and thus "active" pane) — bouncing onto a placeholder would leave the sidebar's swapped-in
        # pane holding focus in the target window instead.
        active_id, _, active_title = tmux("list-panes", "-t", win_index, "-f", "#{pane_active}",
                                           "-F", "#{pane_id}\n#{pane_title}").partition("\n")
        self._selected_pane = active_id if active_id != self._pane_id and active_title != "(tmux-tabs)" else None
        if win_index == self._current_window:
            if self._selected_pane:
                tmux("select-pane", "-t", self._selected_pane)
            return
        tgt = tmux("list-panes", "-t", win_index, "-F", "#{pane_id}\n#{pane_title}").splitlines()
        placeholder = next((tgt[i] for i in range(0, len(tgt), 2) if tgt[i + 1] == "(tmux-tabs)"), None) \
            or self._make_placeholder(win_index, self._width)
        tmux("resize-pane", "-t", placeholder, "-x", str(_safe_width(win_index, self._width)))
        tmux("swap-pane", "-d", "-s", self._pane_id, "-t", placeholder)
        if content := next((p for p in tmux("list-panes", "-t", self._current_window, "-F", "#{pane_id}").splitlines()
                            if p != placeholder), None):
            tmux("select-pane", "-t", content)
        tmux("select-window", "-t", win_index)
        # Bounce the target window's own active-pane state off the placeholder we just swapped out:
        # left alone, tmux still considers the placeholder "active" there (it was, until swap-pane),
        # so its title would read blank on next refresh. _selected_pane is the real content pane to
        # land on if there is one; otherwise fall back to whatever the swap-pane left behind.
        target = self._selected_pane or next(
            (p for p in tmux("list-panes", "-t", win_index, "-F", "#{pane_id}").splitlines()
             if p != self._pane_id), None)
        if target:
            tmux("select-pane", "-t", target)
        # Repaint the selection by mutating the dict IN PLACE and toggling the class, never by
        # reassigning self.windows: that reactive recomposes every row, and destroying the row under
        # the cursor mid-click means App never synthesizes the Click that brought us here (its
        # press/release identity check fails), which wedged the whole sidebar after one click.
        # dead/bell rows keep their status so the bell latch survives.
        for i, w in self.windows.items():
            w["active"], w["last"] = i == win_index, i == self._current_window
            if w["status"] in ("active", ""):
                w["status"] = "active" if w["active"] else ""
        for row in self.query(WindowItem):
            row.set_class(row.win_index == win_index, "active")
        self._current_window = win_index
        self._pinned = None


def _safe_width(win_index, width):
    return max(1, min(width, int(pane_var(win_index, "#{window_width}")) - 1))


def _windows_from(panes):
    windows = {}
    for p in panes:
        if (w := windows.get(p["index"])) is None:
            w = windows[p["index"]] = {"index": p["index"], "win": p["win"], "name": p["name"],
                "bell": p["bell"], "active": p["active"], "last": p["last"],
                "content_panes": 0, "title": "", "panes": []}
        w["panes"].append(p)
        if p["title"] != "(tmux-tabs)":
            if not p["pane_dead"]:
                w["content_panes"] += 1
            if p["pane_active"] or not w["title"]:
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


def _enable_traceback_dump():
    # A hung sidebar shows nothing: Textual owns the terminal, so stderr is invisible and the pane
    # just stops responding. SIGUSR1 dumps every thread's Python stack to TRACEBACK_LOG without
    # killing the app — `kill -USR1 $(pgrep -f "tmux_tabs.py --sidebar")`, then read that file.
    # A dump is what identifies a stall (typically an await that can never complete, e.g. awaiting
    # the removal of the very widget whose handler is running); guessing at one costs far more.
    TRACEBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    # The stream stays open for the process lifetime by design: faulthandler writes to its fd from a
    # signal handler, which cannot open or allocate anything at dump time.
    stream = TRACEBACK_LOG.open("a", buffering=1)
    faulthandler.enable(file=stream)  # also dumps on a hard crash (segfault, abort)
    # chain=False, NOT True: chaining runs SIGUSR1's default action after the dump, which with no
    # previous handler installed kills the process (verified: exits -10 on the first signal). We want
    # to inspect a running sidebar and signal it repeatedly, so the default action must be suppressed.
    faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True, chain=False)


def main():
    if "--sidebar" in sys.argv:
        os.environ.setdefault("TEXTUAL_THEME", "")
        _enable_traceback_dump()
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
    tmux("split-window", "-fhb", "-l", "32", f"{sys.argv[0]} --sidebar")


if __name__ == "__main__":
    main()
