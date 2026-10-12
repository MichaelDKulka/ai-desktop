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
import contextlib
import ctypes
import io
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

from PIL import Image, ImageDraw, ImageFont, ImageGrab

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

# Set by execute(): --json or AI_DESKTOP_JSON=1 makes every result a single JSON object;
# QUIET suppresses all printing (the MCP server runs commands in-process); the last
# payload is kept for the action log; LOG_VIA says who ran the command.
JSON_OUTPUT = False
QUIET = False
CURRENT_COMMAND: str | None = None
LAST_PAYLOAD: dict | None = None
LOG_VIA = "cli"

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


def format_fields(fields: dict) -> str:
    """`key=value` pairs on one line; values with spaces are quoted, lists/dicts skipped."""
    parts = []
    for key, value in fields.items():
        if isinstance(value, (list, dict)):
            continue
        text = str(value)
        if not text or any(c.isspace() for c in text):
            text = f'"{text}"'
        parts.append(f"{key}={text}")
    return " ".join(parts)


def text_mode() -> bool:
    """True when the plain OK/ERROR text form is being printed (not JSON, not quiet)."""
    return not (JSON_OUTPUT or QUIET)


def status(cmd: str, **fields) -> None:
    """Print a one-line, machine-parseable result: `OK <cmd> key=value ...`.

    In JSON mode it prints one object instead. List and dict values only appear in JSON,
    so the text form stays a flat line. Fields must not be named `ok` or `command`.
    """
    global LAST_PAYLOAD
    payload = {"ok": True, "command": cmd, **fields}
    LAST_PAYLOAD = payload
    if QUIET:
        return
    if JSON_OUTPUT:
        print(json.dumps(payload, default=str), flush=True)
        return
    extra = format_fields(fields)
    print(f"OK {cmd}" + (f" {extra}" if extra else ""), flush=True)


def fail(message: str, code: int = EXIT_ERROR, **fields) -> int:
    """Report an error (`ERROR ...` on stderr, or a JSON object on stdout) and return `code`."""
    global LAST_PAYLOAD
    payload = {"ok": False, "command": CURRENT_COMMAND, "error": message, "code": code, **fields}
    LAST_PAYLOAD = payload
    if QUIET:
        return code
    if JSON_OUTPUT:
        print(json.dumps(payload, default=str), flush=True)
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


