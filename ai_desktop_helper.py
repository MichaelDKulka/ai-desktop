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
            elif op == "a11y":
                import ai_desktop_a11y_helper as a11y  # noqa: F401  (Phase 4 adds this)
                reply(a11y.handle(req))
            else:
                reply({"ok": False, "error": f"unknown op {op!r}"})
        except Exception as exc:  # report, never die: the service decides what to do
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)})
    if cast is not None:
        cast.stop()


if __name__ == "__main__":
    main()
