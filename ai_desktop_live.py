"""Live view for ai-desktop: a continuous screen stream, change tracking, and a local feed.

Runs inside the background service (ai_desktop_service.py), which keeps it between
commands. With the live view on:

- screenshots, --seen, --shot and wait read the newest streamed frame (milliseconds, no
  screen flash) instead of running a capture tool;
- a tracker notices every change bigger than a blinking caret, so `observe` can wait for
  "something happened and it settled" and hand back a crop of just what changed, and
  actions with --observe return the result of what they did;
- `live serve` publishes the screen on 127.0.0.1 for local vision models and a viewer page
  with a Stop button.

Frame sources: on GNOME Wayland a Mutter screen cast through ai_desktop_helper.py (run by
the system Python, which has GStreamer); elsewhere the usual capture, polled (on X11
through Pillow, which doesn't flash).
"""
from __future__ import annotations

import base64
import collections
import html
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ai_desktop as _ai_desktop

AD = _ai_desktop  # the ai_desktop module in use (bind() fixes it when run as a script)

TRACK_INTERVAL = 0.1  # seconds between looks at the stream for new frames
POLL_INTERVAL = 0.5  # seconds between captures for a polled source
IDLE_PAUSE = 300  # seconds without use before the stream pauses (it resumes on demand)
HISTORY = 16  # frames kept by sequence number, for `observe --since`
MIN_CELLS = 3  # a change smaller than this many 16-px cells (a blinking caret) is ignored
FULL_SHOT_SHARE = 1 / 3  # a change bigger than this share of the screen gets a full screenshot
REGION_MARGIN = 24  # px of context around a changed area in its screenshot
SYSTEM_PYTHON = "/usr/bin/python3"


def bind(module) -> None:
    global AD
    AD = module


# --- frame sources ------------------------------------------------------------------------


