"""Desktop helper for ai-desktop, run by the system Python (/usr/bin/python3).

GNOME's own APIs need PyGObject (GStreamer for the screen cast, AT-SPI for the
accessibility tree), which Linux distributions ship with the desktop but which can't be
installed into a virtualenv without a compiler. So ai-desktop runs this file with the
system interpreter and talks to it over stdin/stdout:

- one JSON object per line in each direction;
- a "frame" answer is followed by the raw pixel bytes its header announces.

It must not import ai_desktop (that lives in the virtualenv) and must stay compatible with
the Python and PyGObject of Ubuntu 22.04 and newer.

Screen cast: org.gnome.Mutter.ScreenCast, the interface GNOME's own remote desktop and
screen-sharing portal use. A session started by the user's own process needs no dialog;
GNOME shows its screen-sharing indicator while it runs. Mutter sends a frame only when
something on screen changed, so the frame counter (`seq`) doubles as a change signal.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

MUTTER = "org.gnome.Mutter.ScreenCast"
DISPLAY_CONFIG = "org.gnome.Mutter.DisplayConfig"
CURSOR_EMBEDDED = 1


def _call(bus, dest, path, iface, method, args=None, sig=None, timeout=5000):
    params = GLib.Variant(sig, args) if sig else None
    return bus.call_sync(dest, path, iface, method, params, None, Gio.DBusCallFlags.NONE, timeout, None).unpack()


def logical_monitors(bus):
    """[(connector, x, y, scale, primary)] in Mutter's logical (global) coordinates."""
    _serial, _monitors, logical, _props = _call(bus, DISPLAY_CONFIG, "/org/gnome/Mutter/DisplayConfig", DISPLAY_CONFIG, "GetCurrentState")
    out = []
    for x, y, scale, _transform, primary, specs, _p in logical:
        if specs:
            out.append((specs[0][0], int(x), int(y), float(scale), bool(primary)))
    return out


class Cast:
    """One Mutter screen-cast session with a stream per monitor."""

    def __init__(self) -> None:
        Gst.init(None)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.lock = threading.Lock()
        self.frames: dict[str, tuple[int, int, bytes]] = {}  # connector -> (width, height, RGB)
        self.seq = 0
        self.changed_at = time.monotonic()
        self.monitors: list = []
        self.pipelines: list = []
        self.session = None
        self.closed_by_user = False

    def start(self, cursor: bool = True) -> dict:
        if self.session is not None:
            return self.info()
        self.closed_by_user = False
        self.monitors = logical_monitors(self.bus)
        (self.session,) = _call(self.bus, MUTTER, "/org/gnome/Mutter/ScreenCast", MUTTER, "CreateSession", ({},), "(a{sv})")
        self.bus.signal_subscribe(MUTTER, MUTTER + ".Session", "Closed", self.session, None,
                                  Gio.DBusSignalFlags.NONE, self._on_closed)
        nodes: dict[str, int] = {}
        for connector, *_ in self.monitors:
            props = {"cursor-mode": GLib.Variant("u", CURSOR_EMBEDDED if cursor else 0)}
            (stream,) = _call(self.bus, MUTTER, self.session, MUTTER + ".Session", "RecordMonitor", (connector, props), "(sa{sv})")

            def added(_c, _s, _p, _i, _n, params, connector=connector):
                nodes[connector] = params.unpack()[0]

            self.bus.signal_subscribe(MUTTER, MUTTER + ".Stream", "PipeWireStreamAdded", stream, None,
                                      Gio.DBusSignalFlags.NONE, added)
        _call(self.bus, MUTTER, self.session, MUTTER + ".Session", "Start")
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while len(nodes) < len(self.monitors) and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.005)
        if len(nodes) < len(self.monitors):
            self.stop()
            raise RuntimeError("Mutter started no PipeWire stream within 5 s")
        for connector, node in nodes.items():
            pipe = Gst.parse_launch(
                f"pipewiresrc path={node} do-timestamp=true ! videoconvert ! video/x-raw,format=RGB "
                "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
            )
            pipe.get_by_name("sink").connect("new-sample", self._on_sample, connector)
            pipe.set_state(Gst.State.PLAYING)
            self.pipelines.append(pipe)
        deadline = time.monotonic() + 3
        while len(self.frames) < len(nodes) and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.info()

    def _on_closed(self, *_args) -> None:
        # The user clicked "Stop sharing" in GNOME's indicator: respect it until the next
        # explicit `ai-desktop live start`.
        self.closed_by_user = True
        self._teardown()

    def _on_sample(self, sink, connector):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            stride = len(data) // height if height else 0
            if stride != width * 3:  # rows padded: keep only the pixels
                data = b"".join(data[r * stride:r * stride + width * 3] for r in range(height))
            with self.lock:
                self.frames[connector] = (width, height, data)
                self.seq += 1
                self.changed_at = time.monotonic()
        return Gst.FlowReturn.OK

    def _teardown(self) -> None:
        for pipe in self.pipelines:
            pipe.set_state(Gst.State.NULL)
        self.pipelines = []
        with self.lock:
            self.frames = {}

    def stop(self) -> dict:
        self._teardown()
        if self.session is not None:
            try:
                _call(self.bus, MUTTER, self.session, MUTTER + ".Session", "Stop")
            except GLib.Error:
                pass
            self.session = None
        return self.info()

    def info(self) -> dict:
        with self.lock:
            return {
                "ok": True, "running": self.session is not None and bool(self.pipelines),
                "seq": self.seq, "quiet": round(time.monotonic() - self.changed_at, 3),
                "closed_by_user": self.closed_by_user,
                "monitors": [{"connector": c, "x": x, "y": y, "scale": s, "primary": p} for c, x, y, s, p in self.monitors],
            }

    def frame(self) -> tuple[dict, list[bytes]]:
        with self.lock:
            parts, blobs = [], []
            for connector, x, y, scale, primary in self.monitors:
                if connector not in self.frames:
                    continue
                width, height, data = self.frames[connector]
                parts.append({"connector": connector, "x": x, "y": y, "scale": scale, "primary": primary,
                              "width": width, "height": height, "bytes": len(data)})
                blobs.append(data)
            return {"ok": bool(parts), "seq": self.seq, "parts": parts}, blobs



