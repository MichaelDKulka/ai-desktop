#!/usr/bin/env python3
"""AI Desktop Controller: gridded screenshots plus mouse and keyboard control from the CLI.

An agent loop looks like: `screenshot --grid` -> read the image -> `click`/`type`/`key`
-> `screenshot` again. Every command prints one `OK <command> key=value ...` line on
stdout, or `ERROR <message>` on stderr with a non-zero exit code. With `--json` each
command prints one JSON object instead.

WARNING: whoever runs this (typically an AI agent) gets full control of the desktop and
of every account that is logged in on it. See README.md before using it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

__version__ = "0.2.0"

DEFAULT_SPACING = 100
MIN_SPACING = 10

# Screenshot store: the tool's own folder, pruned by age and total size (see "Screenshot store").
DEFAULT_KEEP_DAYS = 30
DEFAULT_KEEP_MB = 2048
PRUNE_INTERVAL = 3600  # seconds between automatic prunes
PRUNE_MARKER = ".last-prune"

EXIT_ERROR = 1
EXIT_FAILSAFE = 3

# Set by main(): --json or AI_DESKTOP_JSON=1 makes every result a single JSON object.
JSON_OUTPUT = False
CURRENT_COMMAND: str | None = None

# Characters `type` can enter on every backend: printable ASCII plus newline and tab.
TYPEABLE = {chr(c) for c in range(32, 127)} | {"\n", "\t"}

KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "control": "ctrl",
    "ctrlleft": "ctrl",
    "shiftleft": "shift",
    "altleft": "alt",
    "option": "alt",
    "del": "delete",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "spacebar": "space",
    "cmd": "meta",
    "command": "meta",
    "super": "meta",
    "win": "meta",
    "winleft": "meta",
    "windows": "meta",
}
KEY_HELP = (
    "enter, tab, esc, space, backspace, delete, up, down, left, right, home, end, "
    "pageup, pagedown, f1-f12, ctrl, shift, alt, cmd/win, or a single character"
)


class CliError(Exception):
    """A user-facing error, reported as `ERROR <message>` with exit code 1.

    Extra fields are included in the JSON object when --json is on (doctor uses this for
    its rows).
    """

    def __init__(self, message: str, **fields) -> None:
        super().__init__(message)
        self.fields = fields


def status(cmd: str, **fields) -> None:
    """Print a one-line, machine-parseable result: `OK <cmd> key=value ...`.

    In JSON mode it prints one object instead. List and dict values only appear in JSON,
    so the text form stays a flat line.
    """
    if JSON_OUTPUT:
        print(json.dumps({"ok": True, "command": cmd, **fields}, default=str), flush=True)
        return
    parts = ["OK", cmd]
    for key, value in fields.items():
        if isinstance(value, (list, dict)):
            continue
        text = str(value)
        if not text or any(c.isspace() for c in text):
            text = f'"{text}"'
        parts.append(f"{key}={text}")
    print(" ".join(parts), flush=True)


def fail(message: str, code: int = EXIT_ERROR, **fields) -> int:
    """Report an error (`ERROR ...` on stderr, or a JSON object on stdout) and return `code`."""
    if JSON_OUTPUT:
        obj = {"ok": False, "command": CURRENT_COMMAND, "error": message, "code": code, **fields}
        print(json.dumps(obj, default=str), flush=True)
    else:
        print(f"ERROR {message}", file=sys.stderr, flush=True)
    return code


def normalize_key(name: str) -> str:
    """Lowercase a key name and map aliases (cmd/win/super -> meta, return -> enter, ...).

    Single characters are kept as-is so `A` still means shift+a.
    """
    if len(name) == 1:
        return name
    key = name.strip().lower()
    if not key:
        raise CliError("empty key name")
    return KEY_ALIASES.get(key, key)


def host_env() -> dict[str, str]:
    """Environment for launching host desktop apps (screenshot tools, Chrome).

    A snap-packaged parent such as the VS Code snap injects its own GTK, GIO and GSettings
    paths into every child process, and host GTK apps then load the snap's libraries and
    schemas and crash. Drop those so the host app sees the real desktop environment.
    """
    env = dict(os.environ)
    snap_dirs = tuple(env[k] for k in ("SNAP", "SNAP_USER_DATA", "SNAP_USER_COMMON") if env.get(k))
    if not snap_dirs:
        return env
    for key in list(env):
        if key == "XDG_DATA_DIRS":
            continue  # filtered entry by entry below, not dropped as a whole
        if key.startswith("SNAP") or env[key].startswith(snap_dirs):
            del env[key]
    if "XDG_DATA_DIRS" in env:
        dirs = [d for d in env["XDG_DATA_DIRS"].split(os.pathsep) if not d.startswith(snap_dirs)]
        if dirs:
            env["XDG_DATA_DIRS"] = os.pathsep.join(dirs)
        else:
            del env["XDG_DATA_DIRS"]
    return env


def is_wayland() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    return session == "wayland" or (not session and bool(os.environ.get("WAYLAND_DISPLAY")))


# --- Grid overlay ------------------------------------------------------------------------

LINE_COLOR = (255, 0, 255, 110)
LABEL_BG = (0, 0, 0, 170)
LABEL_FG = (255, 255, 255, 255)
LABEL_PAD = 2
FONT_CANDIDATES = (
    "DejaVuSans.ttf",
    "Arial.ttf",
    "arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    r"C:\Windows\Fonts\arial.ttf",
)


def load_font(size: int) -> ImageFont.ImageFont:
    """Return a readable TrueType font, falling back to Pillow's built-in font."""
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _draw_label(draw: ImageDraw.ImageDraw, text: str, font, left: int, top: int) -> None:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.rectangle(
        [left, top, left + r - l + 2 * LABEL_PAD, top + b - t + 2 * LABEL_PAD], fill=LABEL_BG
    )
    draw.text((left + LABEL_PAD - l, top + LABEL_PAD - t), text, font=font, fill=LABEL_FG)