def draw_grid(
    img: Image.Image,
    spacing: int = DEFAULT_SPACING,
    origin: tuple[int, int] = (0, 0),
    label_scale: float = 1.0,
) -> Image.Image:
    """Return a copy of `img` with lines every `spacing` px labeled with screen coordinates.

    `origin` is the global coordinate of the image's top-left pixel, so a capture that
    starts at (-1920, 0) gets labels -1900, -1800, ... and lines sit on global multiples
    of `spacing`. X labels sit at the top of each vertical line, Y labels at the left of
    each horizontal line, over a semi-transparent dark box. `label_scale` enlarges the
    labels, for an image that will be downscaled afterwards.
    """
    base = img.convert("RGBA")
    width, height = base.size
    ox, oy = origin
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(max(14, int(min(width, height) // 60 * label_scale)))

    xs = [x for x in range((-ox) % spacing, width, spacing) if x > 0]
    ys = [y for y in range((-oy) % spacing, height, spacing) if y > 0]
    for x in xs:
        draw.line([(x, 0), (x, height - 1)], fill=LINE_COLOR, width=1)
    for y in ys:
        draw.line([(0, y), (width - 1, y)], fill=LINE_COLOR, width=1)

    # The widest possible label decides how many lines to skip so labels never overlap,
    # and keeps the top-left corner clear where the two label rows meet. Negative
    # coordinates are wider than positive ones, so measure the real extremes.
    candidates = [str(v) for v in (ox, ox + width, oy, oy + height)]
    text_w = max(_text_size(draw, text, font)[0] for text in candidates)
    text_h = _text_size(draw, "0123456789-", font)[1]
    box_w, box_h = text_w + 2 * LABEL_PAD, text_h + 2 * LABEL_PAD
    x_every = math.ceil((box_w + 4) / spacing)
    y_every = math.ceil((box_h + 4) / spacing)

    for i, x in enumerate(xs):
        label = str(ox + x)
        w = _text_size(draw, label, font)[0] + 2 * LABEL_PAD
        left = x - w // 2
        if i % x_every == 0 and left >= box_w + 4 and left + w <= width:
            _draw_label(draw, label, font, left, 1)
    for i, y in enumerate(ys):
        top = y - box_h // 2
        if i % y_every == 0 and top >= box_h + 4 and top + box_h <= height:
            _draw_label(draw, str(oy + y), font, 1, top)

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
        # X11 without a tool: Pillow's own XCB capture. pyautogui's screenshot() is no use
        # here, since it refuses to run on Linux unless gnome-screenshot is installed.
        try:
            return ImageGrab.grab(xdisplay=os.environ.get("DISPLAY")).convert("RGB")
        except OSError as exc:
            raise CliError(
                f"cannot capture the X11 screen: {exc}; install a screenshot tool: "
                "sudo apt install gnome-screenshot"
            ) from exc
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


# --- Monitors ----------------------------------------------------------------------------


class Monitor(NamedTuple):
    index: int
    name: str
    x: int  # global logical coordinates, the same space `click` uses
    y: int
    width: int
    height: int
    primary: bool
    scale: float  # physical / logical pixels of this display

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height


def union_rect(monitors: list[Monitor]) -> tuple[int, int, int, int]:
    """Bounding box (left, top, right, bottom) of all monitors, in global coordinates."""
    return (
        min(m.x for m in monitors),
        min(m.y for m in monitors),
        max(m.x + m.width for m in monitors),
        max(m.y + m.height for m in monitors),
    )


def order_monitors(found: list[Monitor]) -> list[Monitor]:
    """Primary first as index 0, then top-to-bottom, left-to-right."""
    ordered = sorted(found, key=lambda m: (not m.primary, m.y, m.x))
    if ordered and not ordered[0].primary:
        ordered[0] = ordered[0]._replace(primary=True)
    return [m._replace(index=i) for i, m in enumerate(ordered)]


XRANDR_MONITOR_RE = re.compile(r"^\s*\d+:\s*([+*]*)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")


def parse_xrandr_monitors(text: str) -> list[Monitor]:
    """Parse `xrandr --listmonitors` output; a `+` before the name marks the primary."""
    found = []
    for line in text.splitlines():
        match = XRANDR_MONITOR_RE.match(line)
        if match:
            flags, name, width, height, x, y = match.groups()
            found.append(Monitor(0, name, int(x), int(y), int(width), int(height), "+" in flags, 1.0))
    return found


def _monitors_linux() -> list[Monitor]:
    """Monitors via xrandr; under Wayland, Xwayland reports the logical layout."""
    if not shutil.which("xrandr"):
        return []
    try:
        result = subprocess.run(
            ["xrandr", "--listmonitors"], capture_output=True, text=True, timeout=5, env=host_env()
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_xrandr_monitors(result.stdout) if result.returncode == 0 else []


def _monitors_windows() -> list[Monitor]:
    import ctypes
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()  # physical pixels everywhere, like pyautogui's clicks
    except Exception:
        pass
    found: list[Monitor] = []
    proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def callback(hmon, hdc, rect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            name = info.szDevice.lstrip("\\.") or f"display-{len(found) + 1}"
            # Capture and mouse both use physical pixels here, so the scale is 1.
            found.append(
                Monitor(0, name, r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1), 1.0)
            )
        return True

    user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    return found


def _monitors_macos() -> list[Monitor]:
    import Quartz

    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err:
        return []
    names: dict[int, str] = {}
    try:
        import AppKit

        for screen in AppKit.NSScreen.screens():
            number = screen.deviceDescription().get("NSScreenNumber")
            if number is not None and screen.respondsToSelector_("localizedName"):
                names[int(number)] = str(screen.localizedName())
    except Exception:
        pass
    found = []
    for i, display in enumerate(ids[:count]):
        bounds = Quartz.CGDisplayBounds(display)  # logical points, same space as mouse events
        width, height = int(bounds.size.width), int(bounds.size.height)
        if not width or not height:
            continue
        scale = Quartz.CGDisplayPixelsWide(display) / width
        found.append(
            Monitor(
                0, names.get(int(display), f"display-{i + 1}"), int(bounds.origin.x), int(bounds.origin.y),
                width, height, bool(Quartz.CGDisplayIsMain(display)), round(scale, 3),
            )
        )
    return found


def detect_monitors(backend: Backend) -> tuple[list[Monitor], str]:
    """Raw monitor list and its source: xrandr, win32, quartz, or fallback (one screen)."""
    found: list[Monitor] = []
    source = "fallback"
    try:
        if sys.platform == "win32":
            found, source = _monitors_windows(), "win32"
        elif sys.platform == "darwin":
            found, source = _monitors_macos(), "quartz"
        else:
            found, source = _monitors_linux(), "xrandr"
    except Exception:
        found = []
    if not found:
        width, height = backend.size()
        return [Monitor(0, "screen", 0, 0, width, height, True, 1.0)], "fallback"
    return found, source


def list_monitors(backend: Backend) -> list[Monitor]:
    return order_monitors(detect_monitors(backend)[0])


def monitor_at(monitors: list[Monitor], x: int, y: int) -> Monitor | None:
    return next((m for m in monitors if m.contains(x, y)), None)


def resolve_monitor(spec: str, monitors: list[Monitor]) -> Monitor | None:
    """`all` -> None; `primary` or an index -> that monitor."""
    spec = str(spec).strip().lower()
    if spec == "all":
        return None
    if spec == "primary":
        return next(m for m in monitors if m.primary)
    try:
        index = int(spec)
    except ValueError as exc:
        raise CliError(
            f"--monitor must be all, primary or an index from 'ai-desktop monitors', not {spec!r}"
        ) from exc
    for m in monitors:
        if m.index == index:
            return m
    raise CliError(f"no monitor {index}; run: ai-desktop monitors")


class Capture(NamedTuple):
    image: Image.Image
    x: int  # global coordinates of the image's top-left pixel
    y: int
    scale: float  # physical / logical ratio of the capture before normalization
    monitor: str  # "all" or the monitor index


def _fit(img: Image.Image, width: int, height: int) -> tuple[Image.Image, float]:
    """Resize a capture to its logical size; returns the image and the original ratio."""
    scale = img.width / width if width else 1.0
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    return img, scale


def _capture_linux(backend: Backend, monitors: list[Monitor], target: Monitor | None) -> Capture:
    img, scale = _fit(grab_linux(), *backend.size())
    if target is None:
        return Capture(img, 0, 0, scale, "all")  # the X root starts at (0, 0)
    return Capture(img.crop(target.rect()), target.x, target.y, scale, str(target.index))


def _capture_windows(monitors: list[Monitor], target: Monitor | None) -> Capture:
    if target is None:
        left, top, right, bottom = union_rect(monitors)
        label = "all"
    else:
        left, top, right, bottom = target.rect()
        label = str(target.index)
    img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
    img, scale = _fit(img, right - left, bottom - top)
    return Capture(img, left, top, scale, label)


def _screencapture_rect(x: int, y: int, width: int, height: int) -> Image.Image:
    """One display region via macOS screencapture, run by us so nothing touches stdout."""
    fd, path = tempfile.mkstemp(prefix="ai_desktop_", suffix=".png")
    os.close(fd)
    try:
        # -R takes its value attached: a separate "-1920,0,..." would be read as a flag.
        result = subprocess.run(
            ["screencapture", "-x", f"-R{x},{y},{width},{height}", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or os.path.getsize(path) == 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise CliError(f"screencapture failed: {detail}; is Screen Recording granted?")
        with Image.open(path) as img:
            return img.convert("RGB")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _capture_macos(monitors: list[Monitor], target: Monitor | None) -> Capture:
    targets = monitors if target is None else [target]
    pieces = []
    for m in targets:
        img, scale = _fit(_screencapture_rect(m.x, m.y, m.width, m.height), m.width, m.height)
        pieces.append((m, img, scale))
    if target is not None:
        m, img, scale = pieces[0]
        return Capture(img, m.x, m.y, scale, str(m.index))
    left, top, right, bottom = union_rect(monitors)
    canvas = Image.new("RGB", (right - left, bottom - top), (0, 0, 0))
    for m, img, _ in pieces:
        canvas.paste(img, (m.x - left, m.y - top))
    return Capture(canvas, left, top, max(scale for _, _, scale in pieces), "all")


def capture_screen(backend: Backend, monitor: str = "all") -> Capture:
    """Capture all monitors (or one) at logical size.

    Image pixel (px, py) is screen point (x + px, y + py), so grid labels can show the
    coordinates `click` needs even for a monitor left of the primary (negative x).
    """
    monitors = list_monitors(backend)
    target = resolve_monitor(monitor, monitors)
    if sys.platform == "win32":
        return _capture_windows(monitors, target)
    if sys.platform == "darwin":
        return _capture_macos(monitors, target)
    return _capture_linux(backend, monitors, target)


# --- Screenshot store --------------------------------------------------------------------

# Only files the tool named itself are ever deleted: YYYYmmdd-HHMMSS-mmm[-N][-mM][-gridS].png
STORE_NAME_RE = re.compile(r"^\d{8}-\d{6}-\d{3}(-\d+)?(-m\d+)?(-grid\d+)?\.png$")


def home_dir() -> Path:
    """`~/.ai-desktop`, or `$AI_DESKTOP_HOME`: screenshots and logs live under it."""
    return Path(os.environ.get("AI_DESKTOP_HOME") or Path.home() / ".ai-desktop").expanduser()


def store_dir() -> Path:
    return home_dir() / "screenshots"


def new_screenshot_path(grid: bool, spacing: int, tag: str = "") -> Path:
    """A path in the store named after the local time, that doesn't exist yet.

    `tag` marks single-monitor captures (`-m1`); the grid suffix comes last.
    """
    now = datetime.now()
    stem = now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"
    suffix = tag + (f"-grid{spacing}" if grid else "")
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
        prune_logs(keep_days)
        marker.parent.mkdir(parents=True, exist_ok=True)
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


# --- Action log --------------------------------------------------------------------------

LOG_NAME_RE = re.compile(r"^actions-\d{8}\.jsonl$")
UNLOGGED_COMMANDS = ("log", "mcp")


def log_dir() -> Path:
    return home_dir() / "logs"


def log_enabled() -> bool:
    return os.environ.get("AI_DESKTOP_LOG", "").strip().lower() not in ("0", "false", "off", "no")


def log_text_enabled() -> bool:
    return os.environ.get("AI_DESKTOP_LOG_TEXT", "").strip().lower() in ("1", "true", "yes", "on")


def redact_log_fields(arg_dict: dict, result: dict) -> None:
    """Keep typed, pasted and copied text out of the log unless AI_DESKTOP_LOG_TEXT=1.

    `type --text`, `paste --text` and `clipboard set --text` become a character count;
    the text printed by `copy` and `clipboard get` is dropped (its `chars` stays).
    """
    if log_text_enabled():
        return
    if isinstance(arg_dict.get("text"), str):
        arg_dict["text_chars"] = len(arg_dict.pop("text"))
    result.pop("text", None)


def log_action(args: argparse.Namespace, code: int, payload: dict | None, started: float) -> None:
    """Append one JSON line for a finished command. Never raises and never prints."""
    try:
        if not log_enabled() or args.command in UNLOGGED_COMMANDS:
            return
        arg_dict = {
            k: v for k, v in vars(args).items() if k not in ("func", "command", "json", "backend")
        }
        result = {k: v for k, v in (payload or {}).items() if k not in ("ok", "command")}
        redact_log_fields(arg_dict, result)
        now = datetime.now()
        entry = {
            "ts": now.isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "via": LOG_VIA,
            "command": args.command,
            "args": arg_dict,
            "backend": getattr(args, "backend", None),
            "ok": bool(payload["ok"]) if payload else code == 0,
            "code": code,
            "ms": int((time.monotonic() - started) * 1000),
            "result": result,
        }
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f"actions-{now:%Y%m%d}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        return
    try:
        prune_if_due()  # hourly; a bad AI_DESKTOP_KEEP_* value is reported by the command itself
    except Exception:
        pass


def log_files() -> list[tuple[Path, os.stat_result]]:
    """Daily log files, newest first."""
    files = []
    try:
        with os.scandir(log_dir()) as entries:
            for entry in entries:
                if LOG_NAME_RE.match(entry.name) and entry.is_file(follow_symlinks=False):
                    files.append((Path(entry.path), entry.stat(follow_symlinks=False)))
    except FileNotFoundError:
        return []
    files.sort(key=lambda item: item[1].st_mtime, reverse=True)
    return files


def prune_logs(keep_days: float | None, now: float | None = None) -> list[Path]:
    """Delete daily log files older than `keep_days` (by mtime). `None` disables the rule."""
    if keep_days is None:
        return []
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    removed = []
    for path, st in log_files():
        if st.st_mtime < cutoff:
            try:
                os.remove(path)
            except OSError:
                continue
            removed.append(path)
    return removed


def read_log_entries(limit: int) -> list[dict]:
    """The most recent `limit` entries, newest first; unreadable lines are skipped."""
    entries: list[dict] = []
    for path, _ in log_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
                if len(entries) >= limit:
                    return entries
    return entries


def format_log_entry(entry: dict) -> str:
    stamp = str(entry.get("ts", ""))[11:19]
    result = entry.get("result") or {}
    if entry.get("ok"):
        outcome, detail = "ok", format_fields(result)
    else:
        outcome, detail = f"ERROR({entry.get('code', '?')})", str(result.get("error", ""))
    via = entry.get("via", "cli")
    return f"{stamp} {via} {entry.get('command')} {outcome} {entry.get('ms', '?')}ms {detail}".rstrip()


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
        monitors = list_monitors(self)
        if monitor_at(monitors, x, y) is None:
            where = "; ".join(
                f"{m.index} {m.name} at {m.x},{m.y} size {m.width}x{m.height}" for m in monitors
            )
            raise CliError(f"({x}, {y}) is on no monitor; monitors: {where}")


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


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong), ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int), ("data", ctypes.c_long * 5),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("client", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


class _X11:
    """Minimal libX11 wrapper for root-window properties and EWMH requests.

    pyautogui's python3-xlib can't read mutter's Xwayland auth cookie from some
    terminals; the system libX11 can, so screen size and window management go through it.
    """

    SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # SubstructureNotify | SubstructureRedirect

    def __init__(self) -> None:
        hint = "set AI_DESKTOP_SCREEN_SIZE=WIDTHxHEIGHT"
        try:
            lib = ctypes.CDLL("libX11.so.6")
        except OSError as exc:
            raise CliError(f"cannot load libX11 to talk to the X server; {hint}") from exc
        c = ctypes
        vp, ci, cl, ul = c.c_void_p, c.c_int, c.c_long, c.c_ulong
        lib.XOpenDisplay.restype = vp
        lib.XOpenDisplay.argtypes = [c.c_char_p]
        lib.XCloseDisplay.argtypes = [vp]
        lib.XDefaultScreen.argtypes = [vp]
        lib.XDefaultRootWindow.restype = ul
        lib.XDefaultRootWindow.argtypes = [vp]
        lib.XDisplayWidth.argtypes = [vp, ci]
        lib.XDisplayHeight.argtypes = [vp, ci]
        lib.XInternAtom.restype = ul
        lib.XInternAtom.argtypes = [vp, c.c_char_p, ci]
        lib.XGetWindowProperty.argtypes = [
            vp, ul, ul, cl, cl, ci, ul, c.POINTER(ul), c.POINTER(ci), c.POINTER(ul), c.POINTER(ul),
            c.POINTER(c.POINTER(c.c_ubyte)),
        ]
        lib.XFree.argtypes = [vp]
        lib.XGetWindowAttributes.argtypes = [vp, ul, c.POINTER(_XWindowAttributes)]
        lib.XTranslateCoordinates.argtypes = [
            vp, ul, ul, ci, ci, c.POINTER(ci), c.POINTER(ci), c.POINTER(ul),
        ]
        lib.XSendEvent.argtypes = [vp, ul, ci, cl, c.POINTER(_XEvent)]
        lib.XFlush.argtypes = [vp]
        # Xlib's default error handler exits the process on a BadWindow (a window that
        # closed between listing and querying); make such errors non-fatal.
        self._error_handler = c.CFUNCTYPE(ci, vp, vp)(lambda display, event: 0)
        lib.XSetErrorHandler(self._error_handler)
        self.lib = lib
        self.display = lib.XOpenDisplay(None)
        if not self.display:
            raise CliError(f"cannot open X display {os.environ.get('DISPLAY', '(unset)')!r}; {hint}")
        self.root = lib.XDefaultRootWindow(self.display)

    def __enter__(self) -> _X11:
        return self

    def __exit__(self, *exc) -> None:
        self.lib.XCloseDisplay(self.display)

    def root_size(self) -> tuple[int, int]:
        screen = self.lib.XDefaultScreen(self.display)
        return self.lib.XDisplayWidth(self.display, screen), self.lib.XDisplayHeight(self.display, screen)

    def atom(self, name: str) -> int:
        return self.lib.XInternAtom(self.display, name.encode(), 0)

    def get_property(self, window: int, name: str) -> tuple[int, bytes]:
        """(format, raw bytes) of a window property, or (0, b"") when it is absent."""
        c = ctypes
        actual_type, actual_format = c.c_ulong(), c.c_int()
        nitems, bytes_after = c.c_ulong(), c.c_ulong()
        prop = c.POINTER(c.c_ubyte)()
        ok = self.lib.XGetWindowProperty(
            self.display, window, self.atom(name), 0, 1 << 20, 0, 0,
            c.byref(actual_type), c.byref(actual_format), c.byref(nitems), c.byref(bytes_after),
            c.byref(prop),
        )
        if ok != 0 or not prop or actual_format.value == 0:
            return 0, b""
        fmt = actual_format.value
        item_size = c.sizeof(c.c_long) if fmt == 32 else fmt // 8  # 32-bit items arrive as C longs
        data = c.string_at(prop, nitems.value * item_size)
        self.lib.XFree(prop)
        return fmt, data

    def get_longs(self, window: int, name: str) -> list[int]:
        fmt, data = self.get_property(window, name)
        if fmt != 32:
            return []
        count = len(data) // ctypes.sizeof(ctypes.c_long)
        return list(struct.unpack(f"@{count}L", data))

    def get_text(self, window: int, name: str) -> str:
        fmt, data = self.get_property(window, name)
        return data.decode("utf-8", "replace").rstrip("\0") if fmt == 8 else ""

    def get_strings(self, window: int, name: str) -> list[str]:
        fmt, data = self.get_property(window, name)
        return [s.decode("utf-8", "replace") for s in data.split(b"\0") if s] if fmt == 8 else []

    def geometry(self, window: int) -> tuple[int, int, int, int, bool]:
        """(x, y, width, height, viewable) of a window in root coordinates."""
        c = ctypes
        attrs = _XWindowAttributes()
        if not self.lib.XGetWindowAttributes(self.display, window, c.byref(attrs)):
            raise CliError(f"window {window:#x} vanished")
        x, y, child = c.c_int(), c.c_int(), c.c_ulong()
        self.lib.XTranslateCoordinates(
            self.display, window, self.root, 0, 0, c.byref(x), c.byref(y), c.byref(child)
        )
        return x.value, y.value, attrs.width, attrs.height, attrs.map_state == 2  # IsViewable

    def send_client_message(self, window: int, name: str, data: list[int]) -> None:
        """An EWMH request about `window`, delivered to the window manager via the root."""
        event = _XEvent()
        msg = event.client
        msg.type = 33  # ClientMessage
        msg.send_event = 1
        msg.display = self.display
        msg.window = window
        msg.message_type = self.atom(name)
        msg.format = 32
        for i, value in enumerate(data[:5]):
            msg.data[i] = value
        self.lib.XSendEvent(self.display, self.root, 0, self.SUBSTRUCTURE_MASK, ctypes.byref(event))
        self.lib.XFlush(self.display)


def x_root_size() -> tuple[int, int]:
    """Size of the X root window. Under Wayland this is Xwayland's root, which spans the
    desktop in logical pixels."""
    with _X11() as x:
        return x.root_size()


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


CHROME_DESKTOP_IDS = (
    "google-chrome", "com.google.Chrome", "chromium", "chromium-browser", "org.chromium.Chromium",
)


def find_desktop_entry(ids: tuple[str, ...]) -> tuple[str, str] | None:
    """(id, path) of the first installed .desktop entry among `ids`, or None."""
    data_dirs = host_env().get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    folders = [Path.home() / ".local/share/applications"]
    folders += [Path(d) / "applications" for d in data_dirs.split(":") if d]
    for app_id in ids:
        for folder in folders:
            path = folder / f"{app_id}.desktop"
            if path.is_file():
                return app_id, str(path)
    return None


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


# --- Windows (the on-screen kind) --------------------------------------------------------


class Win(NamedTuple):
    id: str  # hex X window id / hWnd, or the CGWindow number on macOS
    pid: int
    app: str  # WM_CLASS, owner app name, or exe basename
    title: str
    x: int  # global logical coordinates, the same space `click` uses
    y: int
    width: int
    height: int
    active: bool
    minimized: bool


WAYLAND_WINDOWS_NOTE = (
    "X11/XWayland windows only: native Wayland windows cannot be listed or focused (GNOME "
    "has no public API for them); log in to the 'Ubuntu on Xorg' session for full window control"
)


def _windows_x11() -> list[Win]:
    with _X11() as x:
        stacking = x.get_longs(x.root, "_NET_CLIENT_LIST_STACKING") or x.get_longs(x.root, "_NET_CLIENT_LIST")
        active = (x.get_longs(x.root, "_NET_ACTIVE_WINDOW") or [0])[0]
        hidden, skip = x.atom("_NET_WM_STATE_HIDDEN"), x.atom("_NET_WM_STATE_SKIP_TASKBAR")
        found = []
        for window in reversed(stacking):  # EWMH lists bottom-to-top; we want the front first
            try:
                title = x.get_text(window, "_NET_WM_NAME") or x.get_text(window, "WM_NAME")
                if not title:
                    continue
                state = x.get_longs(window, "_NET_WM_STATE")
                if skip in state:
                    continue
                classes = x.get_strings(window, "WM_CLASS")
                app = classes[1] if len(classes) > 1 else (classes[0] if classes else "")
                pid = (x.get_longs(window, "_NET_WM_PID") or [0])[0]
                gx, gy, width, height, viewable = x.geometry(window)
                extents = x.get_longs(window, "_NET_FRAME_EXTENTS")  # left, right, top, bottom
                if len(extents) == 4:
                    gx, gy = gx - extents[0], gy - extents[2]
                    width += extents[0] + extents[1]
                    height += extents[2] + extents[3]
            except CliError:
                continue  # closed while we were looking
            found.append(
                Win(f"{window:#x}", pid, app, title, gx, gy, width, height, window == active,
                    hidden in state or not viewable)
            )
        return found


def _activate_x11(win: Win) -> dict:
    window = int(win.id, 16)
    with _X11() as x:
        # Source indication 2 = a request from a pager/agent, which window managers honour.
        x.send_client_message(window, "_NET_ACTIVE_WINDOW", [2, 0, 0, 0, 0])
        time.sleep(0.2)
        active = (x.get_longs(x.root, "_NET_ACTIVE_WINDOW") or [0])[0]
    return {"activated": active == window}


def _win32_process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        name = os.path.basename(buf.value)
        return name[:-4] if name.lower().endswith(".exe") else name
    finally:
        kernel32.CloseHandle(handle)


def _windows_win32() -> list[Win]:
    from ctypes import wintypes

    import pygetwindow

    user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    foreground = user32.GetForegroundWindow()
    found = []
    for win in pygetwindow.getAllWindows():  # Z-order, front first
        hwnd = int(win._hWnd)
        title = win.title
        if not title or user32.GetWindowLongW(hwnd, -20) & 0x80:  # untitled or WS_EX_TOOLWINDOW
            continue
        cloaked = wintypes.DWORD(0)
        if dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            continue  # UWP shells and windows on other virtual desktops
        rect = wintypes.RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))  # DWM bounds exclude invisible borders
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(
            Win(f"{hwnd:#x}", pid.value, _win32_process_name(pid.value), title, rect.left, rect.top,
                rect.right - rect.left, rect.bottom - rect.top, hwnd == foreground, bool(win.isMinimized))
        )
    return found


def _activate_win32(win: Win) -> dict:
    user32 = ctypes.windll.user32
    hwnd = int(win.id, 16)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() != hwnd:
        # Windows won't hand the foreground to a process that got no input lately; a
        # synthetic ALT tap counts as input and lifts that lock.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    return {"activated": user32.GetForegroundWindow() == hwnd}


def _windows_macos() -> list[Win]:
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    infos = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    front_pid = None
    try:
        import AppKit

        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        front_pid = int(app.processIdentifier()) if app is not None else None
    except Exception:
        pass
    found = []
    seen_active = False
    for info in infos:  # already front-to-back
        if info.get("kCGWindowLayer", 0) != 0 or info.get("kCGWindowAlpha", 1) <= 0:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
        if width < 50 or height < 50:
            continue
        owner = str(info.get("kCGWindowOwnerName") or "")
        title = str(info.get("kCGWindowName") or "")  # empty for other apps without Screen Recording
        if not title and not owner:
            continue
        pid = int(info.get("kCGWindowOwnerPID", 0))
        active = pid == front_pid and not seen_active
        seen_active = seen_active or active
        found.append(
            Win(str(info.get("kCGWindowNumber", "")), pid, owner, title, int(bounds.get("X", 0)),
                int(bounds.get("Y", 0)), width, height, active, False)
        )
    return found


def _activate_macos(win: Win) -> dict:
    import AppKit

    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(win.pid)
    if app is None:
        raise CliError(f"no running app with pid {win.pid}")
    result = {"activated": bool(app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))}
    if win.title:
        # Raise that one window too (best effort; System Events asks for Automation consent once).
        script = (
            'tell application "System Events" to tell (first process whose unix id is %d) '
            'to perform action "AXRaise" of (first window whose name is %s)'
            % (win.pid, json.dumps(win.title, ensure_ascii=False))
        )
        try:
            done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
            result["raised"] = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            result["raised"] = False
    return result


def list_windows() -> tuple[list[Win], bool]:
    """Windows front-to-back, and whether the list is partial (Wayland: X11 windows only)."""
    if sys.platform == "win32":
        return _windows_win32(), False
    if sys.platform == "darwin":
        return _windows_macos(), False
    return _windows_x11(), is_wayland()


def select_window(
    windows: list[Win],
    title: str | None = None,
    app: str | None = None,
    pid: int | None = None,
    id: str | None = None,
) -> tuple[Win, int]:
    """Pick one window: substring matches for title (or app name) and app, exact for pid/id.

    An exact title match wins; otherwise the front-most match. Returns (window, matches).
    """

    def matches(w: Win) -> bool:
        if id is not None and w.id.lower() != str(id).lower():
            return False
        if pid is not None and w.pid != pid:
            return False
        if app is not None and app.lower() not in w.app.lower():
            return False
        if title is not None:
            needle = title.lower()
            if needle not in w.title.lower() and needle not in w.app.lower():
                return False
        return True

    found = [w for w in windows if matches(w)]
    if not found:
        wanted = ", ".join(
            f"{k}={v!r}" for k, v in (("title", title), ("app", app), ("pid", pid), ("id", id))
            if v is not None
        )
        raise CliError(f"no window matches {wanted}; run: ai-desktop windows")
    if title is not None:
        exact = [w for w in found if w.title.lower() == title.lower()]
        if exact:
            return exact[0], len(found)
    return found[0], len(found)


def activate_window(win: Win) -> dict:
    if sys.platform == "win32":
        return _activate_win32(win)
    if sys.platform == "darwin":
        return _activate_macos(win)
    return _activate_x11(win)


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

    In JSON (or quiet) mode the text is the "text" field instead.
    """
    if JSON_OUTPUT or QUIET:
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
    return (
        "warn",
        "none found; using Pillow's X11 capture",
        "sudo apt install gnome-screenshot for a more reliable capture",
    )


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


def _check_windows():
    windows, partial = list_windows()
    how = {"win32": "Win32", "darwin": "Quartz"}.get(sys.platform, "libX11 EWMH")
    if partial:
        return "warn", f"{how}: {len(windows)} X11/XWayland window(s)", WAYLAND_WINDOWS_NOTE
    hint = ""
    if sys.platform == "darwin":
        hint = (
            "other apps' titles need Screen Recording; focus needs Accessibility and a "
            "one-time Automation consent for System Events"
        )
    return "ok", f"{how}: {len(windows)} window(s)", hint


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

        def _monitors():
            found, source = detect_monitors(backend)
            desc = ", ".join(
                f"{m.name} {m.width}x{m.height}+{m.x}+{m.y}" + (" (primary)" if m.primary else "")
                for m in order_monitors(found)
            )
            if source == "fallback":
                return (
                    "warn",
                    f"1 monitor assumed: {desc}",
                    "the display layout could not be queried (xrandr missing?); "
                    "a second monitor would be invisible to screenshot and click",
                )
            return "ok", f"{len(found)} via {source}: {desc}"

        checks.append(run_check("monitors", _monitors))

        def _capture():
            cap = capture_screen(backend)
            return (
                "ok",
                f"{cap.image.width}x{cap.image.height} at {cap.x},{cap.y}, scale {cap.scale:g} "
                "(the screen may flash once)",
            )

        checks.append(run_check("screenshot", _capture))
    checks.append(run_check("windows", _check_windows, soft=True))
    checks.append(run_check("clipboard", _check_clipboard))
    checks.append(run_check("chrome", _check_chrome, soft=True))
    checks.append(run_check("store", _check_store))
    return checks


# --- Commands ----------------------------------------------------------------------------


def cmd_screenshot(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    cap = capture_screen(backend, args.monitor)
    img = cap.image
    if args.grid:
        img = draw_grid(img, args.spacing, origin=(cap.x, cap.y))
    tag = "" if cap.monitor == "all" else f"-m{cap.monitor}"
    path = save_image(img, args.output or new_screenshot_path(args.grid, args.spacing, tag))
    # Prune after saving: the capture has already succeeded, and the new file is never a
    # candidate. Explicit -o paths live outside the store and are never pruned.
    pruned = prune_if_due() if args.output is None else None
    status(
        "screenshot",
        path=path,
        width=img.width,
        height=img.height,
        x=cap.x,
        y=cap.y,
        monitor=cap.monitor,
        spacing=args.spacing if args.grid else "none",
        scale=round(cap.scale, 3),
        backend=backend.name,
        pruned="skipped" if pruned is None else len(pruned),
    )


def cmd_monitors(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    found, source = detect_monitors(backend)
    monitors = order_monitors(found)
    left, top, right, bottom = union_rect(monitors)
    status(
        "monitors",
        count=len(monitors),
        primary=next(m.index for m in monitors if m.primary),
        virtual_x=left,
        virtual_y=top,
        virtual_width=right - left,
        virtual_height=bottom - top,
        source=source,
        monitors=[m._asdict() for m in monitors],
    )
    if text_mode():
        for m in monitors:
            primary = " primary" if m.primary else ""
            print(f"{m.index} {m.name} {m.x},{m.y} {m.width}x{m.height}{primary} scale={m.scale:g}")


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
        if text_mode():
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
        logs_removed = prune_logs(keep_days)
        try:
            folder.mkdir(parents=True, exist_ok=True)
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
            logs_deleted=len(logs_removed),
        )


def cmd_log(args: argparse.Namespace) -> None:
    entries = read_log_entries(args.limit)
    status("log", path=str(log_dir()), count=len(entries), entries=entries)
    if text_mode():
        for entry in entries:
            print(format_log_entry(entry))


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
    pos = backend.position()
    hit = monitor_at(list_monitors(backend), *pos) if pos else None
    x, y = pos or ("unknown", "unknown")
    status(
        "position",
        x=x,
        y=y,
        monitor=hit.index if hit else "none",
        width=width,
        height=height,
        backend=backend.name,
    )


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
    launcher = "exec"
    if sys.platform.startswith("linux") and not args.profile:
        # Going through the desktop entry gives the launch an activation token, so the
        # window manager may bring the new window to the front instead of opening it
        # behind the terminal. Desktop entries can't carry --profile-directory.
        entry = find_desktop_entry(CHROME_DESKTOP_IDS)
        url = [args.url] if args.url else []
        if entry and shutil.which("gtk-launch"):
            argv, launcher = ["gtk-launch", entry[0], *url], "gtk-launch"
        elif entry and shutil.which("gio"):
            argv, launcher = ["gio", "launch", entry[1], *url], "gio"
    spawn_detached(argv)
    status(
        "launch-chrome",
        exe=exe,
        user_data_dir=user_data or "default",
        profile=args.profile or "last-used",
        url=args.url or "none",
        launcher=launcher,
    )


def cmd_windows(args: argparse.Namespace) -> None:
    windows, partial = list_windows()
    fields: dict = dict(count=len(windows), partial=partial, windows=[w._asdict() for w in windows])
    if partial:
        fields["note"] = WAYLAND_WINDOWS_NOTE
    status("windows", **fields)
    if text_mode():
        for w in windows:
            flags = (" active" if w.active else "") + (" minimized" if w.minimized else "")
            title = json.dumps(w.title, ensure_ascii=False)
            print(f"{w.id} pid={w.pid} {w.x},{w.y} {w.width}x{w.height}{flags} {w.app or '-'} {title}")


def cmd_focus(args: argparse.Namespace) -> None:
    if args.title is None and args.app is None and args.pid is None and args.id is None:
        raise CliError("give --title, --app, --pid or --id (see: ai-desktop windows)")
    windows, _ = list_windows()
    win, matches = select_window(windows, title=args.title, app=args.app, pid=args.pid, id=args.id)
    result = activate_window(win)
    fields = dict(id=win.id, pid=win.pid, app=win.app, title=win.title, matches=matches, **result)
    if not result.get("activated"):
        raise CliError(f"window {win.id} ({win.title!r}) did not come to the front", **fields)
    status("focus", **fields)


def cmd_doctor(args: argparse.Namespace) -> None:
    checks = run_doctor(args.backend)
    counts = {state: sum(1 for c in checks if c.state == state) for state in ("ok", "warn", "fail")}
    if text_mode():
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
        "--monitor",
        default="all",
        help="all (default: every monitor in one image), primary, or an index from 'ai-desktop monitors'",
    )
    p.add_argument(
        "-o",
        "--output",
        help="output path (default: a timestamped file in the screenshot store, "
        "see 'screenshots dir'; files given here are never auto-deleted)",
    )
    p.set_defaults(func=cmd_screenshot)

    p = add("monitors", help="list monitors with their global coordinates")
    p.set_defaults(func=cmd_monitors)

    p = add("windows", help="list open windows, front to back, with their global coordinates")
    p.set_defaults(func=cmd_windows)

    p = add("focus", help="bring a window to the front")
    p.add_argument("--title", help="case-insensitive part of the window title (or app name)")
    p.add_argument("--app", help="case-insensitive part of the app name (WM_CLASS / owner / exe)")
    p.add_argument("--pid", type=int, help="process id")
    p.add_argument("--id", help="window id as printed by 'windows'")
    p.set_defaults(func=cmd_focus)

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

    p = add("log", help="show recent entries from the action log, newest first")
    p.add_argument("--limit", type=_positive_int, default=20, help="how many entries (default 20)")
    p.set_defaults(func=cmd_log)
    return parser


def execute(argv: list[str] | None = None, quiet: bool = False) -> tuple[int, dict | None]:
    """Run one invocation and return (exit code, result payload).

    With quiet=True nothing is printed and bad arguments come back as a payload instead
    of a SystemExit; the MCP server runs commands this way. The action log entry is
    written here whatever the outcome.
    """
    global JSON_OUTPUT, QUIET, CURRENT_COMMAND, LAST_PAYLOAD
    saved = (JSON_OUTPUT, QUIET, CURRENT_COMMAND)
    started = time.monotonic()
    args = None
    code = 0
    LAST_PAYLOAD = None
    QUIET = quiet
    try:
        if quiet:
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    args = build_parser().parse_args(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
                text = buffer.getvalue().strip()
                if code == 0:  # --help / --version
                    LAST_PAYLOAD = {"ok": True, "command": None, "text": text}
                else:
                    LAST_PAYLOAD = {"ok": False, "command": None, "error": f"bad arguments: {text}", "code": code}
                return code, LAST_PAYLOAD
        else:
            args = build_parser().parse_args(argv)
        args.backend = getattr(args, "backend", None) or os.environ.get("AI_DESKTOP_BACKEND", "auto")
        JSON_OUTPUT = bool(getattr(args, "json", False)) or os.environ.get(
            "AI_DESKTOP_JSON", ""
        ).strip().lower() in ("1", "true", "yes")
        CURRENT_COMMAND = args.command
        try:
            args.func(args)
        except CliError as exc:
            code = fail(str(exc), EXIT_ERROR, **exc.fields)
        except OSError as exc:
            code = fail(str(exc), EXIT_ERROR)
        except KeyboardInterrupt:
            code = fail("interrupted", 130)
        except Exception as exc:
            gui = sys.modules.get("pyautogui")
            if gui is not None and isinstance(exc, getattr(gui, "FailSafeException", ())):
                code = fail(
                    "failsafe triggered: the mouse is in a screen corner, so the action was "
                    "aborted. Move it away from the corners to continue.",
                    EXIT_FAILSAFE,
                )
            else:
                code = EXIT_ERROR
                LAST_PAYLOAD = {
                    "ok": False, "command": args.command,
                    "error": f"{type(exc).__name__}: {exc}", "code": code,
                }
                raise
        return code, LAST_PAYLOAD
    finally:
        if args is not None:
            log_action(args, code, LAST_PAYLOAD, started)
        JSON_OUTPUT, QUIET, CURRENT_COMMAND = saved


def main(argv: list[str] | None = None) -> int:
    return execute(argv)[0]


if __name__ == "__main__":
    sys.exit(main())
