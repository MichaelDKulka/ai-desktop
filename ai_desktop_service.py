"""Background service for ai-desktop: keeps the input devices open between commands.

On Wayland every action used to create a virtual mouse or keyboard on /dev/uinput and
wait AI_DESKTOP_UINPUT_DELAY (0.5 s) for the compositor to notice it, then remove it
again: about 0.6 s per action. This per-user process keeps them open, and the CLI hands
its actions to it over a Unix socket.

Rules:
- A missing, busy, stale or broken service never makes a command fail: the CLI then runs
  the command itself, exactly as before.
- The socket lives in a folder only this user can open ($XDG_RUNTIME_DIR/ai-desktop), and
  on Linux the peer's uid is checked too.
- The service runs the same `execute()` as the CLI, with the client's working folder and
  AI_DESKTOP_* settings, and sends back its exit code, stdout and stderr unchanged.
- If the client goes away (Ctrl+C), the running action stops at its next pause.
- It starts on first use and exits after AI_DESKTOP_SERVICE_IDLE seconds (default 1800)
  without a request. AI_DESKTOP_SERVICE=off turns it off.

Linux and macOS only (Unix sockets); elsewhere the CLI always runs commands itself.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import ai_desktop

# Commands the service runs; the rest gain nothing from it (or must see the client's own
# terminal and environment: resume, mcp, launch-chrome, doctor).
FORWARDED = ("click", "move", "drag", "type", "key", "hotkey", "scroll", "paste", "copy", "screenshot", "wait", "run",
             "live", "observe", "text", "ui", "read", "set-text", "locate", "describe")
# These need the service (the live view lives in it), so the client starts one and waits.
NEEDS_SERVICE = ("live",)
DEFAULT_IDLE = 1800  # seconds without a request before the service exits
RESPAWN_GAP = 60  # seconds between attempts to start a service that didn't come up
CONNECT_TIMEOUT = 0.5  # seconds to wait for the service to answer the handshake
MAX_MESSAGE = 64 * 1024 * 1024


def enabled() -> bool:
    if not hasattr(socket, "AF_UNIX") or sys.platform == "win32":
        return False
    return os.environ.get("AI_DESKTOP_SERVICE", "").strip().lower() not in ("0", "off", "false", "no")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "ai-desktop"
    return Path(tempfile.gettempdir()) / f"ai-desktop-{os.getuid()}"


def socket_path() -> Path:
    return runtime_dir() / "service.sock"


def _private_runtime_dir() -> Path:
    """The socket's folder, created 0700; refused if someone else owns it or can enter."""
    folder = runtime_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = folder.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OSError(f"{folder} is not private to this user")
    return folder


def build_id() -> str:
    """Changes whenever the code or the interpreter changes, so an upgraded or edited
    ai-desktop never talks to a service still running the old code."""
    parts = [ai_desktop.__version__, sys.executable, sys.version]
    for module in (ai_desktop, sys.modules[__name__]):
        try:
            st = os.stat(module.__file__)
            parts.append(f"{module.__file__}:{st.st_size}:{st.st_mtime_ns}")
        except (OSError, TypeError):
            parts.append("?")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# Settings that decide which desktop an action reaches. A client whose values differ
# (another session, an ssh login) runs its commands itself.
SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP")


def session_key(env=None) -> dict:
    env = os.environ if env is None else env
    return {k: env.get(k, "") for k in SESSION_KEYS}


def client_settings() -> dict:
    return {k: v for k, v in os.environ.items() if k.startswith("AI_DESKTOP_")}


# --- wire format: one JSON object per line -------------------------------------------------


def _send(sock: socket.socket, obj: dict) -> None:
    sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")


def _recv(sock: socket.socket, buffer: bytearray) -> dict | None:
    """The next message, or None when the other side closed the connection."""
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buffer.extend(chunk)
        if len(buffer) > MAX_MESSAGE:
            raise OSError("message too large")
    line, _, rest = bytes(buffer).partition(b"\n")
    buffer[:] = rest
    return json.loads(line.decode("utf-8"))


# --- client -------------------------------------------------------------------------------


def should_forward(argv: list[str]) -> bool:
    args, _ = ai_desktop.parse_quiet(argv)
    if args is None or args.command not in FORWARDED:
        return False
    if args.command == "run" and getattr(args, "file", None) == "-":
        return False  # the steps come from this process's stdin
    return True


def _connect() -> socket.socket | None:
    path = socket_path()
    try:
        st = path.parent.stat()
    except OSError:
        return None
    # Only talk to a service in a folder this user owns and nobody else can enter: in the
    # /tmp fallback another user could otherwise plant a fake one.
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or not path.exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        return None
    return sock