# --- accessibility (AT-SPI) ------------------------------------------------------------------

MAX_NODES = 4000
MAX_DEPTH = 40
# Roles worth showing an agent even without a name.
ACTIONABLE_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "link", "combo box", "entry", "password text", "text", "slider", "spin button",
    "page tab", "list item", "tree item", "table cell", "icon", "menu", "tool bar", "button",
}


def _atspi():
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    return Atspi


def _states(Atspi, acc):
    states = acc.get_state_set()
    names = []
    for name in ("FOCUSED", "ACTIVE", "CHECKED", "SELECTED", "EXPANDED", "ENABLED", "EDITABLE", "SHOWING", "VISIBLE", "SENSITIVE"):
        if states.contains(getattr(Atspi.StateType, name)):
            names.append(name.lower())
    return names


# In current PyGObject the get_*_iface() helpers return the accessible itself, whose own
# methods shadow the interface's (Accessible.get_text vs Text.get_text), so interface
# functions are called through their classes, after checking the interface is there.


def _has(acc, iface):
    try:
        return iface in (acc.get_interfaces() or [])
    except GLib.Error:
        return False


def _rect(Atspi, acc, kind):
    if not _has(acc, "Component"):
        return None
    try:
        r = Atspi.Component.get_extents(acc, kind)
        return [r.x, r.y, r.width, r.height]
    except GLib.Error:
        return None


def _actions(acc):
    if not _has(acc, "Action"):
        return []
    Atspi = _atspi()
    name = getattr(Atspi.Action, "get_name", None) or Atspi.Action.get_action_name
    try:
        return [name(acc, i) for i in range(Atspi.Action.get_n_actions(acc))]
    except GLib.Error:
        return []


def _text(Atspi, acc, limit):
    if not _has(acc, "Text"):
        return None
    try:
        return Atspi.Text.get_text(acc, 0, min(Atspi.Text.get_character_count(acc), limit))
    except GLib.Error:
        return None


def _apps(Atspi):
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is not None:
            yield i, app


def _active_window(Atspi):
    for i, app in _apps(Atspi):
        for j in range(app.get_child_count()):
            win = app.get_child_at_index(j)
            if win is not None and win.get_state_set().contains(Atspi.StateType.ACTIVE):
                return [i, j], app, win
    return None