def draw_grid(img: Image.Image, spacing: int = DEFAULT_SPACING) -> Image.Image:
    """Return a copy of `img` with lines every `spacing` px labeled with pixel coordinates.

    X labels sit at the top of each vertical line, Y labels at the left of each horizontal
    line, each centred on its line over a semi-transparent dark box.
    """
    base = img.convert("RGBA")
    width, height = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(max(14, min(width, height) // 60))

    xs = range(spacing, width, spacing)
    ys = range(spacing, height, spacing)
    for x in xs:
        draw.line([(x, 0), (x, height - 1)], fill=LINE_COLOR, width=1)
    for y in ys:
        draw.line([(0, y), (width - 1, y)], fill=LINE_COLOR, width=1)

    # Size of the widest possible label decides how many lines to skip so labels never
    # overlap, and keeps the top-left corner clear where the two label rows meet.
    text_w, text_h = _text_size(draw, str(max(width, height)), font)
    box_w, box_h = text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD
    x_every = math.ceil((box_w + 4) / spacing)
    y_every = math.ceil((box_h + 4) / spacing)

    for i, x in enumerate(xs):
        w = _text_size(draw, str(x), font)[0] + 2 * LABEL_PAD
        left = x - w // 2
        if i % x_every == 0 and left >= box_w + 4 and left + w <= width:
            _draw_label(draw, str(x), font, left, 1)
    for i, y in enumerate(ys):
        top = y - box_h // 2
        if i % y_every == 0 and top >= box_h + 4 and top + box_h <= height:
            _draw_label(draw, str(y), font, 1, top)

    return Image.alpha_composite(base, overlay).convert("RGB")


def save_image(img: Image.Image, output: str | Path) -> str:
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        img.save(path)
    except ValueError as exc:  # unknown file extension
        raise CliError(f"cannot save {path}: {exc}") from exc
    return str(path)


# --- Screen capture ----------------------------------------------------------------------

# Tried in order on Linux. Wayland needs one of these (pyautogui can't see native windows);
# on X11 they are preferred too, because we run them ourselves with host_env().
LINUX_SCREENSHOT_TOOLS = (
    ["gnome-screenshot", "-f"],
    ["grim"],
    ["spectacle", "-b", "-n", "-o"],
)


def find_screenshot_tool() -> list[str] | None:
    return next((argv for argv in LINUX_SCREENSHOT_TOOLS if shutil.which(argv[0])), None)


def grab_linux() -> Image.Image:
    tool = find_screenshot_tool()
    if tool is None:
        if is_wayland():
            raise CliError(
                "no Wayland screenshot tool found; install one: "
                "sudo apt install gnome-screenshot (GNOME), grim (Sway/Hyprland) or spectacle (KDE)"
            )
        return load_pyautogui().screenshot().convert("RGB")  # X11: Pillow's XCB grab
    fd, path = tempfile.mkstemp(prefix="ai_desktop_", suffix=".png")
    os.close(fd)
    try:
        result = subprocess.run(
            [*tool, path], capture_output=True, text=True, timeout=30, env=host_env()
        )
        if result.returncode != 0 or os.path.getsize(path) == 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise CliError(f"{tool[0]} failed: {detail}")
        with Image.open(path) as img:
            return img.convert("RGB")
    finally:
        os.remove(path)


def capture_screen(backend: Backend) -> tuple[Image.Image, float]:
    """Capture the screen at the backend's logical size, so image pixels == click coords.

    Returns the image and the original physical/logical scale (2.0 on a Retina display).
    """
    if sys.platform.startswith("linux"):
        img = grab_linux()
    else:
        img = load_pyautogui().screenshot().convert("RGB")
    width, height = backend.size()
    scale = img.width / width
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    return img, scale


# --- Screenshot store --------------------------------------------------------------------

# Only files the tool named itself are ever deleted: YYYYmmdd-HHMMSS-mmm[-N][-gridS].png
STORE_NAME_RE = re.compile(r"^\d{8}-\d{6}-\d{3}(-\d+)?(-grid\d+)?\.png$")


def store_dir() -> Path:
    """`~/.ai-desktop/screenshots`, or `$AI_DESKTOP_HOME/screenshots`."""
    home = os.environ.get("AI_DESKTOP_HOME") or Path.home() / ".ai-desktop"
    return Path(home).expanduser() / "screenshots"


def new_screenshot_path(grid: bool, spacing: int) -> Path:
    """A path in the store named after the local time, that doesn't exist yet."""
    now = datetime.now()
    stem = now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"
    suffix = f"-grid{spacing}" if grid else ""
    folder = store_dir()
    path = folder / f"{stem}{suffix}.png"
    n = 0
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}{suffix}.png"
    return path


def keep_setting(name: str, default: float) -> float | None:
    """Read AI_DESKTOP_KEEP_DAYS or AI_DESKTOP_KEEP_MB; 0 or "off" disables that rule."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return float(default)
    if raw in ("off", "none", "never"):
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise CliError(f"{name} must be a number of days/MB or 'off', not {raw!r}") from exc
    return value if value > 0 else None


def store_files() -> list[tuple[Path, os.stat_result]]:
    """The tool's own screenshots, newest first by modification time.

    Names are local time and not monotonic across a DST change, so ordering and age
    always come from mtime.
    """
    files = []
    try:
        with os.scandir(store_dir()) as entries:
            for entry in entries:
                if STORE_NAME_RE.match(entry.name) and entry.is_file(follow_symlinks=False):
                    files.append((Path(entry.path), entry.stat(follow_symlinks=False)))
    except FileNotFoundError:
        return []
    files.sort(key=lambda item: item[1].st_mtime, reverse=True)
    return files


def prune_store(
    keep_days: float | None, keep_mb: float | None, now: float | None = None
) -> list[Path]:
    """Delete store files older than `keep_days`, then the oldest until the total is under
    `keep_mb`. `None` disables a rule. Returns the paths that were removed."""
    now = time.time() if now is None else now
    files = store_files()
    doomed: list[Path] = []
    if keep_days is not None:
        cutoff = now - keep_days * 86400
        doomed += [path for path, st in files if st.st_mtime < cutoff]
        files = [(path, st) for path, st in files if st.st_mtime >= cutoff]
    if keep_mb is not None:
        total = sum(st.st_size for _, st in files)
        limit = keep_mb * 1024 * 1024
        while files and total > limit:
            path, st = files.pop()  # the oldest is last
            doomed.append(path)
            total -= st.st_size
    removed = []
    for path in doomed:
        try:
            os.remove(path)
        except OSError:  # open in a viewer on Windows, or another agent got there first
            continue
        removed.append(path)
    return removed


def prune_if_due() -> list[Path] | None:
    """Automatic prune, at most once per PRUNE_INTERVAL. Returns None when skipped.

    Filesystem trouble never fails the screenshot that triggered it; a bad
    AI_DESKTOP_KEEP_* value does raise, because that is the user's own setting.
    """
    marker = store_dir() / PRUNE_MARKER
    try:
        if time.time() - marker.stat().st_mtime < PRUNE_INTERVAL:
            return None
    except OSError:
        pass  # no marker yet
    keep_days = keep_setting("AI_DESKTOP_KEEP_DAYS", DEFAULT_KEEP_DAYS)
    keep_mb = keep_setting("AI_DESKTOP_KEEP_MB", DEFAULT_KEEP_MB)
    try:
        removed = prune_store(keep_days, keep_mb)
        marker.touch()
    except OSError:
        return None
    return removed


def _setting_text(value: float | None) -> str | int | float:
    if value is None:
        return "off"
    return int(value) if float(value).is_integer() else value


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


# --- Backends ----------------------------------------------------------------------------


def load_pyautogui():
    """Import pyautogui lazily so --help, grid and launch-chrome work without a display."""
    try:
        import pyautogui
    except Exception as exc:  # ImportError, or Xlib display errors on Linux
        raise CliError(f"cannot load pyautogui: {exc}") from exc
    pyautogui.FAILSAFE = True  # moving the mouse into any screen corner aborts
    pyautogui.PAUSE = 0.1
    return pyautogui


class Backend:
    name = ""

    def size(self) -> tuple[int, int]:
        raise NotImplementedError

    def check_point(self, x: int, y: int) -> None:
        width, height = self.size()
        if not (0 <= x < width and 0 <= y < height):
            raise CliError(
                f"({x}, {y}) is off screen; valid range is x 0..{width - 1}, y 0..{height - 1}"
            )


class PyAutoGuiBackend(Backend):
    """Windows, macOS and Linux X11."""

    name = "pyautogui"

    def __init__(self) -> None:
        self.gui = load_pyautogui()

    def size(self) -> tuple[int, int]:
        width, height = self.gui.size()
        return int(width), int(height)

    def position(self) -> tuple[int, int] | None:
        x, y = self.gui.position()
        return int(x), int(y)

    def check_point(self, x: int, y: int) -> None:
        super().check_point(x, y)
        width, height = self.size()
        if x in (0, width - 1) and y in (0, height - 1):
            raise CliError(
                f"({x}, {y}) is a failsafe corner; leaving the mouse there would abort every "
                "later command. Use a point at least 1 px inside the screen."
            )

    def resolve_key(self, name: str) -> str:
        key = normalize_key(name)
        if len(key) == 1 and key in TYPEABLE:
            return key
        if key == "meta":
            key = "command" if sys.platform == "darwin" else "win"
        if not self.gui.isValidKey(key):
            raise CliError(f"unknown key {name!r}; use one of: {KEY_HELP}")
        return key

    def move(self, x: int, y: int) -> None:
        self.gui.moveTo(x, y)

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        self.gui.click(x=x, y=y, button=button, clicks=clicks, interval=0.1)

    def type_text(self, text: str, interval: float) -> None:
        self.gui.write(text, interval=interval)

    def press(self, key: str) -> None:
        self.gui.press(key)

    def hotkey(self, keys: list[str]) -> None:
        self.gui.hotkey(*keys, interval=0.05)

    def scroll(self, notches: int, x: int | None = None, y: int | None = None) -> None:
        # pyautogui hands Windows the raw wheel delta, and one notch is 120 (WHEEL_DELTA).
        clicks = notches * 120 if sys.platform == "win32" else notches
        self.gui.scroll(clicks, x=x, y=y)


# Linux uinput constants, from <linux/input-event-codes.h> and <linux/uinput.h>.
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
REL_WHEEL = 0x08
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
KEY_LEFTSHIFT = 42
BUS_USB = 0x03
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_DEV_SETUP = 0x405C5503  # struct uinput_setup: input_id, char name[80], u32 ff_effects_max
UI_ABS_SETUP = 0x401C5504  # struct uinput_abs_setup: u16 code, struct input_absinfo
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
INPUT_EVENT = struct.Struct("@llHHi")  # struct input_event: timeval, type, code, value
MOUSE_BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

UINPUT_KEYS = {
    "esc": 1,
    "backspace": 14,
    "tab": 15,
    "enter": 28,
    "ctrl": 29,
    "shift": 42,
    "shiftright": 54,
    "alt": 56,
    "space": 57,
    "capslock": 58,
    **{f"f{i}": 58 + i for i in range(1, 11)},
    "f11": 87,
    "f12": 88,
    "ctrlright": 97,
    "printscreen": 99,
    "altright": 100,
    "home": 102,
    "up": 103,
    "pageup": 104,
    "left": 105,
    "right": 106,
    "end": 107,
    "down": 108,
    "pagedown": 109,
    "insert": 110,
    "delete": 111,
    "meta": 125,
}


def _build_us_charmap() -> dict[str, tuple[int, bool]]:
    """Map each typeable character to (evdev keycode, needs shift) on a US layout."""
    charmap = {" ": (57, False), "\n": (28, False), "\t": (15, False)}
    rows = (
        ("1234567890-=", "!@#$%^&*()_+", 2),
        ("qwertyuiop[]", "QWERTYUIOP{}", 16),
        ("asdfghjkl;'`", 'ASDFGHJKL:"~', 30),
        ("\\zxcvbnm,./", "|ZXCVBNM<>?", 43),
    )
    for plain, shifted, first_code in rows:
        for offset, (p, s) in enumerate(zip(plain, shifted)):
            charmap[p] = (first_code + offset, False)
            charmap[s] = (first_code + offset, True)
    return charmap


US_CHARMAP = _build_us_charmap()


def x_root_size() -> tuple[int, int]:
    """Size of the X root window via the system libX11.

    Under Wayland this is Xwayland's root, which spans the desktop in logical pixels.
    pyautogui isn't used here: its python3-xlib dependency can't read mutter's Xwayland
    auth cookie.
    """
    import ctypes

    hint = "set AI_DESKTOP_SCREEN_SIZE=WIDTHxHEIGHT"
    try:
        x11 = ctypes.CDLL("libX11.so.6")
    except OSError as exc:
        raise CliError(f"cannot load libX11 to read the screen size; {hint}") from exc
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    display = x11.XOpenDisplay(None)
    if not display:
        raise CliError(f"cannot open X display {os.environ.get('DISPLAY', '(unset)')!r}; {hint}")
    try:
        screen = x11.XDefaultScreen(display)
        return x11.XDisplayWidth(display, screen), x11.XDisplayHeight(display, screen)
    finally:
        x11.XCloseDisplay(display)


class _UInputDevice:
    """A short-lived virtual input device on /dev/uinput."""

    def __init__(self, name: str, keys=(), rel=(), abs_axes=()) -> None:
        import fcntl  # POSIX only, so imported here rather than at the top

        self._ioctl = fcntl.ioctl
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except PermissionError as exc:
            raise CliError(
                "no write access to /dev/uinput; add the udev rule from "
                "'Linux on Wayland' in README.md"
            ) from exc
        except FileNotFoundError as exc:
            raise CliError("/dev/uinput not found; run: sudo modprobe uinput") from exc
        try:
            for ev_type, set_bit, codes in (
                (EV_KEY, UI_SET_KEYBIT, keys),
                (EV_REL, UI_SET_RELBIT, rel),
                (EV_ABS, UI_SET_ABSBIT, [code for code, _ in abs_axes]),
            ):
                if codes:
                    self._ioctl(self.fd, UI_SET_EVBIT, ev_type)
                    for code in codes:
                        self._ioctl(self.fd, set_bit, code)
            for code, maximum in abs_axes:
                # input_absinfo: value, minimum, maximum, fuzz, flat, resolution
                self._ioctl(self.fd, UI_ABS_SETUP, struct.pack("Hxx6i", code, 0, 0, maximum, 0, 0, 0))
            setup = struct.pack("HHHH80sI", BUS_USB, 0x1234, 0x5678, 1, name.encode(), 0)
            self._ioctl(self.fd, UI_DEV_SETUP, setup)
            self._ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        # The compositor needs a moment to notice the new device before events count.
        time.sleep(float(os.environ.get("AI_DESKTOP_UINPUT_DELAY", "0.5")))

    def emit(self, ev_type: int, code: int, value: int) -> None:
        os.write(self.fd, INPUT_EVENT.pack(0, 0, ev_type, code, value))
        os.write(self.fd, INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))

    def __enter__(self) -> _UInputDevice:
        return self

    def __exit__(self, *exc) -> None:
        time.sleep(0.1)  # let the compositor drain events before the device disappears
        self._ioctl(self.fd, UI_DEV_DESTROY)
        os.close(self.fd)


class UInputBackend(Backend):
    """Linux Wayland: injects input through virtual devices on /dev/uinput.

    The pointer is an absolute device (like a VM tablet) with its axis range set to the
    logical screen size, so the compositor maps each value straight to a pixel.
    """

    name = "uinput"

    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise CliError("the uinput backend only works on Linux")
        self._size: tuple[int, int] | None = None

    def size(self) -> tuple[int, int]:
        if self._size is None:
            override = os.environ.get("AI_DESKTOP_SCREEN_SIZE")
            if override:
                try:
                    width, height = (int(v) for v in override.lower().split("x"))
                except ValueError as exc:
                    raise CliError("AI_DESKTOP_SCREEN_SIZE must look like 1920x1080") from exc
            else:
                width, height = x_root_size()
            self._size = (int(width), int(height))
        return self._size

    def position(self) -> tuple[int, int] | None:
        return None  # Wayland doesn't let clients read the global pointer position

    def resolve_key(self, name: str) -> tuple[int, bool]:
        key = normalize_key(name)
        if key in UINPUT_KEYS:
            return UINPUT_KEYS[key], False
        if key in US_CHARMAP:
            return US_CHARMAP[key]
        raise CliError(f"unknown key {name!r}; use one of: {KEY_HELP}")

    def _pointer(self) -> _UInputDevice:
        width, height = self.size()
        return _UInputDevice(
            "ai-desktop pointer",
            keys=tuple(MOUSE_BUTTONS.values()),
            rel=(REL_WHEEL,),
            abs_axes=((ABS_X, width - 1), (ABS_Y, height - 1)),
        )

    def _keyboard(self) -> _UInputDevice:
        codes = set(UINPUT_KEYS.values()) | {code for code, _ in US_CHARMAP.values()}
        return _UInputDevice("ai-desktop keyboard", keys=sorted(codes))

    @staticmethod
    def _move_to(dev: _UInputDevice, x: int, y: int) -> None:
        dev.emit(EV_ABS, ABS_X, x)
        dev.emit(EV_ABS, ABS_Y, y)
        time.sleep(0.05)

    @staticmethod
    def _tap(dev: _UInputDevice, code: int, shift: bool) -> None:
        if shift:
            dev.emit(EV_KEY, KEY_LEFTSHIFT, 1)
        dev.emit(EV_KEY, code, 1)
        time.sleep(0.005)
        dev.emit(EV_KEY, code, 0)
        if shift:
            dev.emit(EV_KEY, KEY_LEFTSHIFT, 0)

    def move(self, x: int, y: int) -> None:
        with self._pointer() as dev:
            self._move_to(dev, x, y)

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        code = MOUSE_BUTTONS[button]
        with self._pointer() as dev:
            self._move_to(dev, x, y)
            for i in range(clicks):
                if i:
                    time.sleep(0.08)
                dev.emit(EV_KEY, code, 1)
                time.sleep(0.02)
                dev.emit(EV_KEY, code, 0)

    def type_text(self, text: str, interval: float) -> None:
        with self._keyboard() as dev:
            for ch in text:
                self._tap(dev, *US_CHARMAP[ch])
                time.sleep(interval)

    def press(self, key: tuple[int, bool]) -> None:
        with self._keyboard() as dev:
            self._tap(dev, *key)

    def hotkey(self, keys: list[tuple[int, bool]]) -> None:
        codes: list[int] = []
        for code, shift in keys:
            if shift and KEY_LEFTSHIFT not in codes:
                codes.append(KEY_LEFTSHIFT)
            codes.append(code)
        with self._keyboard() as dev:
            for code in codes:
                dev.emit(EV_KEY, code, 1)
                time.sleep(0.02)
            for code in reversed(codes):
                dev.emit(EV_KEY, code, 0)
                time.sleep(0.02)

    def scroll(self, notches: int, x: int | None = None, y: int | None = None) -> None:
        step = 1 if notches > 0 else -1  # positive REL_WHEEL scrolls up, like pyautogui
        with self._pointer() as dev:
            if x is not None and y is not None:
                self._move_to(dev, x, y)
            for _ in range(abs(notches)):
                dev.emit(EV_REL, REL_WHEEL, step)
                time.sleep(0.02)


BACKENDS = {"pyautogui": PyAutoGuiBackend, "uinput": UInputBackend}


def get_backend(name: str) -> Backend:
    if name == "auto":
        name = "uinput" if is_wayland() else "pyautogui"
    if name not in BACKENDS:
        raise CliError(f"unknown backend {name!r}; use auto, pyautogui or uinput")
    return BACKENDS[name]()


# --- Chrome ------------------------------------------------------------------------------


MAC_CHROME_APPS = (
    "/Applications/Google Chrome.app",
    os.path.expanduser("~/Applications/Google Chrome.app"),
)


def _chrome_from_registry() -> list[str]:
    import winreg

    paths = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
            ) as key:
                paths.append(winreg.QueryValue(key, None))
        except OSError:
            pass
    return paths


def find_chrome_executable() -> str:
    candidates: list[str] = []
    if sys.platform == "win32":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        candidates.extend(_chrome_from_registry())
        names = ("chrome",)
    else:
        names = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise CliError("Google Chrome not found in the usual locations or on PATH")


def spawn_detached(argv: list[str]) -> None:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": host_env(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def launch_chrome_macos(url: str | None, profile: str | None) -> None:
    app = next((p for p in MAC_CHROME_APPS if os.path.isdir(p)), "Google Chrome")
    argv = ["open", "-a", app]
    if url:
        argv.append(url)
    if profile:
        argv += ["--args", f"--profile-directory={profile}"]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise CliError(f"could not open Google Chrome: {result.stderr.strip()}")
    status("launch-chrome", app=app, profile=profile or "last-used", url=url or "none")


# --- Clipboard ---------------------------------------------------------------------------

_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


def _clipboard_tools() -> tuple[list[str], list[str]]:
    """Return (set_argv, get_argv) for this platform's clipboard tool."""
    if sys.platform == "darwin":
        return ["pbcopy"], ["pbpaste"]
    if sys.platform == "win32":
        return (
            _PS + ["[Console]::InputEncoding = [Text.Encoding]::UTF8; "
                   "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            _PS + ["[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                   "$c = Get-Clipboard -Raw; if ($c) { [Console]::Out.Write($c) }"],
        )
    # X tools come first: under Wayland they go through Xwayland, whose clipboard the
    # compositor shares with native apps, and unlike wl-copy they never steal focus.
    candidates = []
    if os.environ.get("DISPLAY"):
        candidates += [
            (["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"]),
            (["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]),
        ]
    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append((["wl-copy"], ["wl-paste", "--no-newline"]))
    for set_argv, get_argv in candidates:
        if shutil.which(set_argv[0]):
            return set_argv, get_argv
    raise CliError("no clipboard tool found; install one: sudo apt install xclip (or xsel, or wl-clipboard)")


def _clipboard_env() -> dict[str, str]:
    env = host_env()
    if sys.platform == "darwin":
        env["LANG"] = "en_US.UTF-8"  # pbcopy/pbpaste pick their text encoding from LANG
    return env


def set_clipboard(text: str) -> None:
    set_argv, _ = _clipboard_tools()
    # xclip, xsel and wl-copy fork into the background to serve the clipboard and keep
    # stdout/stderr open, so capture stderr in a file: run() would wait forever on a pipe.
    with tempfile.TemporaryFile() as err:
        result = subprocess.run(
            set_argv, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=err,
            env=_clipboard_env(), timeout=15,
        )
        if result.returncode != 0:
            err.seek(0)
            detail = err.read().decode(errors="replace").strip() or f"exit code {result.returncode}"
            raise CliError(f"{set_argv[0]} could not set the clipboard: {detail}")


def get_clipboard() -> str:
    _, get_argv = _clipboard_tools()
    result = subprocess.run(get_argv, capture_output=True, env=_clipboard_env(), timeout=15)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        if any(s in detail.lower() for s in ("not available", "nothing is copied", "no selection")):
            return ""  # empty clipboard, or it holds something other than text
        raise CliError(f"{get_argv[0]} could not read the clipboard: {detail or result.returncode}")
    return result.stdout.decode("utf-8", errors="replace")


def print_text(cmd: str, text: str, **fields) -> None:
    """Status line, then the text verbatim (as UTF-8, whatever the console encoding).

    In JSON mode the text is the "text" field instead.
    """
    if JSON_OUTPUT:
        status(cmd, chars=len(text), text=text, **fields)
        return
    status(cmd, chars=len(text), **fields)
    sys.stdout.buffer.write(text.encode("utf-8"))
    if text and not text.endswith("\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()


def _shortcut(backend: Backend, letter: str) -> list:
    """Copy/paste shortcut keys: cmd+<letter> on macOS, ctrl+<letter> elsewhere."""
    modifier = "cmd" if sys.platform == "darwin" else "ctrl"
    return [backend.resolve_key(modifier), backend.resolve_key(letter)]


# --- Doctor ------------------------------------------------------------------------------


class Check(NamedTuple):
    name: str
    state: str  # ok | warn | fail
    detail: str
    hint: str = ""


def run_check(name: str, fn, soft: bool = False) -> Check:
    """Run one check; any exception becomes a fail (a warn when `soft`) so doctor never crashes.

    `fn` returns `(state, detail)` or `(state, detail, hint)`.
    """
    try:
        state, detail, *hint = fn()
    except Exception as exc:  # every failure must become a row
        return Check(name, "warn" if soft else "fail", str(exc).strip() or type(exc).__name__)
    return Check(name, state, detail, hint[0] if hint else "")


MAC_PERMISSION_HINT = (
    "System Settings > Privacy & Security > {perm}: enable the app you run the agent from "
    "(Terminal, iTerm, VS Code...), then quit and reopen it. macOS attributes this check to "
    "the app this command was launched from, so run doctor from the same place as the agent."
)


def mac_permission(framework: str, symbol: str) -> bool | None:
    """Ask macOS whether this process has a TCC permission, via ctypes (no extra packages)."""
    import ctypes

    lib = ctypes.CDLL(f"/System/Library/Frameworks/{framework}.framework/{framework}")
    fn = getattr(lib, symbol, None)
    if fn is None:  # older macOS without this API
        return None
    fn.restype = ctypes.c_bool
    fn.argtypes = []
    return bool(fn())


def windows_dpi_scale() -> float:
    import ctypes

    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()  # pyautogui does the same at import
    except Exception:
        pass
    try:
        dpi = user32.GetDpiForSystem()  # Windows 10 1607+
    except AttributeError:
        dc = user32.GetDC(None)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        user32.ReleaseDC(None, dc)
    return dpi / 96


def _check_python():
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"
    return ("ok" if v >= (3, 9) else "fail"), detail, "Python 3.9 or newer is required"


def _check_pillow():
    import PIL

    return "ok", f"Pillow {PIL.__version__}"


def _check_pyautogui():
    gui = load_pyautogui()
    return "ok", f"pyautogui {getattr(gui, '__version__', '?')}"


def _check_mac_permission(perm: str, framework: str, symbol: str):
    granted = mac_permission(framework, symbol)
    if granted is None:
        return "warn", "cannot query on this macOS version", "grant it by hand if needed"
    if granted:
        return "ok", "granted"
    return "fail", "not granted", MAC_PERMISSION_HINT.format(perm=perm)


def _check_windows_dpi():
    scale = windows_dpi_scale()
    if abs(scale - 1) < 0.01:
        return "ok", "100% display scaling"
    return (
        "warn",
        f"{scale * 100:.0f}% display scaling",
        "100% is recommended; other scales work on the primary screen only",
    )


def _check_linux_session():
    session = "wayland" if is_wayland() else "x11"
    detail = (
        f"{session} (DISPLAY={os.environ.get('DISPLAY', '')!r}, "
        f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', '')!r})"
    )
    if os.environ.get("SNAP"):
        return (
            "warn",
            detail + ", launched from a snap",
            "host tools (screenshot, Chrome, xclip) get a cleaned environment automatically",
        )
    return "ok", detail


def _check_linux_screenshot_tool():
    tool = find_screenshot_tool()
    if tool:
        return "ok", shutil.which(tool[0])
    if is_wayland():
        return (
            "fail",
            "none found",
            "sudo apt install gnome-screenshot (GNOME), grim (Sway/Hyprland) or spectacle (KDE)",
        )
    return "warn", "none found; using pyautogui", "sudo apt install gnome-screenshot for a more reliable capture"


def _check_uinput():
    if not os.path.exists("/dev/uinput"):
        return "fail", "/dev/uinput missing", "sudo modprobe uinput"
    if not os.access("/dev/uinput", os.W_OK):
        return (
            "fail",
            "/dev/uinput not writable",
            "add the udev rule from README.md ('Linux on Wayland'), or run ./setup.sh",
        )
    return "ok", "/dev/uinput writable"


def _check_libx11():
    override = os.environ.get("AI_DESKTOP_SCREEN_SIZE")
    if override:
        return "ok", f"AI_DESKTOP_SCREEN_SIZE={override}"
    width, height = x_root_size()
    return "ok", f"libX11 reports {width}x{height}"


def _check_clipboard():
    set_argv, _ = _clipboard_tools()
    return "ok", set_argv[0]


def _check_chrome():
    if sys.platform == "darwin":
        for app in MAC_CHROME_APPS:
            if os.path.isdir(app):
                return "ok", app
        return "warn", "Google Chrome.app not found", "install Chrome to use launch-chrome"
    return "ok", find_chrome_executable()


def _check_store():
    folder = store_dir()
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / f".probe-{os.getpid()}"
    probe.write_bytes(b"ok")
    probe.unlink()
    return "ok", f"{folder} ({len(store_files())} files)"


def run_doctor(backend_name: str) -> list[Check]:
    linux = sys.platform.startswith("linux")
    wayland = is_wayland()
    checks = [run_check("python", _check_python), run_check("pillow", _check_pillow)]
    if linux:
        checks.append(run_check("session", _check_linux_session))
        checks.append(run_check("screenshot-tool", _check_linux_screenshot_tool))
    # On Wayland the uinput backend does the work, so pyautogui is only a nice-to-have there.
    checks.append(run_check("pyautogui", _check_pyautogui, soft=wayland and backend_name != "pyautogui"))
    if sys.platform == "darwin":
        checks.append(
            run_check(
                "screen-recording",
                lambda: _check_mac_permission(
                    "Screen Recording", "CoreGraphics", "CGPreflightScreenCaptureAccess"
                ),
            )
        )
        checks.append(
            run_check(
                "accessibility",
                lambda: _check_mac_permission(
                    "Accessibility", "ApplicationServices", "AXIsProcessTrusted"
                ),
            )
        )
    if sys.platform == "win32":
        checks.append(run_check("dpi-scale", _check_windows_dpi))
        checks.append(
            run_check("powershell", lambda: ("ok", shutil.which("powershell") or "missing"))
        )
    if wayland:
        checks.append(run_check("uinput", _check_uinput))
        checks.append(run_check("libx11", _check_libx11))

    backend: Backend | None = None

    def _backend():
        nonlocal backend
        backend = get_backend(backend_name)
        return "ok", backend.name

    checks.append(run_check("backend", _backend))
    if backend is not None:
        checks.append(run_check("screen-size", lambda: ("ok", "%dx%d logical px" % backend.size())))

        def _capture():
            img, scale = capture_screen(backend)
            return "ok", f"{img.width}x{img.height}, scale {scale:g} (the screen may flash once)"

        checks.append(run_check("screenshot", _capture))
    checks.append(run_check("clipboard", _check_clipboard))
    checks.append(run_check("chrome", _check_chrome, soft=True))
    checks.append(run_check("store", _check_store))
    return checks


# --- Commands ----------------------------------------------------------------------------


def cmd_screenshot(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    img, scale = capture_screen(backend)
    if args.grid:
        img = draw_grid(img, args.spacing)
    path = save_image(img, args.output or new_screenshot_path(args.grid, args.spacing))
    # Prune after saving: the capture has already succeeded, and the new file is never a
    # candidate. Explicit -o paths live outside the store and are never pruned.
    pruned = prune_if_due() if args.output is None else None
    status(
        "screenshot",
        path=path,
        width=img.width,
        height=img.height,
        spacing=args.spacing if args.grid else "none",
        scale=round(scale, 3),
        backend=backend.name,
        pruned="skipped" if pruned is None else len(pruned),
    )


def cmd_screenshots(args: argparse.Namespace) -> None:
    folder = store_dir()
    files = store_files()
    if args.action == "dir":
        status(
            "screenshots",
            action="dir",
            path=str(folder),
            count=len(files),
            mb=round(sum(st.st_size for _, st in files) / 1048576, 1),
            keep_days=_setting_text(keep_setting("AI_DESKTOP_KEEP_DAYS", DEFAULT_KEEP_DAYS)),
            keep_mb=_setting_text(keep_setting("AI_DESKTOP_KEEP_MB", DEFAULT_KEEP_MB)),
        )
    elif args.action == "latest":
        if not files:
            raise CliError(f"no screenshots in {folder} yet; run: ai-desktop screenshot --grid")
        path, st = files[0]
        status("screenshots", action="latest", path=str(path), modified=_iso(st.st_mtime))
    elif args.action == "list":
        rows = [
            {"path": str(path), "bytes": st.st_size, "modified": _iso(st.st_mtime)}
            for path, st in files[: args.limit]
        ]
        status("screenshots", action="list", path=str(folder), count=len(rows), files=rows)
        if not JSON_OUTPUT:
            for row in rows:
                print(row["path"])
    else:  # clean: ignores the hourly throttle and applies both rules now
        if args.all:
            keep_days: float | None = 0.0
            keep_mb: float | None = None
        else:
            keep_days = args.days if args.days is not None else keep_setting(
                "AI_DESKTOP_KEEP_DAYS", DEFAULT_KEEP_DAYS
            )
            keep_mb = keep_setting("AI_DESKTOP_KEEP_MB", DEFAULT_KEEP_MB)
        sizes = {path: st.st_size for path, st in files}
        removed = prune_store(keep_days, keep_mb)
        try:
            (folder / PRUNE_MARKER).touch()
        except OSError:
            pass
        status(
            "screenshots",
            action="clean",
            path=str(folder),
            deleted=len(removed),
            kept=len(files) - len(removed),
            freed_mb=round(sum(sizes.get(path, 0) for path in removed) / 1048576, 1),
        )


def cmd_grid(args: argparse.Namespace) -> None:
    src = Path(args.input).expanduser()
    if not src.is_file():
        raise CliError(f"input image not found: {src}")
    with Image.open(src) as img:
        gridded = draw_grid(img, args.spacing)
    path = save_image(gridded, args.output or src.with_name(f"{src.stem}_grid.png"))
    status("grid", path=path, width=gridded.width, height=gridded.height, spacing=args.spacing)


def cmd_click(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    backend.check_point(args.x, args.y)
    clicks = 2 if args.double else 1
    backend.click(args.x, args.y, args.button, clicks)
    status("click", x=args.x, y=args.y, button=args.button, clicks=clicks)


def cmd_move(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    backend.check_point(args.x, args.y)
    backend.move(args.x, args.y)
    status("move", x=args.x, y=args.y)


def cmd_type(args: argparse.Namespace) -> None:
    bad = sorted({c for c in args.text if c not in TYPEABLE})
    if bad:
        raise CliError("cannot type these characters (ASCII only): " + " ".join(map(repr, bad)))
    backend = get_backend(args.backend)
    backend.type_text(args.text, args.interval)
    status("type", chars=len(args.text))


def cmd_key(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    backend.press(backend.resolve_key(args.key))
    status("key", key=args.key)


def cmd_hotkey(args: argparse.Namespace) -> None:
    names = args.keys.split("+")
    if any(not n.strip() for n in names):
        raise CliError(f"bad hotkey {args.keys!r}; use names joined by '+', e.g. ctrl+shift+t")
    backend = get_backend(args.backend)
    backend.hotkey([backend.resolve_key(n.strip()) for n in names])
    status("hotkey", keys=args.keys)


def cmd_scroll(args: argparse.Namespace) -> None:
    if (args.x is None) != (args.y is None):
        raise CliError("give both --x and --y, or neither")
    backend = get_backend(args.backend)
    if args.x is not None:
        backend.check_point(args.x, args.y)
    notches = args.amount if args.direction == "up" else -args.amount
    backend.scroll(notches, args.x, args.y)
    status(
        "scroll",
        direction=args.direction,
        amount=args.amount,
        x=args.x if args.x is not None else "current",
        y=args.y if args.y is not None else "current",
    )


def cmd_copy(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    keys = _shortcut(backend, "c")
    # Park a sentinel on the clipboard so stale contents can't pass for a fresh copy.
    previous = get_clipboard()
    sentinel = f"ai-desktop-copy-{uuid.uuid4().hex}"
    set_clipboard(sentinel)
    time.sleep(0.2)
    backend.hotkey(keys)
    text = sentinel
    deadline = time.monotonic() + 2
    while text == sentinel and time.monotonic() < deadline:
        time.sleep(0.1)
        text = get_clipboard()
    if text == sentinel:
        set_clipboard(previous)
        raise CliError(
            "nothing was copied; select something first (terminals copy with ctrl+shift+c: "
            "use hotkey, then clipboard get)"
        )
    print_text("copy", text)


def cmd_paste(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    keys = _shortcut(backend, "v")
    if args.text is not None:
        set_clipboard(args.text)
        time.sleep(0.2)  # let the compositor pick up the new clipboard owner
    backend.hotkey(keys)
    status("paste", chars=len(args.text) if args.text is not None else "clipboard")


def cmd_clipboard(args: argparse.Namespace) -> None:
    if args.action == "set":
        if args.text is None:
            raise CliError("clipboard set needs --text")
        set_clipboard(args.text)
        status("clipboard", action="set", chars=len(args.text))
    else:
        print_text("clipboard", get_clipboard(), action="get")


def cmd_position(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    width, height = backend.size()
    x, y = backend.position() or ("unknown", "unknown")
    status("position", x=x, y=y, width=width, height=height, backend=backend.name)


def cmd_launch_chrome(args: argparse.Namespace) -> None:
    if sys.platform == "darwin":
        launch_chrome_macos(args.url, args.profile)
        return
    exe = find_chrome_executable()
    argv = [exe]
    user_data = None
    if sys.platform == "win32":
        # Point at the existing signed-in data so cookies, logins and extensions carry over.
        local = os.environ.get("LOCALAPPDATA")
        candidate = os.path.join(local, "Google", "Chrome", "User Data") if local else None
        if candidate and os.path.isdir(candidate):
            user_data = candidate
            argv.append(f"--user-data-dir={user_data}")
    if args.profile:
        argv.append(f"--profile-directory={args.profile}")
    if args.url:
        argv.append(args.url)
    spawn_detached(argv)
    status(
        "launch-chrome",
        exe=exe,
        user_data_dir=user_data or "default",
        profile=args.profile or "last-used",
        url=args.url or "none",
    )


def cmd_doctor(args: argparse.Namespace) -> None:
    checks = run_doctor(args.backend)
    counts = {state: sum(1 for c in checks if c.state == state) for state in ("ok", "warn", "fail")}
    if not JSON_OUTPUT:
        width = max(len(c.name) for c in checks)
        for c in checks:
            line = f"{c.state:<4} {c.name:<{width}}  {c.detail}"
            if c.hint and c.state != "ok":
                line += f"  -> {c.hint}"
            print(line)
    # Not named ok/warn/fail: "ok" would clash with the JSON success flag.
    info = dict(
        version=__version__,
        platform=f"{platform.system()} {platform.release()}",
        backend=args.backend,
        passed=counts["ok"],
        warnings=counts["warn"],
        failed=counts["fail"],
        checks=[c._asdict() for c in checks],
    )
    if counts["fail"]:
        raise CliError(f"{counts['fail']} check(s) failed; see the lines marked fail", **info)
    status("doctor", **info)


# --- CLI ---------------------------------------------------------------------------------


def _spacing(value: str) -> int:
    spacing = int(value)
    if spacing < MIN_SPACING:
        raise argparse.ArgumentTypeError(f"must be at least {MIN_SPACING}")
    return spacing


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def build_parser() -> argparse.ArgumentParser:
    # Global flags live in a parent parser so they work before or after the subcommand.
    # SUPPRESS stops a subparser's default from overwriting a value given before it;
    # main() fills in the real defaults.
    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        "--backend",
        default=argparse.SUPPRESS,
        choices=("auto", "pyautogui", "uinput"),
        help="input backend; auto picks uinput on Linux Wayland, else pyautogui "
        "(env: AI_DESKTOP_BACKEND)",
    )
    flags.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print one JSON object instead of the OK/ERROR line (env: AI_DESKTOP_JSON=1)",
    )
    parser = argparse.ArgumentParser(
        prog="ai-desktop",
        description="See the desktop with a coordinate grid and control the mouse and keyboard.",
        epilog="WARNING: this gives the caller full control of the desktop and logged-in accounts.",
        parents=[flags],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[flags], **kwargs)

    p = add("screenshot", help="capture the screen, optionally with a coordinate grid")
    p.add_argument("--grid", action="store_true", help="overlay labeled grid lines")
    p.add_argument("--spacing", type=_spacing, default=DEFAULT_SPACING, help="grid spacing in px (default 100)")
    p.add_argument(
        "-o",
        "--output",
        help="output path (default: a timestamped file in the screenshot store, "
        "see 'screenshots dir'; files given here are never auto-deleted)",
    )
    p.set_defaults(func=cmd_screenshot)

    p = add("grid", help="draw a coordinate grid on an existing image")
    p.add_argument("--input", required=True, help="image to annotate")
    p.add_argument("-o", "--output", help="output path (default <input>_grid.png)")
    p.add_argument("--spacing", type=_spacing, default=DEFAULT_SPACING, help="grid spacing in px (default 100)")
    p.set_defaults(func=cmd_grid)

    p = add("click", help="click at a screen coordinate")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.add_argument("--button", choices=tuple(MOUSE_BUTTONS), default="left")
    p.add_argument("--double", action="store_true", help="double-click")
    p.set_defaults(func=cmd_click)

    p = add("move", help="move the mouse to a screen coordinate")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.set_defaults(func=cmd_move)

    p = add("type", help="type text (printable ASCII, newline, tab)")
    p.add_argument("--text", required=True)
    p.add_argument("--interval", type=_non_negative_float, default=0.02, help="seconds between keystrokes (default 0.02)")
    p.set_defaults(func=cmd_type)

    p = add("key", help="press one key, e.g. enter, tab, esc, space, backspace")
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_key)

    p = add("hotkey", help="press a key combination, e.g. ctrl+c, cmd+t, alt+tab")
    p.add_argument("--keys", required=True)
    p.set_defaults(func=cmd_hotkey)

    p = add("scroll", help="scroll the mouse wheel")
    p.add_argument("--direction", choices=("up", "down"), default="down")
    p.add_argument("--amount", type=_positive_int, default=3, help="wheel notches (default 3)")
    p.add_argument("--x", type=int, help="move here before scrolling")
    p.add_argument("--y", type=int, help="move here before scrolling")
    p.set_defaults(func=cmd_scroll)

    p = add("copy", help="press the copy shortcut and print what was copied")
    p.set_defaults(func=cmd_copy)

    p = add("paste", help="press the paste shortcut, optionally putting --text on the clipboard first")
    p.add_argument("--text", help="text to paste; any characters, unlike type")
    p.set_defaults(func=cmd_paste)

    p = add("clipboard", help="read or write the clipboard without pressing keys")
    p.add_argument("action", choices=("get", "set"))
    p.add_argument("--text", help="text for clipboard set")
    p.set_defaults(func=cmd_clipboard)

    p = add("position", help="print the mouse position and screen size")
    p.set_defaults(func=cmd_position)

    p = add("launch-chrome", help="open Google Chrome with your signed-in profile")
    p.add_argument("--url", help="page to open")
    p.add_argument("--profile", help="profile directory, e.g. 'Profile 1' (default: last used)")
    p.set_defaults(func=cmd_launch_chrome)

    p = add("screenshots", help="manage the screenshot store: dir, latest, list, clean")
    p.add_argument("action", choices=("dir", "latest", "list", "clean"))
    p.add_argument("--limit", type=_positive_int, default=20, help="list: how many to show (default 20)")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--days",
        type=_non_negative_float,
        help="clean: delete files older than this many days (default: AI_DESKTOP_KEEP_DAYS or 30)",
    )
    group.add_argument("--all", action="store_true", help="clean: delete every screenshot in the store")
    p.set_defaults(func=cmd_screenshots)

    p = add("doctor", help="check that everything this platform needs is installed and permitted")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    global JSON_OUTPUT, CURRENT_COMMAND
    args = build_parser().parse_args(argv)
    args.backend = getattr(args, "backend", None) or os.environ.get("AI_DESKTOP_BACKEND", "auto")
    JSON_OUTPUT = bool(getattr(args, "json", False)) or os.environ.get(
        "AI_DESKTOP_JSON", ""
    ).strip().lower() in ("1", "true", "yes")
    CURRENT_COMMAND = args.command
    try:
        args.func(args)
    except CliError as exc:
        return fail(str(exc), EXIT_ERROR, **exc.fields)
    except OSError as exc:
        return fail(str(exc), EXIT_ERROR)
    except KeyboardInterrupt:
        return fail("interrupted", 130)
    except Exception as exc:
        gui = sys.modules.get("pyautogui")
        if gui is not None and isinstance(exc, getattr(gui, "FailSafeException", ())):
            return fail(
                "failsafe triggered: the mouse is in a screen corner, so the action was "
                "aborted. Move it away from the corners to continue.",
                EXIT_FAILSAFE,
            )
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