class HelperProcess:
    """ai_desktop_helper.py under the system Python, spoken to over pipes."""

    def __init__(self, python: str | None = None) -> None:
        python = python or os.environ.get("AI_DESKTOP_SYSTEM_PYTHON") or SYSTEM_PYTHON
        script = Path(AD.__file__).with_name("ai_desktop_helper.py")
        self.proc = subprocess.Popen(
            [python, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=AD.host_env(), start_new_session=os.name == "posix",
        )
        self.lock = threading.Lock()

    def ask(self, request: dict) -> tuple[dict, list[bytes]]:
        with self.lock:
            if self.proc.poll() is not None:
                raise AD.CliError("the live view helper has exited")
            self.proc.stdin.write((json.dumps(request) + "\n").encode())
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                raise AD.CliError("the live view helper stopped answering")
            head = json.loads(line)
            blobs = []
            for part in head.get("parts", []) if head.get("ok") else []:
                data = self.proc.stdout.read(part["bytes"])
                if len(data) != part["bytes"]:
                    raise AD.CliError("the live view helper sent a short frame")
                blobs.append(data)
            return head, blobs

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()


def compose(parts: list[dict], blobs: list[bytes]):
    """The monitors' frames on one canvas in global logical coordinates."""
    Image = AD.Image
    pieces = []
    for part, data in zip(parts, blobs):
        img = Image.frombytes("RGB", (part["width"], part["height"]), data)
        scale = float(part.get("scale") or 1.0)
        if abs(scale - 1.0) > 1e-3:
            img = img.resize((round(part["width"] / scale), round(part["height"] / scale)), Image.Resampling.LANCZOS)
        pieces.append((int(part["x"]), int(part["y"]), img, scale))
    left = min(x for x, _, _, _ in pieces)
    top = min(y for _, y, _, _ in pieces)
    right = max(x + img.width for x, _, img, _ in pieces)
    bottom = max(y + img.height for _, y, img, _ in pieces)
    if len(pieces) == 1:
        x, y, img, scale = pieces[0]
        return AD.Capture(img, x, y, scale, "all", "none")
    canvas = Image.new("RGB", (right - left, bottom - top), (0, 0, 0))
    for x, y, img, _ in pieces:
        canvas.paste(img, (x - left, y - top))
    return AD.Capture(canvas, left, top, max(s for *_, s in pieces), "all", "none")


class StreamSource:
    """GNOME Wayland: a Mutter screen cast. Mutter sends a frame only when the screen
    changed, so a new frame number is already a sign of change."""

    name = "mutter-screencast"

    def __init__(self, helper_factory=HelperProcess) -> None:
        self.helper_factory = helper_factory
        self.helper = None
        self.seq = -1
        self.cached = None

    def start(self) -> dict:
        if self.helper is None:
            self.helper = self.helper_factory()
        head, _ = self.helper.ask({"op": "start", "cursor": False})
        if not head.get("ok") or not head.get("running"):
            raise AD.CliError(f"the screen cast did not start: {head.get('error') or head}")
        return head

    def stop(self) -> None:
        if self.helper is not None:
            try:
                self.helper.ask({"op": "stop"})
            except (AD.CliError, OSError, ValueError):
                pass
            self.helper.close()
            self.helper = None
        self.cached = None

    def current_seq(self) -> int:
        head, _ = self.helper.ask({"op": "info"})
        if head.get("closed_by_user"):
            raise AD.CliError("screen sharing was stopped in GNOME's indicator; run: ai-desktop live start")
        return int(head.get("seq", 0))

    def capture(self):
        seq = self.current_seq()
        if self.cached is None or seq != self.seq:
            head, blobs = self.helper.ask({"op": "frame"})
            if not head.get("ok"):
                raise AD.CliError(f"no frame from the screen cast: {head.get('error')}")
            self.cached, self.seq = compose(head["parts"], blobs), int(head["seq"])
        return self.seq, self.cached


class PollSource:
    """Everywhere else: capture on demand, at most every POLL_INTERVAL seconds."""

    name = "polled"

    def __init__(self, grab=None) -> None:
        self.grab = grab or default_grab
        self.seq = 0
        self.cached = None
        self.taken = 0.0
        self.fp = None

    def start(self) -> dict:
        self.capture()
        return {"ok": True}

    def stop(self) -> None:
        self.cached = None

    def capture(self):
        now = AD._now()
        if self.cached is None or now - self.taken >= POLL_INTERVAL:
            cap = self.grab()
            fp = AD.fingerprint(cap.image, (max(1, cap.image.width // 16), max(1, cap.image.height // 16)))
            if fp != self.fp:
                self.seq += 1
            self.cached, self.fp, self.taken = cap, fp, now
        return self.seq, self.cached


def default_grab():
    if sys.platform.startswith("linux") and not AD.is_wayland():
        img = AD.ImageGrab.grab(xdisplay=os.environ.get("DISPLAY")).convert("RGB")  # no flash, unlike the tools
        return AD.Capture(img, 0, 0, 1.0, "all", "none")
    return AD.capture_tool(AD.get_backend("auto"), "all")


def stream_available() -> str | None:
    """None when a Mutter screen cast can work here, else why not."""
    if not (sys.platform.startswith("linux") and AD.is_wayland()):
        return "not a Wayland session"
    if AD.desktop_name() != "gnome":
        return "the screen cast uses GNOME's Mutter; this desktop is " + AD.desktop_name()
    python = os.environ.get("AI_DESKTOP_SYSTEM_PYTHON") or SYSTEM_PYTHON
    if not shutil.which(python) and not os.path.exists(python):
        return f"{python} not found"
    check = subprocess.run(
        [python, "-c", "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); "
                       "assert Gst.ElementFactory.find('pipewiresrc')"],
        capture_output=True, text=True, timeout=20, env=AD.host_env(),
    )
    if check.returncode != 0:
        return "GStreamer's PipeWire source is missing (sudo apt install gstreamer1.0-pipewire python3-gi gir1.2-gstreamer-1.0)"
    return None


def choose_source():
    if sys.platform.startswith("linux") and AD.is_wayland():
        why = stream_available()
        if why is not None:
            raise AD.CliError(f"the live view can't stream here: {why}")
        return StreamSource()
    return PollSource()


# --- change tracking ----------------------------------------------------------------------


def significant_blocks(a, b, min_cells: int = MIN_CELLS) -> list[tuple[int, int, int, int]]:
    """Changed areas (left, top, w, h) of image `b` against `a`, without the tiny ones."""
    cell = AD.GUARD_BLOCK_CELL
    blocks = AD.changed_blocks(a, b)
    return [blk for blk in blocks if (blk[2] / cell) * (blk[3] / cell) >= min_cells - 1e-9]


def union(blocks):
    left = min(b[0] for b in blocks)
    top = min(b[1] for b in blocks)
    right = max(b[0] + b[2] for b in blocks)
    bottom = max(b[1] + b[3] for b in blocks)
    return left, top, right - left, bottom - top


class LiveView:
    def __init__(self, source) -> None:
        self.source = source
        self.lock = threading.RLock()
        self.history: collections.OrderedDict[int, object] = collections.OrderedDict()
        self.events: collections.deque = collections.deque(maxlen=200)
        self.seq = -1
        self.latest = None
        self.changed_at = AD._now()  # last significant change
        self.used_at = AD._now()
        self.running = False
        self.paused = False
        self.thread = None
        self.feed = None
        self.listeners: list = []

    # lifecycle
    def start(self) -> dict:
        with self.lock:
            info = self.source.start()
            self.running, self.paused = True, False
            self.used_at = AD._now()
            self._grab()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._track, daemon=True)
                self.thread.start()
            return info

    def stop(self) -> None:
        with self.lock:
            self.running = False
            self.source.stop()
            if self.feed is not None:
                self.feed.shutdown()
                self.feed = None

    def _pause(self) -> None:
        with self.lock:
            if self.running and not self.paused:
                self.source.stop()
                self.paused = True

    def _ensure(self) -> None:
        with self.lock:
            self.used_at = AD._now()
            if self.running and self.paused:
                self.source.start()
                self.paused = False
                self._grab()

    def status(self) -> dict:
        with self.lock:
            return {
                "running": self.running, "paused": self.paused, "source": self.source.name, "seq": self.seq,
                "quiet": round(AD._now() - self.changed_at, 1), "events": len(self.events),
                "feed": self.feed.url if self.feed else "none",
            }

    # frames
    def _grab(self):
        seq, cap = self.source.capture()
        with self.lock:
            if seq != self.seq:
                if self.latest is not None:
                    blocks = significant_blocks(self.latest.image, cap.image)
                    if blocks:
                        self.changed_at = AD._now()
                        x, y, w, h = union(blocks)
                        event = {"seq": seq, "t": time.time(), "x": cap.x + x, "y": cap.y + y, "width": w, "height": h}
                        self.events.append(event)
                        for listener in list(self.listeners):
                            listener(event)
                self.seq, self.latest = seq, cap
                self.history[seq] = cap
                while len(self.history) > HISTORY:
                    self.history.popitem(last=False)
            return self.seq, self.latest

    def _track(self) -> None:
        while self.running:
            if not self.paused:
                if AD._now() - self.used_at > IDLE_PAUSE and self.feed is None:
                    self._pause()
                else:
                    try:
                        self._grab()
                    except Exception:
                        time.sleep(1)  # a hiccup in the helper: try again shortly
            time.sleep(TRACK_INTERVAL if isinstance(self.source, StreamSource) else POLL_INTERVAL)

    def frame(self, backend, monitor: str = "all", cursor: bool = False):
        """FRAME_SOURCE for ai_desktop.capture_screen: the newest frame, cut to `monitor`;
        None when the stream doesn't cover the layout ai-desktop knows (then it captures)."""
        if not self.running:
            return None
        self._ensure()
        _, cap = self._grab()
        monitors = AD.list_monitors(backend)
        left, top, right, bottom = AD.union_rect(monitors)
        if (cap.x, cap.y, cap.x + cap.image.width, cap.y + cap.image.height) != (left, top, right, bottom):
            return None  # the stream and xrandr disagree (a monitor just changed): capture
        target = AD.resolve_monitor(monitor, monitors)
        if target is None:
            return cap
        rect = (target.x - cap.x, target.y - cap.y, target.x - cap.x + target.width, target.y - cap.y + target.height)
        return AD.Capture(cap.image.crop(rect), target.x, target.y, cap.scale, str(target.index), "none")

    def snapshot(self):
        self._ensure()
        return self._grab()


# The live view of this process (the background service); None until `live start`.
LIVE: LiveView | None = None


# --- observing ----------------------------------------------------------------------------


def observe(view: LiveView, *, base=None, timeout: float = 10.0, quiet: float = 0.5,
            min_cells: int = MIN_CELLS, poll: float = 0.05) -> dict:
    """Wait until the screen differs from `base` (a (seq, Capture) pair; default: now) by
    more than a caret, and then stays unchanged for `quiet` seconds. Returns the fields of
    the answer, with the changed area in screen coordinates."""
    base_seq, base_cap = base if base is not None else view.snapshot()
    start = AD._now()
    deadline = start + timeout
    while True:
        seq, cap = view.snapshot()
        settled = AD._now() - view.changed_at >= quiet
        if seq != base_seq and settled:
            blocks = significant_blocks(base_cap.image, cap.image, min_cells) if cap.image.size == base_cap.image.size else [(0, 0, cap.image.width, cap.image.height)]
            if blocks:
                x, y, w, h = union(blocks)
                return {"changed": True, "seq": seq, "since": base_seq, "changed_x": cap.x + x, "changed_y": cap.y + y,
                        "changed_width": w, "changed_height": h, "areas": len(blocks), "_cap": cap}
        if AD._now() >= deadline:
            return {"changed": False, "seq": seq, "since": base_seq, "_cap": cap}
        AD.check_cancel()
        AD._sleep(poll)


def change_shot(backend, result: dict, max_width=None) -> dict:
    """A screenshot of what changed: the changed area with some context, or the whole
    screen when much changed."""
    cap = result.pop("_cap")
    if not result.get("changed"):
        return result
    x, y, w, h = result["changed_x"], result["changed_y"], result["changed_width"], result["changed_height"]
    if w * h >= FULL_SHOT_SHARE * cap.image.width * cap.image.height:
        info = AD.take_screenshot(backend, grid=True, cap=cap, cursor="none", max_width=max_width)
    else:
        m = REGION_MARGIN
        factor = 2 if max(w, h) < 200 else 1
        info = AD.take_screenshot(backend, grid=True, cap=cap, cursor="none", max_width=max_width,
                                  region=(x - m, y - m, w + 2 * m, h + 2 * m), factor=factor)
    result.update(shot=info["path"], screenshot=info)
    return result


class PolledView(LiveView):
    """A short-lived view over plain captures, for observe without the background service."""

    def __init__(self, backend) -> None:
        super().__init__(PollSource(grab=lambda: AD.capture_screen(backend, "all")))
        self.running = True

    def snapshot(self):
        return self._grab()


def view_for(backend) -> LiveView:
    return LIVE if LIVE is not None and LIVE.running else PolledView(backend)


# --- --observe on actions -----------------------------------------------------------------


def before_action(backend) -> tuple:
    """The screen just before an action with --observe (instant with the live view)."""
    view = view_for(backend)
    return view, view.snapshot()


def after_action(backend, prepared, timeout: float = 3.0, quiet: float = 0.4) -> dict:
    view, base = prepared
    result = observe(view, base=base, timeout=timeout, quiet=quiet)
    return {("observe_" + k if k in ("seq", "since", "areas") else k): v for k, v in change_shot(backend, result).items()}


# --- the local feed -----------------------------------------------------------------------


VIEWER = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-desktop live</title>
<style>
:root{color-scheme:light dark;--bg:#f6f6f4;--fg:#1b1b1b;--muted:#666;--card:#fff;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--muted:#999;--card:#202020;--line:#333}}
body{margin:0;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0;flex:1}
#stop{background:#c62828;color:#fff;border:0;border-radius:8px;padding:10px 22px;font-weight:700;font-size:15px;cursor:pointer}
#stop[disabled]{background:#777;cursor:default}
main{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;padding:16px}
@media (max-width:800px){main{grid-template-columns:1fr}}
#screen{width:100%;border:1px solid var(--line);border-radius:6px;background:#000}
#log{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px;max-height:80vh;overflow:auto;font:12px/1.5 ui-monospace,monospace}
#log div{border-bottom:1px solid var(--line);padding:3px 0;word-break:break-word}
.muted{color:var(--muted)}
</style>
<header><h1>ai-desktop live view <span id="state" class="muted"></span></h1><button id="stop">STOP</button></header>
<main><img id="screen" alt="the screen, live"><section><div class="muted">Changes and actions</div><div id="log"></div></section></main>
<script>
const token = "__TOKEN__";
const q = "token=" + encodeURIComponent(token);
document.getElementById("screen").src = "/stream.mjpg?" + q;
const log = document.getElementById("log");
function add(text){const d=document.createElement("div");d.textContent=new Date().toLocaleTimeString()+"  "+text;log.prepend(d);while(log.childNodes.length>200)log.lastChild.remove();}
const events = new EventSource("/events?" + q);
events.addEventListener("change", e => {const c=JSON.parse(e.data);add("changed "+c.width+"x"+c.height+" at "+c.x+","+c.y);});
events.addEventListener("action", e => {const a=JSON.parse(e.data);add((a.ok?"":"FAILED ")+a.command+" "+(a.summary||""));});
events.addEventListener("stopped", e => {document.getElementById("state").textContent="(stopped: resume with 'ai-desktop resume' in a terminal)";document.getElementById("stop").disabled=true;});
document.getElementById("stop").onclick = async () => {
  const r = await fetch("/stop", {method:"POST", headers:{"Authorization":"Bearer "+token}});
  add(r.ok ? "STOP sent: every action now refuses to run" : "stop failed: " + r.status);
};
</script>"""


class Feed:
    """HTTP on 127.0.0.1: the screen for local models, a viewer page with a Stop button.
    Every request needs the token; Host and Origin must be this server, so a web page in
    a browser can't reach it through DNS rebinding or a cross-site request. Nothing here
    performs an action except Stop."""

    def __init__(self, view: LiveView, port: int = 0, home: Path | None = None) -> None:
        self.view = view
        self.token = secrets.token_urlsafe(18)
        self.home = home or AD.home_dir()
        feed = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # stdout is not ours to write
                pass

            def _allowed(self) -> bool:
                port = feed.server.server_address[1]
                hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
                if self.headers.get("Host") not in hosts:
                    return False
                origin = self.headers.get("Origin")
                if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                    return False
                given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    given = auth[7:]
                return secrets.compare_digest(given, feed.token)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._allowed():
                    return self._send(403, b"forbidden\n", "text/plain")
                url = urlparse(self.path)
                params = parse_qs(url.query)
                if url.path == "/":
                    page = VIEWER.replace("__TOKEN__", html.escape(feed.token, quote=True))
                    return self._send(200, page.encode(), "text/html; charset=utf-8")
                if url.path == "/frame.jpg":
                    return self._send(200, feed.jpeg(params), "image/jpeg")
                if url.path == "/status":
                    return self._send(200, json.dumps({**feed.view.status(), "stopped": AD.stop_path().exists()}).encode(), "application/json")
                if url.path == "/stream.mjpg":
                    return feed.stream(self, params)
                if url.path == "/events":
                    return feed.events(self)
                return self._send(404, b"not found\n", "text/plain")

            def do_POST(self):
                if not self._allowed():
                    return self._send(403, b"forbidden\n", "text/plain")
                if urlparse(self.path).path == "/stop":
                    feed.stop_switch()
                    return self._send(200, b'{"stopped": true}\n', "application/json")
                return self._send(404, b"not found\n", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/?token={self.token}"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def stop_switch(self) -> None:
        AD.private_dir(self.home)
        with AD.private_file(self.home / AD.STOP_FILE) as fh:
            fh.write(json.dumps({"since": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": "viewer Stop button"}) + "\n")

    def jpeg(self, params) -> bytes:
        _, cap = self.view.snapshot()
        img = cap.image
        if params.get("grid", ["0"])[0] in ("1", "true"):
            img = AD.draw_grid(img, AD.DEFAULT_SPACING, (cap.x, cap.y))
        try:
            width = int(params.get("max_width", ["0"])[0])
        except ValueError:
            width = 0
        if width and img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))), AD.Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def stream(self, handler, params) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        last = None
        try:
            while True:
                seq, _ = self.view.snapshot()
                if seq != last:
                    data = self.jpeg(params)
                    handler.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    handler.wfile.flush()
                    last = seq
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def events(self, handler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        queue: collections.deque = collections.deque(maxlen=100)
        self.view.listeners.append(queue.append)
        log_pos = _log_end()
        was_stopped = None
        try:
            while True:
                while queue:
                    handler.wfile.write(b"event: change\ndata: " + json.dumps(queue.popleft()).encode() + b"\n\n")
                log_pos, entries = _new_log_entries(log_pos)
                for entry in entries:
                    summary = AD.format_fields(entry.get("result") or {})[:160]
                    data = {"command": entry.get("command"), "ok": entry.get("ok"), "summary": summary}
                    handler.wfile.write(b"event: action\ndata: " + json.dumps(data).encode() + b"\n\n")
                stopped = AD.stop_path().exists()
                if stopped and not was_stopped:
                    handler.wfile.write(b"event: stopped\ndata: {}\n\n")
                was_stopped = stopped
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            if queue.append in self.view.listeners:
                self.view.listeners.remove(queue.append)


def _log_end() -> tuple:
    files = AD.log_files()
    return (files[0][0], files[0][1].st_size) if files else (None, 0)


def _new_log_entries(pos: tuple) -> tuple[tuple, list[dict]]:
    files = AD.log_files()
    if not files:
        return pos, []
    path, st = files[0]
    start = pos[1] if path == pos[0] else 0
    if st.st_size <= start:
        return (path, st.st_size), []
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read()
    entries = []
    for line in chunk.decode("utf-8", "replace").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return (path, st.st_size), entries


def encode_frame(view: LiveView, max_width: int = 1024) -> str:
    """The newest frame as base64 JPEG (for vision models)."""
    feed = Feed.__new__(Feed)
    feed.view = view
    return base64.b64encode(feed.jpeg({"max_width": [str(max_width)]})).decode()