def a11y_tree(req: dict) -> dict:
    """Visible, useful elements of the active window (or of every app), flattened. Each has
    a `path` (child indexes from the desktop) to act on it later, and both its SCREEN and
    WINDOW extents: native Wayland apps report SCREEN as window-relative."""
    Atspi = _atspi()
    roots = []
    if req.get("scope") == "all":
        for i, app in _apps(Atspi):
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if win is not None and win.get_state_set().contains(Atspi.StateType.SHOWING):
                    roots.append(([i, j], app, win))
    else:
        found = _active_window(Atspi)
        if found:
            roots.append(found)
    out, count = [], 0
    windows = []
    for path, app, win in roots:
        app_name = app.get_name() or ""
        toolkit = ""
        try:
            toolkit = app.get_toolkit_name() or ""
        except GLib.Error:
            pass
        screen = _rect(Atspi, win, Atspi.CoordType.SCREEN)
        window = _rect(Atspi, win, Atspi.CoordType.WINDOW)
        windows.append({"path": path, "app": app_name, "toolkit": toolkit, "title": win.get_name() or "",
                        "screen": screen, "window": window})
        stack = [(win, path, 0)]
        while stack and count < MAX_NODES:
            acc, apath, depth = stack.pop()
            count += 1
            try:
                states = _states(Atspi, acc)
                if acc is not win and "showing" not in states:
                    continue
                role = acc.get_role_name() or ""
                name = (acc.get_name() or "").strip()
                rect = _rect(Atspi, acc, Atspi.CoordType.SCREEN)
                actions = _actions(acc)
                editable = "editable" in states
                if acc is not win and rect and rect[2] > 0 and rect[3] > 0 and (name or role in ACTIONABLE_ROLES or actions or editable):
                    item = {"path": apath, "role": role, "name": name[:200], "rect": rect, "states": states,
                            "actions": actions[:6], "window": len(windows) - 1}
                    if role in ("entry", "text", "spin button", "combo box"):  # never a password
                        text = _text(Atspi, acc, 200)
                        if text:
                            item["text"] = text
                    out.append(item)
                if depth < MAX_DEPTH:
                    n = acc.get_child_count()
                    for k in range(min(n, 500) - 1, -1, -1):
                        child = acc.get_child_at_index(k)
                        if child is not None:
                            stack.append((child, apath + [k], depth + 1))
            except GLib.Error:
                continue
    out.sort(key=lambda e: (e["window"], e["rect"][1], e["rect"][0]))
    return {"ok": True, "windows": windows, "elements": out, "visited": count, "truncated": count >= MAX_NODES}


def _resolve(Atspi, path):
    acc = Atspi.get_desktop(0)
    for index in path:
        acc = acc.get_child_at_index(index) if acc is not None else None
    return acc


def a11y_do(req: dict) -> dict:
    """Act on the element at `path`, after checking it is still the same one."""
    Atspi = _atspi()
    acc = _resolve(Atspi, req["path"])
    if acc is None:
        return {"ok": False, "error": "stale", "detail": "the element is gone"}
    role, name = acc.get_role_name() or "", (acc.get_name() or "").strip()
    if role != req.get("role") or name[:200] != req.get("name", ""):
        return {"ok": False, "error": "stale", "detail": f"now {role} {name[:40]!r}"}
    what = req.get("do")
    if what == "press":
        actions = _actions(acc)
        for wanted in ("click", "press", "activate", "jump", "toggle", "open"):
            if wanted in actions:
                Atspi.Action.do_action(acc, actions.index(wanted))
                return {"ok": True, "did": wanted}
        if actions:
            Atspi.Action.do_action(acc, 0)
            return {"ok": True, "did": actions[0]}
        if _has(acc, "Component") and Atspi.Component.grab_focus(acc):
            return {"ok": True, "did": "focus"}
        return {"ok": False, "error": "no action", "detail": "the element offers no action and can't take focus"}
    if what == "focus":
        return {"ok": bool(_has(acc, "Component") and Atspi.Component.grab_focus(acc)), "did": "focus"}
    if what == "set-text":
        if not _has(acc, "EditableText"):
            return {"ok": False, "error": "not editable"}
        return {"ok": bool(Atspi.EditableText.set_text_contents(acc, req.get("text", ""))), "did": "set-text"}
    if what == "read":
        info = {"ok": True, "role": role, "name": name, "states": _states(Atspi, acc)}
        if role != "password text":
            text = _text(Atspi, acc, 10000)
            if text is not None:
                info["text"] = text
        if _has(acc, "Value"):
            info["value"] = Atspi.Value.get_current_value(acc)
        return info
    return {"ok": False, "error": f"unknown action {what!r}"}

def main() -> None:
    out = sys.stdout.buffer
    cast = None

    def reply(obj, blobs=()):
        out.write(json.dumps(obj).encode() + b"\n")
        for blob in blobs:
            out.write(blob)
        out.flush()

    for line in sys.stdin.buffer:
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "ping":
                reply({"ok": True, "pid": __import__("os").getpid()})
            elif op == "start":
                cast = cast or Cast()
                reply(cast.start(cursor=bool(req.get("cursor", True))))
            elif op == "stop":
                reply(cast.stop() if cast else {"ok": True, "running": False})
            elif op == "info":
                reply(cast.info() if cast else {"ok": True, "running": False, "seq": 0})
            elif op == "frame":
                if cast is None or cast.session is None:
                    reply({"ok": False, "error": "not started"})
                else:
                    header, blobs = cast.frame()
                    reply(header, blobs)
            elif op == "a11y-tree":
                reply(a11y_tree(req))
            elif op == "a11y-do":
                reply(a11y_do(req))
            else:
                reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as exc:  # report, never die: the service decides what to do
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)})
    if cast is not None:
        cast.stop()


if __name__ == "__main__":
    main()