def spawn() -> bool:
    """Start the service in the background, at most once per RESPAWN_GAP seconds."""
    try:
        folder = _private_runtime_dir()
        marker = folder / "spawned"
        try:
            if time.time() - marker.stat().st_mtime < RESPAWN_GAP:
                return False
        except FileNotFoundError:
            pass
        marker.touch()
        log = ai_desktop.private_dir(ai_desktop.home_dir()) / "service.log"
        with ai_desktop.private_file(log, "ab") as err:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "serve"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                start_new_session=True, close_fds=True, cwd="/",
            )
        return True
    except OSError:
        return False


def try_forward(argv: list[str] | None) -> int | None:
    """Run the command in the service and print its output; None if it should run here."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if not enabled() or not should_forward(argv):
        return None
    sock = _connect()
    if sock is None:
        if argv_command(argv) in NEEDS_SERVICE:
            sock = start_and_connect()
        else:
            spawn()  # ready for the next command; this one runs here
        if sock is None:
            return None
    buffer = bytearray()
    try:
        with sock:
            _send(sock, {"op": "hello", "build": build_id(), "session": session_key()})
            answer = _recv(sock, buffer)
            if not answer or not answer.get("ok"):
                if answer and answer.get("reason") == "build":
                    with contextlib.suppress(OSError):
                        stop()  # old code: the next command starts a fresh service
                return None
            sock.settimeout(None)  # an action may take long; Ctrl+C here cancels it there
            _send(sock, {"op": "run", "argv": argv, "cwd": os.getcwd(), "settings": client_settings()})
            try:
                result = _recv(sock, buffer)
            except KeyboardInterrupt:  # closing the socket makes the service stop the action
                print("ERROR interrupted", file=sys.stderr, flush=True)
                return 130
    except (OSError, ValueError):
        return None
    if result is None or "code" not in result:
        return None
    sys.stdout.write(result.get("stdout", ""))
    sys.stdout.flush()
    sys.stderr.write(result.get("stderr", ""))
    sys.stderr.flush()
    return int(result["code"])


def argv_command(argv: list[str]) -> str | None:
    args, _ = ai_desktop.parse_quiet(argv)
    return None if args is None else args.command


def start_and_connect(wait: float = 5.0) -> socket.socket | None:
    with contextlib.suppress(OSError):
        (runtime_dir() / "spawned").unlink()  # needed now: ignore the respawn gap
    if not spawn():
        return None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        sock = _connect()
        if sock is not None:
            return sock
        time.sleep(0.05)
    return None


def call(argv: list[str], cancel=None, via: str = "cli") -> dict | None:
    """Run a command in the service and return {"code", "stdout", "stderr"} instead of
    printing; None if there is no usable service. `cancel` (a threading.Event) closes the
    connection when set, which stops the command at its next pause."""
    if not enabled():
        return None
    sock = _connect()
    if sock is None:
        spawn()
        return None
    buffer = bytearray()
    try:
        with sock:
            _send(sock, {"op": "hello", "build": build_id(), "session": session_key()})
            answer = _recv(sock, buffer)
            if not answer or not answer.get("ok"):
                if answer and answer.get("reason") == "build":
                    with contextlib.suppress(OSError):
                        stop()
                return None
            settings = {**client_settings(), "AI_DESKTOP_VIA": via}
            _send(sock, {"op": "run", "argv": list(argv), "cwd": os.getcwd(), "settings": settings})
            sock.settimeout(0.2)
            while True:
                try:
                    return _recv(sock, buffer)
                except socket.timeout:
                    if cancel is not None and cancel.is_set():
                        return {"code": 130, "stdout": "", "stderr": "ERROR cancelled by the client\n"}
    except (OSError, ValueError):
        return None


def request(op: str) -> dict | None:
    sock = _connect()
    if sock is None:
        return None
    try:
        with sock:
            buffer = bytearray()
            _send(sock, {"op": op})
            return _recv(sock, buffer)
    except (OSError, ValueError):
        return None


def stop() -> dict | None:
    return request("stop")


def status() -> dict | None:
    return request("status")


# --- server -------------------------------------------------------------------------------


class Service:
    def __init__(self, path: Path, idle: float) -> None:
        self.path = path
        self.idle = idle
        self.build = build_id()
        self.session = session_key()
        self.started = time.time()
        self.requests = 0
        self.last_request = time.monotonic()
        self.running = True

    def serve(self) -> None:
        if _connect() is not None:
            return  # another service already answers here (two clients started one each)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()  # a stale socket left by a service that died
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            listener.bind(str(self.path))
        finally:
            os.umask(old_umask)
        own_inode = self.path.stat().st_ino
        listener.listen(8)
        listener.settimeout(1.0)
        try:
            with ai_desktop.keep_devices():
                while self.running:
                    if time.monotonic() - self.last_request > self.idle and not self._feed_open():
                        break
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        try:
                            self.handle(conn)
                        except (OSError, ValueError):
                            continue
        finally:
            listener.close()
            with contextlib.suppress(OSError):
                if self.path.stat().st_ino == own_inode:  # not a newer service's socket
                    self.path.unlink()

    @staticmethod
    def _feed_open() -> bool:
        live = sys.modules.get("ai_desktop_live")
        return bool(live and live.LIVE is not None and live.LIVE.feed is not None)

    def _peer_ok(self, conn: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return True  # macOS: the 0700 folder is the guard
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", creds)
        return uid == os.getuid()

    def handle(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        if not self._peer_ok(conn):
            return
        buffer = bytearray()
        msg = _recv(conn, buffer)
        if msg is None:
            return
        op = msg.get("op")
        if op == "status":
            _send(conn, {"ok": True, "pid": os.getpid(), "build": self.build, "requests": self.requests,
                         "uptime": round(time.time() - self.started), "idle_exit": self.idle,
                         "session": self.session})
            return
        if op == "stop":
            self.running = False
            _send(conn, {"ok": True, "pid": os.getpid()})
            return
        if op != "hello":
            return
        if msg.get("build") != self.build:
            _send(conn, {"ok": False, "reason": "build"})
            return
        if msg.get("session") != self.session:
            _send(conn, {"ok": False, "reason": "session"})
            return
        _send(conn, {"ok": True})
        conn.settimeout(None)
        req = _recv(conn, buffer)
        if req is None or req.get("op") != "run":
            return
        self.last_request = time.monotonic()
        self.requests += 1
        result = self.run(conn, req)
        if result is not None:
            with contextlib.suppress(OSError):
                _send(conn, result)
        self.last_request = time.monotonic()

    def run(self, conn: socket.socket, req: dict) -> dict | None:
        """Run one command on a worker thread; if the client hangs up, cancel it."""
        token = threading.Event()
        out: dict = {}

        def work():
            out.update(execute_like_cli(req["argv"], req.get("cwd") or "/", req.get("settings") or {}, token))

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.1)
            if worker.is_alive():
                readable, _, _ = select.select([conn], [], [], 0)
                if readable and not conn.recv(1, socket.MSG_PEEK):
                    token.set()  # the client went away: stop at the next pause
        return out if not token.is_set() else None


def execute_like_cli(argv: list[str], cwd: str, settings: dict, token) -> dict:
    """Run `argv` as the CLI would in the client's folder with the client's AI_DESKTOP_*
    settings, capturing exactly what it prints."""
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("AI_DESKTOP_")}
    saved_cwd = os.getcwd()
    raw_out, raw_err = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(raw_out, encoding="utf-8", errors="backslashreplace", write_through=True)
    stderr = io.TextIOWrapper(raw_err, encoding="utf-8", errors="backslashreplace", write_through=True)
    code = 1
    try:
        for key in saved_env:
            os.environ.pop(key, None)
        os.environ.update({k: str(v) for k, v in settings.items() if k.startswith("AI_DESKTOP_")})
        os.environ.pop("AI_DESKTOP_SERVICE", None)
        ai_desktop.LOG_VIA = f"{os.environ.pop('AI_DESKTOP_VIA', None) or 'cli'}/service"
        os.chdir(cwd)
        ai_desktop.CANCEL = token
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = ai_desktop.execute(argv)[0]
            except SystemExit as exc:  # argparse; the client checked the arguments already
                code = exc.code if isinstance(exc.code, int) else 2
            except Exception as exc:  # execute() reports everything; this is a last resort
                print(f"ERROR internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
                code = 1
    finally:
        ai_desktop.CANCEL = None
        with contextlib.suppress(OSError):
            os.chdir(saved_cwd)
        for key in [k for k in os.environ if k.startswith("AI_DESKTOP_")]:
            os.environ.pop(key)
        os.environ.update(saved_env)
        stdout.flush()
        stderr.flush()
    return {"code": code, "stdout": raw_out.getvalue().decode("utf-8"), "stderr": raw_err.getvalue().decode("utf-8")}


def serve() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so atexit removes the devices
    try:
        idle = float(os.environ.get("AI_DESKTOP_SERVICE_IDLE", DEFAULT_IDLE))
    except ValueError:
        idle = DEFAULT_IDLE
    ai_desktop.LOG_VIA = "cli/service"
    ai_desktop.IN_SERVICE = True
    Service(_private_runtime_dir() / "service.sock", idle).serve()


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        print("usage: ai_desktop_service.py serve  (started by ai-desktop itself)", file=sys.stderr)
        sys.exit(2)
