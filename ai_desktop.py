#!/usr/bin/env python3
"""AI Desktop Controller: gridded screenshots plus mouse and keyboard control from the CLI.

An agent loop looks like: `screenshot --grid` -> read the image -> `click`/`type`/`key`
-> `screenshot` again. Every command prints one `OK <command> key=value ...` line on
stdout, or `ERROR <message>` on stderr with a non-zero exit code. With `--json` each
command prints one JSON object instead. Mouse moves follow a random curved path and typing
pauses like a person between keys; `--instant` turns that off. Actions take `--seen
latest` (abort if the screen changed since that screenshot) and `--shot` (return the next
screenshot); `wait --stable` waits for the screen to settle, `run` does several steps in one
process, and `screenshot --zoom X,Y` shows a small area enlarged with screen coordinates.

WARNING: whoever runs this (typically an AI agent) gets full control of the desktop and
of every account that is logged in on it. See README.md before using it.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import functools
import io
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import types
import uuid
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageGrab, PngImagePlugin

__version__ = "0.6.0.dev0"

DEFAULT_SPACING = 100
MIN_SPACING = 10
DEFAULT_MAX_WIDTH = 1920  # saved screenshots wider than this are shrunk (AI_DESKTOP_MAX_WIDTH)
MAX_WIDTH_OVERRIDE: int | None = None  # `ai-desktop mcp --max-width`, for every screenshot it takes
MIN_MAX_WIDTH = 200  # a smaller --max-width would make the labels absurdly large
ZOOM_RADIUS = 200
ZOOM_FACTOR = 3
ZOOM_MAX_EDGE = 4096  # a zoom is never drawn bigger than this, even with --max-width 0

# Screenshot store: the tool's own folder, pruned by age and total size (see "Screenshot store").
DEFAULT_KEEP_DAYS = 30
DEFAULT_KEEP_MB = 2048
PRUNE_INTERVAL = 3600  # seconds between automatic prunes
PRUNE_MARKER = ".last-prune"
META_KEY = "ai-desktop"  # PNG text chunk with the image's origin, scale and pointer

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
    """A user-facing error, reported as `ERROR <message>` with exit code `code` (default 1).

    Extra fields are included in the JSON object when --json is on (doctor uses this for
    its rows).
    """

    def __init__(self, message: str, code: int = EXIT_ERROR, **fields) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


_FIELD_ESCAPES = {'"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def format_fields(fields: dict) -> str:
    """`key=value` pairs on one line; lists/dicts are skipped. A value that is empty or has
    whitespace, a quote, `=` or a control character is quoted, with quotes and control
    characters escaped, so a newline or quote in it (a tool's error, a window title) can't
    break the line. Backslashes stay as they are: Windows paths must remain usable."""
    parts = []
    for key, value in fields.items():
        if isinstance(value, (list, dict)):
            continue
        text = str(value)
        if not text or any(c.isspace() or c in '"=' or ord(c) < 32 for c in text):
            text = '"' + "".join(
                _FIELD_ESCAPES.get(c, c if ord(c) >= 32 else f"\\x{ord(c):02x}") for c in text
            ) + '"'
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
    """A Linux Wayland session. XDG_SESSION_TYPE says so, but a compositor started from a
    text console (sway without a display manager) inherits "tty": then WAYLAND_DISPLAY
    decides, unless the session is explicitly x11."""
    if not sys.platform.startswith("linux"):
        return False
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    return session == "wayland" or (session != "x11" and bool(os.environ.get("WAYLAND_DISPLAY")))


def desktop_name() -> str:
    """gnome, kde, wlroots (Sway, Hyprland and the like) or other."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gnome" in desktop:
        return "gnome"
    if "kde" in desktop:
        return "kde"
    if any(name in desktop for name in ("sway", "hyprland", "river", "wayfire", "labwc", "niri", "wlroots")):
        return "wlroots"
    return "other"


# --- Grid overlay ------------------------------------------------------------------------

# Two-tone lines, a dark outline under a magenta core, stay visible on white, pink, dark
# and busy content alike.
LINE_COLOR = (255, 0, 255, 150)
LINE_OUTLINE = (40, 0, 40, 110)
LABEL_BG = (0, 0, 0, 190)
LABEL_FG = (255, 255, 255, 255)
LABEL_PAD = 2
TICK_LEN = 7  # unlabelled half-spacing ticks on each line, px
LANDMARK_MIN = 400  # interior "x,y" labels at most this often, in saved-image px
# Vision models shrink images to about this long edge before reading them; labels are
# sized so their digits stay legible there.
MODEL_VIEW_EDGE = 1568
LABEL_VIEW_PX = 16
MAX_AUTO_LINES = 24  # automatic spacing draws at most this many lines along the long edge
AUTO_SPACINGS = (100, 200, 250, 500)
ZOOM_SPACINGS = (10, 20, 25, 50, 100)
FONT_CANDIDATES = (
    "DejaVuSans.ttf",
    "Arial.ttf",
    "arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    r"C:\Windows\Fonts\arial.ttf",
)


@functools.lru_cache(maxsize=64)
def load_font(size: int) -> ImageFont.ImageFont:
    """Return a readable TrueType font, falling back to Pillow's built-in font (cached:
    each screenshot asks for the same few sizes)."""
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


def _text_w(font, text: str) -> int:
    left, _, right, _ = font.getbbox(text)
    return right - left


def _draw_label(
    draw: ImageDraw.ImageDraw, text: str, font, left: int, top: int, pad: int = LABEL_PAD
) -> None:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.rectangle([left, top, left + r - l + 2 * pad, top + b - t + 2 * pad], fill=LABEL_BG)
    draw.text((left + pad - l, top + pad - t), text, font=font, fill=LABEL_FG)


def grid_lines(
    length_px: int, origin: int, spacing: int, pixel_scale: float = 1.0
) -> list[tuple[int, int]]:
    """(local px, global coordinate) of each grid line along one image axis.

    Lines sit on global multiples of `spacing`. `origin` is the global coordinate of local
    pixel 0 and `pixel_scale` the image px per screen px (3 for a 3x zoom). A line at
    local 0 is left out so it doesn't hide the image's edge.
    """
    lines = []
    g = math.ceil(origin / spacing) * spacing
    end = origin + length_px / pixel_scale
    while g < end:
        local = round((g - origin) * pixel_scale)
        if 0 < local < length_px:
            lines.append((local, g))
        g += spacing
    return lines


def _half_lines(length_px: int, origin: int, spacing: int, pixel_scale: float) -> list[int]:
    """Local px of the half-spacing positions between grid lines, for ticks."""
    out = []
    g = math.floor(origin / spacing) * spacing + spacing / 2
    end = origin + length_px / pixel_scale
    while g < end:
        local = round((g - origin) * pixel_scale)
        if 0 < local < length_px:
            out.append(local)
        g += spacing
    return out


def grid_font_px(saved_long_edge: float) -> int:
    """Label font size for an image saved with this long edge: LABEL_VIEW_PX once a
    vision model has shrunk it to MODEL_VIEW_EDGE, and never smaller than that."""
    return max(LABEL_VIEW_PX, round(LABEL_VIEW_PX * saved_long_edge / MODEL_VIEW_EDGE))


def view_scale(width: int, max_width: int | None) -> float:
    """The factor an image `width` px wide is shrunk by to fit `max_width` (0/None: never)."""
    return min(1.0, max_width / width) if max_width and width > 0 else 1.0


def label_metrics(font, origin: tuple[int, int], logical_size: tuple[float, float], pad: int) -> tuple[int, int]:
    """(width, height) of the box around the widest coordinate label of an image.

    Negative coordinates are wider than positive ones, so the real extremes are measured.
    """
    ox, oy = origin
    w, h = logical_size
    text_w = max(_text_w(font, str(round(v))) for v in (ox, ox + w, oy, oy + h))
    _, top, _, bottom = font.getbbox("0123456789-")
    return text_w + 2 * pad, bottom - top + 2 * pad


def auto_spacing(
    long_edge: float,
    box_w: float,
    k: float = 1.0,
    pixel_scale: float = 1.0,
    candidates: tuple[int, ...] = AUTO_SPACINGS,
) -> int:
    """The smallest candidate spacing whose labels fit between lines and that draws at
    most MAX_AUTO_LINES lines along the long edge (`long_edge` in screen px, `box_w` in
    drawn px, `k` the label enlargement)."""
    for spacing in candidates:
        if spacing * pixel_scale >= box_w + 8 * k and long_edge / spacing <= MAX_AUTO_LINES:
            return spacing
    return candidates[-1]


def label_every(box: float, k: float, spacing: int, pixel_scale: float = 1.0) -> int:
    """Label every n-th line so labels `box` drawn px long never touch."""
    return max(1, math.ceil((box + 8 * k) / (spacing * pixel_scale)))


def grid_font(size: tuple[int, int], k: float = 1.0):
    """The label font for an image of `size` drawn px that will be shrunk by 1/k."""
    return load_font(round(grid_font_px(max(size) / k) * k))


def draw_grid(
    img: Image.Image,
    spacing: int = DEFAULT_SPACING,
    origin: tuple[int, int] = (0, 0),
    label_scale: float = 1.0,
    *,
    pixel_scale: float = 1.0,
    landmarks: bool = True,
    ticks: bool = True,
) -> Image.Image:
    """Return a copy of `img` with lines every `spacing` screen px, labelled with screen
    coordinates.

    `origin` is the global coordinate of the image's top-left pixel, so a capture that
    starts at (-1920, 0) gets labels -1900, -1800, ... and lines sit on global multiples of
    `spacing`. `pixel_scale` is image px per screen px (a zoom). `label_scale` enlarges
    lines and labels for an image that will be shrunk by that factor afterwards.

    Lines are a magenta core over a dark outline, with short ticks halfway between them.
    X labels sit at the top and bottom of each vertical line, Y labels at the left and
    right of each horizontal line, and "x,y" landmarks mark some interior crossings, all
    as white text on a dark box. Labels are thinned out by their global value, so the
    same coordinates are labelled whatever the capture's origin.
    """
    base = img.convert("RGBA")
    width, height = base.size
    ox, oy = origin
    k, scale = label_scale, pixel_scale
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    xs = grid_lines(width, ox, spacing, scale)
    ys = grid_lines(height, oy, spacing, scale)

    core = max(1, round(k))
    edge = max(1, round(k))
    # Every outline first, then the cores on top, so line crossings stay magenta.
    for line_w, color in ((core + 2 * edge, LINE_OUTLINE), (core, LINE_COLOR)):
        for x, _ in xs:
            draw.line([(x, 0), (x, height - 1)], fill=color, width=line_w)
        for y, _ in ys:
            draw.line([(0, y), (width - 1, y)], fill=color, width=line_w)

    if ticks and spacing * scale >= 20:
        half = max(2, round(TICK_LEN * k / 2))
        for x in _half_lines(width, ox, spacing, scale):
            for y, _ in ys:
                draw.line([(x, y - half), (x, y + half)], fill=LINE_COLOR, width=core)
        for y in _half_lines(height, oy, spacing, scale):
            for x, _ in xs:
                draw.line([(x - half, y), (x + half, y)], fill=LINE_COLOR, width=core)

    font = grid_font((width, height), k)
    pad = max(LABEL_PAD, round(LABEL_PAD * k))
    box_w, box_h = label_metrics(font, origin, (width / scale, height / scale), pad)
    every_x = label_every(box_w, k, spacing, scale)
    every_y = label_every(box_h, k, spacing, scale)
    gap = max(4, round(4 * k))
    side = box_w + gap  # the columns at the left and right edges hold the y labels
    band = box_h + gap  # the rows at the top and bottom edges hold the x labels
    far_x = width > 4 * side  # room for a second column of labels on the right
    far_y = height > 4 * band  # and a second row at the bottom

    for x, g in xs:
        if (g // spacing) % every_x:
            continue
        label = str(g)
        w = _text_w(font, label) + 2 * pad
        left = x - w // 2
        if left < side or left + w > width - (side if far_x else 0):
            continue
        _draw_label(draw, label, font, left, 1, pad)
        if far_y:
            _draw_label(draw, label, font, left, height - box_h - 1, pad)
    for y, g in ys:
        if (g // spacing) % every_y:
            continue
        top = y - box_h // 2
        if top < band or top + box_h > height - (band if far_y else 0):
            continue
        label = str(g)
        _draw_label(draw, label, font, 1, top, pad)
        if far_x:
            _draw_label(draw, label, font, width - _text_w(font, label) - 2 * pad - 1, top, pad)

    if landmarks and xs and ys:
        # Interior "x,y" labels on crossings every `step` screen px: at least
        # LANDMARK_MIN saved-image px apart, and at least two lines apart.
        step = math.ceil(max(LANDMARK_MIN * k / scale, 2 * spacing) / spacing) * spacing
        offset = core + max(3, round(3 * k))  # beside the crossing, which stays a line pixel
        for x, gx in xs:
            if gx % step:
                continue
            for y, gy in ys:
                if gy % step:
                    continue
                label = f"{gx},{gy}"
                left, top = x + offset, y + offset
                right = left + _text_w(font, label) + 2 * pad
                if left < side or right > width - side or top < band or top + box_h > height - band:
                    continue
                _draw_label(draw, label, font, left, top, pad)

    return Image.alpha_composite(base, overlay).convert("RGB")


def _tighten(path: Path, mode: int) -> None:
    """Remove group and other access from a file or folder the tool owns (POSIX only)."""
    if os.name != "posix":
        return
    try:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            os.chmod(path, mode)
    except OSError:
        pass  # not ours, or gone: never fail a command over this


def private_dir(path: Path) -> Path:
    """Create a folder for the tool's own files. Screenshots of the whole desktop and the
    action log must not be readable by other local users, so every folder from home_dir()
    down to `path` is made 0700. The home folder itself only when it is the default
    `.ai-desktop` or was created just now: AI_DESKTOP_HOME may point at the user's own folder."""
    path = Path(path)
    home = home_dir()
    created_home = not home.exists()
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "posix":
        return path
    if created_home or home.name == ".ai-desktop":
        _tighten(home, 0o700)
    try:
        rel = path.resolve().relative_to(home.resolve())
    except (ValueError, OSError):
        return path
    folder = home
    for part in rel.parts:
        folder = folder / part
        _tighten(folder, 0o700)
    return path


def private_file(path: str | Path, mode: str = "w", encoding: str | None = "utf-8"):
    """Open a file for writing that only its owner can read (0600 on POSIX)."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode.startswith("a") else os.O_TRUNC)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    _tighten(Path(path), 0o600)  # an existing file keeps its mode under os.open
    return os.fdopen(fd, mode, encoding=None if "b" in mode else encoding)


def save_image(img: Image.Image, output: str | Path, meta: dict | None = None, **save_kwargs) -> str:
    """Save `img`, readable only by its owner; a PNG also gets `meta` as an `ai-desktop`
    text chunk (origin, scale, ...)."""
    path = Path(output).expanduser().resolve()
    try:
        path.parent.relative_to(home_dir().expanduser().resolve())
        private_dir(path.parent)
    except ValueError:
        path.parent.mkdir(parents=True, exist_ok=True)  # the caller's own folder (-o): left as is
    fmt = Image.registered_extensions().get(path.suffix.lower())
    if fmt is None:
        raise CliError(f"cannot save {path}: unknown file extension {path.suffix or '(none)'!r}")
    if meta is not None and fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_text(META_KEY, json.dumps(meta))
        save_kwargs["pnginfo"] = info
    try:
        with private_file(path, "wb") as fh:
            img.save(fh, format=fmt, **save_kwargs)
    except ValueError as exc:  # a mode the format can't store
        raise CliError(f"cannot save {path}: {exc}") from exc
    return str(path)


def read_image_meta(path: str | Path) -> dict | None:
    """The `ai-desktop` chunk that save_image wrote, or None (not a PNG, no chunk, unreadable)."""
    try:
        with Image.open(path) as img:
            raw = getattr(img, "text", {}).get(META_KEY)
        data = json.loads(raw) if raw else None
    except (OSError, ValueError, SyntaxError):
        return None
    return data if isinstance(data, dict) else None


# --- Screen capture ----------------------------------------------------------------------

# Wayland needs one of these (pyautogui can't see native windows); on X11 they are
# preferred too, because we run them ourselves with host_env().
LINUX_SCREENSHOT_TOOLS = {
    "gnome-screenshot": ["gnome-screenshot", "-f"],
    "grim": ["grim"],
    "spectacle": ["spectacle", "-b", "-n", "-o"],
}
# Which tools can work in which session, best first. grim needs wlroots' screencopy, and
# gnome-screenshot captures only Xwayland's empty root on other Wayland desktops.
SCREENSHOT_TOOL_ORDER = {
    ("wayland", "gnome"): ("gnome-screenshot",),
    ("wayland", "kde"): ("spectacle",),
    ("wayland", "wlroots"): ("grim", "spectacle"),
    ("wayland", "other"): ("grim", "spectacle", "gnome-screenshot"),
    ("x11", "kde"): ("spectacle", "gnome-screenshot"),
}
X11_TOOL_ORDER = ("gnome-screenshot", "spectacle")
# The flag that makes each tool draw the mouse pointer into the capture.
CURSOR_FLAGS = {"gnome-screenshot": ["-p"], "grim": ["-c"], "spectacle": ["-p"]}
_LAST_CAPTURE_TOOL: str | None = None  # the tool that made the latest Linux capture


def screenshot_tools() -> list[list[str]]:
    """The installed screenshot tools that suit this session, best first."""
    session = "wayland" if is_wayland() else "x11"
    order = SCREENSHOT_TOOL_ORDER.get((session, desktop_name()), X11_TOOL_ORDER if session == "x11" else ())
    return [LINUX_SCREENSHOT_TOOLS[name] for name in order if shutil.which(name)]


def find_screenshot_tool() -> list[str] | None:
    tools = screenshot_tools()
    return tools[0] if tools else None


def linux_cursor_supported() -> bool:
    tool = _LAST_CAPTURE_TOOL or (find_screenshot_tool() or [None])[0]
    return tool in CURSOR_FLAGS


def _run_screenshot_tool(tool: list[str], cursor: bool) -> Image.Image:
    fd, path = tempfile.mkstemp(prefix="ai_desktop_", suffix=".png")
    os.close(fd)
    # The flag goes right after the program name: the tools' last option takes the path.
    flags = CURSOR_FLAGS.get(tool[0], []) if cursor else []
    try:
        try:
            result = subprocess.run(
                [tool[0], *flags, *tool[1:], path], capture_output=True, text=True, timeout=30, env=host_env()
            )
        except subprocess.TimeoutExpired as exc:
            raise CliError(f"{tool[0]} did not finish within {exc.timeout:g} s") from exc
        if result.returncode != 0 or os.path.getsize(path) == 0:
            detail = result.stderr.strip().splitlines()[-1:] or [f"exit code {result.returncode}"]
            raise CliError(f"{tool[0]} failed: {detail[0]}")
        with Image.open(path) as img:
            return img.convert("RGB")
    finally:
        os.remove(path)


def grab_linux(cursor: bool = False) -> Image.Image:
    """Capture the whole X root / Wayland desktop; `cursor` asks the tool to draw the pointer.
    Each suitable tool is tried in turn, then on X11 Pillow's own capture."""
    global _LAST_CAPTURE_TOOL
    _LAST_CAPTURE_TOOL = None
    errors = []
    for tool in screenshot_tools():
        try:
            img = _run_screenshot_tool(tool, cursor)
        except CliError as exc:
            errors.append(str(exc))
            continue
        _LAST_CAPTURE_TOOL = tool[0]
        return img
    if is_wayland():
        if errors:
            raise CliError("cannot capture the screen: " + "; ".join(errors))
        raise CliError(
            "no Wayland screenshot tool found for this desktop; install one: "
            "sudo apt install gnome-screenshot (GNOME), grim (Sway/Hyprland) or spectacle (KDE)"
        )
    # X11 without a working tool: Pillow's own XCB capture. pyautogui's screenshot() is no
    # use here, since it refuses to run on Linux unless gnome-screenshot is installed.
    try:
        img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY")).convert("RGB")
    except OSError as exc:
        errors.append(f"Pillow: {exc}")
        raise CliError(
            "cannot capture the X11 screen: " + "; ".join(errors) + "; install a screenshot tool: "
            "sudo apt install gnome-screenshot"
        ) from exc
    _LAST_CAPTURE_TOOL = "pillow"
    return img


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
    """Parse `xrandr --listmonitors` output. Before the name, `*` marks the primary;
    `+` only means the monitor is automatic, and every normal monitor has it."""
    found = []
    for line in text.splitlines():
        match = XRANDR_MONITOR_RE.match(line)
        if match:
            flags, name, width, height, x, y = match.groups()
            found.append(Monitor(0, name, int(x), int(y), int(width), int(height), "*" in flags, 1.0))
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


# The layout can't change within one command, and detecting it runs xrandr on Linux, so
# execute() keeps it here for the length of each command (None: no command running).
_MONITOR_CACHE: dict | None = None


def list_monitors(backend: Backend) -> list[Monitor]:
    if _MONITOR_CACHE is None:
        return order_monitors(detect_monitors(backend)[0])
    if backend.name not in _MONITOR_CACHE:
        _MONITOR_CACHE[backend.name] = order_monitors(detect_monitors(backend)[0])
    return list(_MONITOR_CACHE[backend.name])


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


def to_global(backend: "Backend", monitor: str | None, x: int, y: int) -> tuple[int, int]:
    """Map (x, y) relative to `monitor`'s top-left to global coordinates.

    With no monitor the point is already global and is returned unchanged.
    """
    if monitor is None:
        return x, y
    target = resolve_monitor(monitor, list_monitors(backend))
    if target is None:
        raise CliError("--monitor all has no origin; give primary or an index")
    if not (0 <= x < target.width and 0 <= y < target.height):
        raise CliError(
            f"({x}, {y}) is outside monitor {target.index} ({target.width}x{target.height}); "
            "with --monitor, x and y are relative to that monitor's top-left"
        )
    return target.x + x, target.y + y


def _monitor_fields(args: argparse.Namespace) -> dict:
    monitor = getattr(args, "monitor", None)
    return {} if monitor is None else {"monitor": monitor}


class Capture(NamedTuple):
    image: Image.Image
    x: int  # global coordinates of the image's top-left pixel
    y: int
    scale: float  # physical / logical ratio of the capture before normalization
    monitor: str  # "all" or the monitor index
    cursor: str = "none"  # "native" when the capture tool drew the mouse pointer
    raw: Image.Image | None = None  # the physical pixels, when scale != 1 (for --zoom)


def _fit(img: Image.Image, width: int, height: int) -> tuple[Image.Image, float]:
    """Resize a capture to its logical size; returns the image and the original ratio."""
    scale = img.width / width if width else 1.0
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    return img, scale


def _scaled(rect: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    return tuple(round(v * scale) for v in rect)  # type: ignore[return-value]


def _capture_linux(
    backend: Backend, monitors: list[Monitor], target: Monitor | None, cursor: bool = False
) -> Capture:
    grabbed = grab_linux(cursor=cursor)
    img, scale = _fit(grabbed, *backend.size())
    raw = grabbed if grabbed.size != img.size else None
    drawn = "native" if cursor and linux_cursor_supported() else "none"
    if target is None:
        return Capture(img, 0, 0, scale, "all", drawn, raw)  # the X root starts at (0, 0)
    raw = raw.crop(_scaled(target.rect(), scale)) if raw is not None else None
    return Capture(img.crop(target.rect()), target.x, target.y, scale, str(target.index), drawn, raw)


def _capture_windows(monitors: list[Monitor], target: Monitor | None) -> Capture:
    if target is None:
        left, top, right, bottom = union_rect(monitors)
        label = "all"
    else:
        left, top, right, bottom = target.rect()
        label = str(target.index)
    grabbed = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
    img, scale = _fit(grabbed, right - left, bottom - top)
    raw = grabbed if grabbed.size != img.size else None
    return Capture(img, left, top, scale, label, "none", raw)  # ImageGrab never draws the pointer


def _screencapture_rect(x: int, y: int, width: int, height: int, cursor: bool = False) -> Image.Image:
    """One display region via macOS screencapture, run by us so nothing touches stdout."""
    fd, path = tempfile.mkstemp(prefix="ai_desktop_", suffix=".png")
    os.close(fd)
    try:
        # -R takes its value attached: a separate "-1920,0,..." would be read as a flag.
        # -C draws the mouse pointer.
        result = subprocess.run(
            ["screencapture", "-x", *(["-C"] if cursor else []), f"-R{x},{y},{width},{height}", path],
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


def _capture_macos(monitors: list[Monitor], target: Monitor | None, cursor: bool = False) -> Capture:
    targets = monitors if target is None else [target]
    drawn = "native" if cursor else "none"
    pieces = []
    for m in targets:
        grabbed = _screencapture_rect(m.x, m.y, m.width, m.height, cursor=cursor)
        img, scale = _fit(grabbed, m.width, m.height)
        pieces.append((m, img, scale, grabbed))
    if target is not None:
        m, img, scale, grabbed = pieces[0]
        raw = grabbed if grabbed.size != img.size else None
        return Capture(img, m.x, m.y, scale, str(m.index), drawn, raw)
    # Displays may differ in scale, so the stitched "all" image keeps no physical copy.
    left, top, right, bottom = union_rect(monitors)
    canvas = Image.new("RGB", (right - left, bottom - top), (0, 0, 0))
    for m, img, _, _ in pieces:
        canvas.paste(img, (m.x - left, m.y - top))
    return Capture(canvas, left, top, max(scale for _, _, scale, _ in pieces), "all", drawn)


# Set inside the background service while the live view runs: a callable(backend, monitor,
# cursor) returning the newest streamed frame as a Capture, or None to capture as usual.
FRAME_SOURCE = None
IN_SERVICE = False  # True in the background service process (ai_desktop_service.serve)


def capture_screen(backend: Backend, monitor: str = "all", *, cursor: bool = False) -> Capture:
    """Capture all monitors (or one) at logical size.

    Image pixel (px, py) is screen point (x + px, y + py), so grid labels can show the
    coordinates `click` needs even for a monitor left of the primary (negative x).
    `cursor` asks the capture tool to draw the mouse pointer; Capture.cursor says whether
    it did. With the live view running, the newest streamed frame is used (milliseconds,
    no screen flash).
    """
    if FRAME_SOURCE is not None:
        try:
            cap = FRAME_SOURCE(backend, monitor, cursor)
        except CliError:
            raise
        except Exception:
            cap = None
        if cap is not None:
            return cap
    return capture_tool(backend, monitor, cursor=cursor)


def capture_tool(backend: Backend, monitor: str = "all", *, cursor: bool = False) -> Capture:
    """capture_screen without the live view: the platform's capture tool or API."""
    monitors = list_monitors(backend)
    target = resolve_monitor(monitor, monitors)
    if sys.platform == "win32":
        return _capture_windows(monitors, target)
    if sys.platform == "darwin":
        return _capture_macos(monitors, target, cursor)
    return _capture_linux(backend, monitors, target, cursor)


# --- Screenshot store --------------------------------------------------------------------

# Only files the tool named itself are ever deleted:
# YYYYmmdd-HHMMSS-mmm[-N][-mM][-zoom][-gridS][.raw].png, where .raw.png is the un-gridded
# capture kept next to each screenshot for --seen and `wait --changed`.
STORE_NAME_RE = re.compile(r"^\d{8}-\d{6}-\d{3}(-\d+)?(-m\d+)?(-zoom|-region)?(-grid\d+)?(\.raw)?\.png$")
RAW_SUFFIX = ".raw.png"


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


def store_files(include_raw: bool = False) -> list[tuple[Path, os.stat_result]]:
    """The tool's own screenshots, newest first by modification time.

    The `.raw.png` sidecars are left out unless `include_raw`. Names are local time and
    not monotonic across a DST change, so ordering and age always come from mtime.
    """
    files = []
    try:
        with os.scandir(store_dir()) as entries:
            for entry in entries:
                if not STORE_NAME_RE.match(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                if include_raw or not entry.name.endswith(RAW_SUFFIX):
                    files.append((Path(entry.path), entry.stat(follow_symlinks=False)))
    except FileNotFoundError:
        return []
    files.sort(key=lambda item: item[1].st_mtime, reverse=True)
    return files


def prune_store(
    keep_days: float | None, keep_mb: float | None, now: float | None = None, keep: tuple = ()
) -> list[Path]:
    """Delete store files older than `keep_days`, then the oldest until the total is under
    `keep_mb`. `None` disables a rule. A screenshot and its `.raw.png` copy are judged and
    deleted together, by the newer of the two, so --seen never finds half a pair. Pairs
    with a file in `keep` (the screenshot just taken) are never deleted but count towards
    the total. Returns the paths that were removed."""
    now = time.time() if now is None else now
    protected = {Path(p).resolve() for p in keep}
    groups: dict[str, list[tuple[Path, os.stat_result]]] = {}
    for path, st in store_files(include_raw=True):
        stem = path.name[: -len(RAW_SUFFIX)] if path.name.endswith(RAW_SUFFIX) else path.name[: -len(".png")]
        groups.setdefault(stem, []).append((path, st))
    pairs = []  # (newest mtime, total size, paths), newest first
    kept_size = 0
    for members in groups.values():
        size = sum(st.st_size for _, st in members)
        if any(path.resolve() in protected for path, _ in members):
            kept_size += size
            continue
        pairs.append((max(st.st_mtime for _, st in members), size, [path for path, _ in members]))
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    doomed: list[Path] = []
    if keep_days is not None:
        cutoff = now - keep_days * 86400
        doomed += [path for mtime, _, paths in pairs if mtime < cutoff for path in paths]
        pairs = [pair for pair in pairs if pair[0] >= cutoff]
    if keep_mb is not None:
        total = kept_size + sum(size for _, size, _ in pairs)
        limit = keep_mb * 1024 * 1024
        while pairs and total > limit:
            _, size, paths = pairs.pop()  # the oldest is last
            doomed.extend(paths)
            total -= size
    removed = []
    for path in doomed:
        try:
            os.remove(path)
        except OSError:  # open in a viewer on Windows, or another agent got there first
            continue
        removed.append(path)
    return removed


def prune_if_due(keep: tuple = ()) -> list[Path] | None:
    """Automatic prune, at most once per PRUNE_INTERVAL. Returns None when skipped.
    `keep` protects the files just written (see prune_store).

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
        removed = prune_store(keep_days, keep_mb, keep=keep)
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
    for key in ("text", "value"):  # type/paste/clipboard text, set-text's new value
        if isinstance(arg_dict.get(key), str):
            arg_dict[f"{key}_chars"] = len(arg_dict.pop(key))
    result.pop("text", None)
    # `run`: the raw --steps JSON can hold typed text (on success it is replaced by the
    # command names), and its steps' results can hold copied text. Copies, not edits:
    # the payload also goes back to the caller.
    if isinstance(arg_dict.get("steps"), str):
        arg_dict["steps_chars"] = len(arg_dict.pop("steps"))
    if isinstance(result.get("results"), list):
        result["results"] = [
            {**item, "result": {k: v for k, v in item["result"].items() if k != "text"}}
            if isinstance(item, dict) and isinstance(item.get("result"), dict) else item
            for item in result["results"]
        ]


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
        folder = private_dir(log_dir())
        with private_file(folder / f"actions-{now:%Y%m%d}.jsonl", "a") as fh:
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


# --- Human-like input pacing -------------------------------------------------------------

# Set per command by execute(): --instant or AI_DESKTOP_HUMAN=0 clear HUMAN_INPUT, and
# --seed or AI_DESKTOP_SEED seed RNG. Every pacing wait goes through _sleep so tests can
# stub it; the short hardware settles inside the backends stay time.sleep.
HUMAN_INPUT = True
RNG = random.Random()
_sleep = time.sleep
_now = time.monotonic  # the clock for `wait` and other polling; tests swap in a fake one

HUMAN_CHAR_PAUSE = (0.10, 0.15)  # seconds after an ordinary character
HUMAN_WORD_PAUSE = (0.25, 0.5)  # seconds after a word break
WORD_BREAKS = " \n\t"
HUMAN_MOVE_SPEED = (700, 1400)  # px/s, drawn once per move
HUMAN_MOVE_DURATION = (0.15, 1.5)  # seconds for the whole move
HUMAN_STEP_PX = 15
HUMAN_STEPS = (8, 60)
HUMAN_CURVE = (0.08, 0.25)  # Bezier control-point offset, as a fraction of the distance
HUMAN_JITTER_PX = 2
POINTER_FILE = "pointer.json"  # Wayland: where the tool last put the pointer

# --- Stop switch ---------------------------------------------------------------------------

# `ai-desktop stop` creates this file; while it exists every action refuses to start and a
# running one aborts at its next pause (between glide steps, typed characters, drag steps,
# scroll notches), on every platform including Wayland, where no corner failsafe exists.
STOP_FILE = "STOP"
STOP_MESSAGE = (
    "stopped: the human ran 'ai-desktop stop', so no action runs until they resume. "
    "Stop and ask them; do not resume or remove the stop yourself."
)
# Commands that still work while stopped: they only look, or they manage the stop itself.
STOP_EXEMPT = (
    "stop", "resume", "screenshot", "grid", "monitors", "windows", "position", "log",
    "screenshots", "doctor", "wait", "mcp", "service", "live", "observe", "watch", "text", "ui", "read",
    "locate", "describe",
)


def stop_path() -> Path:
    return home_dir() / STOP_FILE


def is_stopped() -> bool:
    return stop_path().exists()


# Set by the MCP server for the call it is running: a threading.Event the client's
# cancellation sets, so the action stops at its next pause instead of finishing.
CANCEL = None


def check_cancel() -> None:
    if CANCEL is not None and CANCEL.is_set():
        raise CliError("cancelled by the client", code=130, cancelled=True)


def check_stop() -> None:
    """Abort if the client cancelled the call (exit 130), or if the human has stopped
    ai-desktop (exit 3, like the failsafe)."""
    check_cancel()
    if is_stopped():
        raise CliError(STOP_MESSAGE, code=EXIT_FAILSAFE, stopped=True)


def _pause(seconds: float) -> None:
    """A pacing wait that first checks the stop switch."""
    check_stop()
    _sleep(seconds)


def human_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "instant", False):
        return False
    return os.environ.get("AI_DESKTOP_HUMAN", "").strip().lower() not in ("0", "false", "off", "no")


def seed_rng(args: argparse.Namespace) -> None:
    seed = getattr(args, "seed", None)
    if seed is None:
        raw = os.environ.get("AI_DESKTOP_SEED", "").strip()
        if not raw:
            return
        try:
            seed = int(raw)
        except ValueError as exc:
            raise CliError(f"AI_DESKTOP_SEED must be an integer, not {raw!r}") from exc
    RNG.seed(seed)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def nearest_on_monitors(monitors: list[Monitor], x: float, y: float) -> tuple[int, int]:
    """The point on any monitor closest to (x, y): itself when it is already on one."""
    best: tuple[int, int] | None = None
    best_dist = math.inf
    for m in monitors:
        cx = int(_clamp(round(x), m.x, m.x + m.width - 1))
        cy = int(_clamp(round(y), m.y, m.y + m.height - 1))
        dist = (cx - x) ** 2 + (cy - y) ** 2
        if dist < best_dist:
            best, best_dist = (cx, cy), dist
    return best if best is not None else (round(x), round(y))


def confine_point(x: float, y: float, monitors: list[Monitor], size: tuple[int, int]) -> tuple[int, int]:
    """Put a waypoint on a monitor and 1 px away from the failsafe corners of the primary screen."""
    px, py = nearest_on_monitors(monitors, x, y)
    width, height = size
    if px in (0, width - 1) and py in (0, height - 1):
        px += 1 if px == 0 else -1
        py += 1 if py == 0 else -1
    return px, py


def human_path(
    start: tuple[int, int],
    end: tuple[int, int],
    monitors: list[Monitor],
    size: tuple[int, int],
    rng: random.Random,
    max_duration: float | None = None,
) -> list[tuple[int, int, float]]:
    """Waypoints (x, y, pause before) along a random curve from start towards end, taking
    at most `max_duration` seconds when given.

    The end point itself is not included: the caller's final exact move lands there.
    The curve is a cubic Bezier with randomly offset control points, walked slow-fast-slow,
    with a little jitter that fades out before the target.
    """
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 2:
        return []
    low, high = HUMAN_MOVE_DURATION
    if max_duration is not None:
        low, high = min(low, max_duration), min(high, max_duration)
    duration = _clamp(dist / rng.uniform(*HUMAN_MOVE_SPEED), low, high)
    steps = int(_clamp(round(dist / HUMAN_STEP_PX), *HUMAN_STEPS))
    nx, ny = -dy / dist, dx / dist  # unit normal to the straight line
    controls = []
    for low, high in ((0.2, 0.4), (0.6, 0.8)):
        t = rng.uniform(low, high)
        offset = rng.uniform(*HUMAN_CURVE) * dist * rng.choice((-1, 1))
        controls.append((x0 + dx * t + nx * offset, y0 + dy * t + ny * offset))
    (cx1, cy1), (cx2, cy2) = controls
    path: list[tuple[int, int, float]] = []
    previous = (x0, y0)
    for i in range(1, steps):
        u = i / steps
        s = u * u * (3 - 2 * u)  # smoothstep: slow start, fast middle, slow finish
        a, b, c, d = (1 - s) ** 3, 3 * (1 - s) ** 2 * s, 3 * (1 - s) * s * s, s**3
        bx = a * x0 + b * cx1 + c * cx2 + d * x1
        by = a * y0 + b * cy1 + c * cy2 + d * y1
        jitter = (1 - u) * HUMAN_JITTER_PX
        bx += rng.uniform(-jitter, jitter)
        by += rng.uniform(-jitter, jitter)
        pause = duration / steps * rng.uniform(0.6, 1.4)
        point = confine_point(bx, by, monitors, size)
        if point == previous or point == (x1, y1):
            if path:  # same pixel as the last waypoint: wait longer there instead
                px, py, wait = path[-1]
                path[-1] = (px, py, wait + pause)
            continue
        path.append((*point, pause))
        previous = point
    return path


def typing_plan(text: str, rng: random.Random) -> list[tuple[str, float]]:
    """Split text into a list of characters, each with the pause that follows it."""
    chars = list(text)
    plan = []
    for i, ch in enumerate(chars):
        if i == len(chars) - 1:
            pause = 0.0
        elif ch in WORD_BREAKS:
            pause = rng.uniform(*HUMAN_WORD_PAUSE)
        else:
            pause = rng.uniform(*HUMAN_CHAR_PAUSE)
        plan.append((ch, pause))
    return plan


def synthetic_start(target: tuple[int, int], monitor: Monitor, rng: random.Random) -> tuple[int, int]:
    """A random point near the target on its monitor, for when the real position is unknown."""
    angle = rng.uniform(0, 2 * math.pi)
    radius = rng.uniform(200, 600)
    x = int(_clamp(round(target[0] + radius * math.cos(angle)), monitor.x, monitor.x + monitor.width - 1))
    y = int(_clamp(round(target[1] + radius * math.sin(angle)), monitor.y, monitor.y + monitor.height - 1))
    if (x, y) == target:
        x += 1 if x < monitor.x + monitor.width - 1 else -1
    return x, y


def remember_pointer(x: int, y: int) -> None:
    """Keep where the tool last put the pointer; Wayland won't tell us. Never raises."""
    try:
        with private_file(private_dir(home_dir()) / POINTER_FILE) as fh:
            fh.write(json.dumps({"x": x, "y": y}))
    except OSError:
        pass


def recall_pointer() -> tuple[int, int] | None:
    try:
        data = json.loads((home_dir() / POINTER_FILE).read_text(encoding="utf-8"))
        return int(data["x"]), int(data["y"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def known_pointer(backend: Backend, monitors: list[Monitor]) -> tuple[tuple[int, int] | None, str]:
    """Where the pointer is: ((x, y), "live") when the backend can read it, the position
    ai-desktop last moved it to as "remembered" (Wayland), or (None, "unknown")."""
    pos = backend.position()
    if pos is not None:
        return pos, "live"
    pos = recall_pointer()
    if pos is not None and monitor_at(monitors, *pos) is not None:
        return pos, "remembered"
    return None, "unknown"


# --- Backends ----------------------------------------------------------------------------


def _stub_mouseinfo() -> None:
    """pyautogui imports mouseinfo, which calls sys.exit() on Linux when tkinter is missing
    (stock Ubuntu has no python3-tk). We never use its window, so give it an empty module."""
    if "mouseinfo" not in sys.modules:
        stub = types.ModuleType("mouseinfo")

        def unavailable(*args, **kwargs):
            raise CliError("mouseinfo is not available in ai-desktop")

        stub.MouseInfoWindow = unavailable  # type: ignore[attr-defined]
        sys.modules["mouseinfo"] = stub


def load_pyautogui():
    """Import pyautogui lazily so --help, grid and launch-chrome work without a display."""
    _stub_mouseinfo()
    try:
        import pyautogui
    except (Exception, SystemExit) as exc:  # ImportError, Xlib display errors, a sys.exit()
        raise CliError(f"cannot load pyautogui: {exc}") from exc
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1
    # pyautogui itself only watches the top-left corner; make all four corners of the
    # primary screen abort, as documented. check_point and the glide never go there.
    try:
        width, height = (int(v) for v in pyautogui.size())
        pyautogui.FAILSAFE_POINTS = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    except Exception:
        pass  # keep pyautogui's default
    return pyautogui


class Backend:
    name = ""
    glide_max: float | None = None  # seconds; set for guarded actions (see guarded())

    def prepare(self, kind: str) -> None:
        """Get the input device for `kind` ("pointer" or "keyboard") ready ahead of time."""

    def size(self) -> tuple[int, int]:
        raise NotImplementedError

    def position(self) -> tuple[int, int] | None:
        return None

    def check_point(self, x: int, y: int) -> None:
        monitors = list_monitors(self)
        self._monitors = monitors  # reused by the path, so xrandr runs once per command
        if monitor_at(monitors, x, y) is None:
            where = "; ".join(
                f"{m.index} {m.name} at {m.x},{m.y} size {m.width}x{m.height}" for m in monitors
            )
            raise CliError(f"({x}, {y}) is on no monitor; monitors: {where}")

    def _path_to(
        self, x: int, y: int, start: tuple[int, int] | None = None
    ) -> list[tuple[int, int, float]]:
        """Human-like waypoints towards (x, y), or none with --instant."""
        if not HUMAN_INPUT:
            return []
        monitors = getattr(self, "_monitors", None) or list_monitors(self)
        start = start or self.position()
        if start is None:
            start = synthetic_start((x, y), monitor_at(monitors, x, y) or monitors[0], RNG)
        return human_path(start, (x, y), monitors, self.size(), RNG, self.glide_max)


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

    def _glide(self, x: int, y: int) -> None:
        # _pause=False skips pyautogui.PAUSE per waypoint; the failsafe check still runs.
        for px, py, pause in self._path_to(x, y):
            if pause > 0:
                _pause(pause)
            self.gui.moveTo(px, py, _pause=False)

    def move(self, x: int, y: int) -> None:
        self._glide(x, y)
        self.gui.moveTo(x, y)

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        self._glide(x, y)
        self.gui.click(x=x, y=y, button=button, clicks=clicks, interval=0.1)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str, duration: float) -> None:
        # Glide to the start like click; the move with the button held is a straight line
        # over `duration` (a curve would change what a selection or a drawing covers).
        self._glide(x1, y1)
        self.gui.moveTo(x1, y1)
        self.gui.mouseDown(button=button)
        try:
            # dragTo, not moveTo: on macOS a move with the button held must be sent as
            # drag events, or windows, sliders and selections don't follow.
            self.gui.dragTo(x2, y2, duration=duration, button=button, mouseDownUp=False)
        finally:
            self._release(button)

    def _release(self, button: str) -> None:
        # After a failsafe abort the pointer is still in the corner, and mouseUp runs the
        # failsafe check first: it would raise again and leave the button held down.
        saved = self.gui.FAILSAFE
        self.gui.FAILSAFE = False
        try:
            self.gui.mouseUp(button=button)
        finally:
            self.gui.FAILSAFE = saved

    def type_text(self, text: str, interval: float) -> None:
        if not HUMAN_INPUT:
            self.gui.write(text, interval=interval)
            return
        for ch, pause in typing_plan(text, RNG):
            self.gui.write(ch, interval=0, _pause=False)
            if pause:
                _pause(pause)

    def press(self, key: str) -> None:
        self.gui.press(key)

    def hotkey(self, keys: list[str]) -> None:
        self.gui.hotkey(*keys, interval=0.05)

    def scroll(self, notches: int, x: int | None = None, y: int | None = None) -> None:
        # pyautogui hands Windows the raw wheel delta, and one notch is 120 (WHEEL_DELTA).
        clicks = notches * 120 if sys.platform == "win32" else notches
        if x is not None and y is not None:
            self._glide(x, y)
            self.gui.moveTo(x, y)  # on Windows pyautogui.scroll ignores x and y
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
    """A virtual input device on /dev/uinput: created and destroyed per action, or kept
    open across actions inside keep_devices()."""

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
        self.last_abs = (0, 0)  # the axes start at 0

    def emit(self, ev_type: int, code: int, value: int, sync: bool = True) -> None:
        """Write one event; `sync` ends the frame (SYN_REPORT). Events that belong together,
        such as the X and Y of one pointer position, go in one frame."""
        data = INPUT_EVENT.pack(0, 0, ev_type, code, value)
        if sync:
            data += INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
        os.write(self.fd, data)

    def close(self) -> None:
        if self.fd is None:
            return
        time.sleep(0.1)  # let the compositor drain events before the device disappears
        try:
            self._ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> _UInputDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# Virtual devices kept open between actions inside keep_devices() (`run`, the MCP server),
# keyed by kind and screen size. None: every action creates and destroys its own device.
_DEVICE_CACHE: dict | None = None
_ATEXIT_REGISTERED = False


def close_devices() -> None:
    global _DEVICE_CACHE
    cache, _DEVICE_CACHE = _DEVICE_CACHE, None
    for dev in (cache or {}).values():
        try:
            dev.close()
        except OSError:
            pass


@contextlib.contextmanager
def keep_devices():
    """Reuse the uinput devices across actions for the duration of the block.

    Creating a device costs AI_DESKTOP_UINPUT_DELAY (0.5 s) before the compositor sees its
    events; inside this block that cost is paid once per device. Nested blocks share the
    outer session, and the devices are destroyed when the outermost block ends (or at
    exit; the kernel also removes them when the process dies).
    """
    global _DEVICE_CACHE, _ATEXIT_REGISTERED
    if _DEVICE_CACHE is not None:
        yield
        return
    _DEVICE_CACHE = {}
    if not _ATEXIT_REGISTERED:
        atexit.register(close_devices)
        _ATEXIT_REGISTERED = True
    try:
        yield
    finally:
        close_devices()


class _Lease:
    """A cached device lent to one action. If the action fails (an error, Ctrl+C), the
    device is destroyed so the kernel releases any key or button it still holds."""

    def __init__(self, key: tuple, dev) -> None:
        self.key = key
        self.dev = dev

    def __enter__(self):
        return self.dev

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            if _DEVICE_CACHE is not None and _DEVICE_CACHE.get(self.key) is self.dev:
                del _DEVICE_CACHE[self.key]
            try:
                self.dev.close()
            except OSError:
                pass
        return False


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

    def _device(self, key: tuple, factory):
        """A context manager yielding the device: a fresh one per action, or a lease on a
        cached one inside keep_devices()."""
        if _DEVICE_CACHE is None:
            return factory()
        dev = _DEVICE_CACHE.get(key)
        if dev is None:
            for stale in [k for k in _DEVICE_CACHE if k[0] == key[0]]:  # e.g. a resized screen
                try:
                    _DEVICE_CACHE.pop(stale).close()
                except OSError:
                    pass
            dev = _DEVICE_CACHE[key] = factory()
        return _Lease(key, dev)

    def _pointer(self):
        width, height = self.size()
        return self._device(
            ("pointer", width, height),
            lambda: _UInputDevice(
                "ai-desktop pointer",
                keys=tuple(MOUSE_BUTTONS.values()),
                rel=(REL_WHEEL,),
                abs_axes=((ABS_X, width - 1), (ABS_Y, height - 1)),
            ),
        )

    def _keyboard(self):
        codes = set(UINPUT_KEYS.values()) | {code for code, _ in US_CHARMAP.values()}
        return self._device(("keyboard",), lambda: _UInputDevice("ai-desktop keyboard", keys=sorted(codes)))

    def prepare(self, kind: str) -> None:
        # Inside keep_devices() this creates the cached device now, so the compositor's
        # start-up delay is paid before a --seen check rather than between it and the action.
        if _DEVICE_CACHE is not None:
            with self._pointer() if kind == "pointer" else self._keyboard():
                pass

    def _emit_abs(self, dev: _UInputDevice, x: int, y: int) -> None:
        # Stay inside the axis range _pointer() declared, even if xrandr disagrees with size().
        width, height = self.size()
        x, y = int(_clamp(x, 0, width - 1)), int(_clamp(y, 0, height - 1))
        last = getattr(dev, "last_abs", None)
        if last == (x, y):
            # The kernel drops an event that repeats an axis's value, so on a reused device
            # a move to where it last was would be lost (say the human moved the mouse
            # since): step 1 px away first.
            dev.emit(EV_ABS, ABS_X, x + 1 if x < width - 1 else x - 1)
        dev.emit(EV_ABS, ABS_X, x, sync=False)  # one frame: the compositor sees (x, y) at once
        dev.emit(EV_ABS, ABS_Y, y)
        if last is not None:
            dev.last_abs = (x, y)

    def _move_to(self, dev: _UInputDevice, x: int, y: int) -> None:
        self._emit_abs(dev, x, y)
        time.sleep(0.05)

    def _glide(self, dev: _UInputDevice, x: int, y: int) -> None:
        if not HUMAN_INPUT:
            return
        start, _ = known_pointer(self, getattr(self, "_monitors", None) or list_monitors(self))
        for px, py, pause in self._path_to(x, y, start):
            if pause > 0:
                _pause(pause)
            self._emit_abs(dev, px, py)

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
            self._glide(dev, x, y)
            self._move_to(dev, x, y)
        remember_pointer(x, y)

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        code = MOUSE_BUTTONS[button]
        with self._pointer() as dev:
            self._glide(dev, x, y)
            self._move_to(dev, x, y)
            for i in range(clicks):
                if i:
                    time.sleep(0.08)
                dev.emit(EV_KEY, code, 1)
                time.sleep(0.02)
                dev.emit(EV_KEY, code, 0)
        remember_pointer(x, y)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str, duration: float) -> None:
        code = MOUSE_BUTTONS[button]
        steps = max(2, int(duration / 0.02))
        with self._pointer() as dev:
            self._glide(dev, x1, y1)
            self._move_to(dev, x1, y1)
            dev.emit(EV_KEY, code, 1)
            try:
                last = (x1, y1)
                for i in range(1, steps + 1):
                    point = (round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
                    check_stop()
                    if point != last:  # a repeat would make _emit_abs step aside with the button held
                        self._emit_abs(dev, *point)
                        last = point
                    time.sleep(duration / steps)
            finally:
                dev.emit(EV_KEY, code, 0)
        remember_pointer(x2, y2)

    def type_text(self, text: str, interval: float) -> None:
        with self._keyboard() as dev:
            if not HUMAN_INPUT:
                for ch in text:
                    check_stop()
                    self._tap(dev, *US_CHARMAP[ch])
                    time.sleep(interval)
                return
            for ch, pause in typing_plan(text, RNG):
                self._tap(dev, *US_CHARMAP[ch])
                if pause:
                    _pause(pause)

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
                self._glide(dev, x, y)
                self._move_to(dev, x, y)
            for _ in range(abs(notches)):
                check_stop()
                dev.emit(EV_REL, REL_WHEEL, step)
                time.sleep(0.02)
        if x is not None and y is not None:
            remember_pointer(x, y)


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
    folders += [Path(d) / "applications" for d in data_dirs.split(os.pathsep) if d]
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


def chrome_user_data_dir() -> Path | None:
    """Where Chrome keeps its profiles, if it is there."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        candidates = [Path(local) / "Google" / "Chrome" / "User Data"] if local else []
    elif sys.platform == "darwin":
        candidates = [Path.home() / "Library" / "Application Support" / "Google" / "Chrome"]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        candidates = [config / "google-chrome", config / "chromium"]
    return next((c for c in candidates if c.is_dir()), None)


def chrome_profiles(user_data: Path) -> dict[str, str]:
    """{profile folder: display name} from Chrome's Local State (folders only if unreadable)."""
    try:
        state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
        cache = state["profile"]["info_cache"]
        return {folder: str(info.get("name") or folder) for folder, info in cache.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {d.name: d.name for d in user_data.iterdir() if d.is_dir() and (d.name == "Default" or d.name.startswith("Profile "))}


def resolve_chrome_profile(profile: str | None) -> str | None:
    """The profile folder to pass to Chrome. Chrome silently creates a new, signed-out
    profile for any unknown --profile-directory, so an unknown name is an error here; a
    display name ("Work") is mapped to its folder ("Profile 2")."""
    if profile is None:
        return None
    if profile.startswith("-") or "/" in profile or "\\" in profile or profile in (".", ".."):
        raise CliError(f"--profile must be a profile folder name such as 'Profile 1', not {profile!r}")
    user_data = chrome_user_data_dir()
    if user_data is None:
        return profile  # can't check; Chrome will use or create it
    profiles = chrome_profiles(user_data)
    if profile in profiles:
        return profile
    by_name = [folder for folder, name in profiles.items() if name.lower() == profile.lower()]
    if len(by_name) == 1:
        return by_name[0]
    listing = ", ".join(f"{folder!r} ({name})" for folder, name in sorted(profiles.items())) or "none"
    raise CliError(f"no Chrome profile {profile!r} in {user_data}; profiles: {listing}")


URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")


def normalize_url(url: str | None) -> str | None:
    """Give a bare host a scheme: desktop launchers and `open` treat "example.com" as a
    local file. Existing files and anything with a scheme are left alone."""
    if url is None:
        return None
    url = url.strip()
    if not url or url.startswith("-"):
        raise CliError(f"--url must be a web address or a file, not {url!r}")
    if URL_SCHEME_RE.match(url) or os.path.exists(os.path.expanduser(url)):
        return url
    host = url.split("/", 1)[0].split(":", 1)[0].lower()
    local = host in ("localhost", "[") or host.startswith("127.") or host.endswith(".localhost")
    return ("http://" if local else "https://") + url


def launch_chrome_macos(url: str | None, profile: str | None) -> None:
    app = next((p for p in MAC_CHROME_APPS if os.path.isdir(p)), None)
    if profile:
        # `open -a ... --args` passes arguments only when it starts a new instance; with
        # Chrome already running the profile was dropped. The binary itself hands the
        # request to the running instance, profile included.
        if app is None:
            raise CliError("Google Chrome.app not found in /Applications or ~/Applications")
        spawn_detached([os.path.join(app, "Contents", "MacOS", "Google Chrome"), f"--profile-directory={profile}", *([url] if url else [])])
        launcher = "binary"
    else:
        argv = ["open", "-a", app or "Google Chrome"] + ([url] if url else [])
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise CliError(f"could not open Google Chrome: {result.stderr.strip()}")
        launcher = "open"
    status("launch-chrome", app=app or "Google Chrome", profile=profile or "last-used", url=url or "none", launcher=launcher)


def run_launcher(argv: list[str], wait: float = 5.0) -> str | None:
    """Start a short-lived launcher (gtk-launch, gio) detached and wait briefly for it.
    Returns None when it worked (or is still running), else the reason it failed."""
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                env=host_env(), start_new_session=True,
            )
        except OSError as exc:
            return str(exc)
        try:
            code = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            return None
        if code == 0:
            return None
        err.seek(0)
        detail = err.read().decode(errors="replace").strip().splitlines()
        return detail[-1] if detail else f"exit code {code}"


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
    type: str = "normal"  # normal, dialog or utility (X11 EWMH window type; "normal" elsewhere)


# EWMH window types that are not windows an agent works with.
X11_SKIP_TYPES = ("DOCK", "DESKTOP", "SPLASH", "TOOLBAR", "MENU", "DROPDOWN_MENU", "POPUP_MENU",
                  "TOOLTIP", "NOTIFICATION", "COMBO", "DND")
X11_KEEP_TYPES = ("NORMAL", "DIALOG", "UTILITY")

WAYLAND_WINDOWS_NOTE = (
    "X11/XWayland windows only: native Wayland windows cannot be listed or focused (GNOME "
    "has no public API for them); log in to the 'Ubuntu on Xorg' session for full window control"
)


def _windows_x11() -> list[Win]:
    with _X11() as x:
        stacking = x.get_longs(x.root, "_NET_CLIENT_LIST_STACKING") or x.get_longs(x.root, "_NET_CLIENT_LIST")
        active = (x.get_longs(x.root, "_NET_ACTIVE_WINDOW") or [0])[0]
        hidden, skip = x.atom("_NET_WM_STATE_HIDDEN"), x.atom("_NET_WM_STATE_SKIP_TASKBAR")
        skip_types = {x.atom(f"_NET_WM_WINDOW_TYPE_{t}") for t in X11_SKIP_TYPES}
        keep_types = {x.atom(f"_NET_WM_WINDOW_TYPE_{t}"): t.lower() for t in X11_KEEP_TYPES}
        found = []
        for window in reversed(stacking):  # EWMH lists bottom-to-top; we want the front first
            try:
                title = x.get_text(window, "_NET_WM_NAME") or x.get_text(window, "WM_NAME")
                if not title:
                    continue
                state = x.get_longs(window, "_NET_WM_STATE")
                types = x.get_longs(window, "_NET_WM_WINDOW_TYPE")
                if skip_types.intersection(types):
                    continue
                kind = next((keep_types[t] for t in types if t in keep_types), "normal")
                # Without a type, skip-taskbar marks panels and the like; but GNOME also sets
                # it on every dialog with a parent, and those must stay listed.
                if skip in state and kind != "dialog":
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
                # A window that draws its own decorations (GTK) includes an invisible shadow
                # margin around what you see; leave it out, as the Win32 path does.
                shadow = x.get_longs(window, "_GTK_FRAME_EXTENTS")
                if len(shadow) == 4:
                    gx, gy = gx + shadow[0], gy + shadow[2]
                    width = max(1, width - shadow[0] - shadow[1])
                    height = max(1, height - shadow[2] - shadow[3])
            except CliError:
                continue  # closed while we were looking
            found.append(
                Win(f"{window:#x}", pid, app, title, gx, gy, width, height, window == active,
                    hidden in state or not viewable, kind)
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
    # activateWithOptions_ only says the request was accepted, and with several windows
    # the app may come forward with another one in front: check what is really in front.
    time.sleep(0.3)
    try:
        front = _windows_macos()
    except Exception:
        front = []
    if front:
        top = front[0]
        result["activated"] = bool(result["activated"]) and top.pid == win.pid and (not win.title or top.title == win.title)
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

def _powershell() -> list[str]:
    """PowerShell by its full path: Windows searches the current folder before System32
    for a bare name, so a powershell.exe in the agent's working folder would run instead."""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return [exe if os.path.isfile(exe) else "powershell", "-NoProfile", "-NonInteractive", "-Command"]


def _clipboard_tools() -> tuple[list[str], list[str]]:
    """Return (set_argv, get_argv) for this platform's clipboard tool."""
    if sys.platform == "darwin":
        return ["pbcopy"], ["pbpaste"]
    if sys.platform == "win32":
        ps = _powershell()
        return (
            ps + ["[Console]::InputEncoding = [Text.Encoding]::UTF8; "
                  "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            ps + ["[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "$c = Get-Clipboard -Raw; if ($c) { [Console]::Out.Write($c) }"],
        )
    x_tools = [
        (["xclip", "-selection", "clipboard", "-in"], ["xclip", "-selection", "clipboard", "-out"]),
        (["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]),
    ] if os.environ.get("DISPLAY") else []
    # Plain text only: without a type wl-paste returns whatever is offered first (an
    # image's bytes), and wl-copy guesses a type from the content.
    wl_tools = [
        (["wl-copy", "--type", "text/plain"], ["wl-paste", "--no-newline", "--type", "text"])
    ] if os.environ.get("WAYLAND_DISPLAY") else []
    # On GNOME the X tools come first: Xwayland's clipboard is always shared with native
    # apps there, and unlike wl-copy they never steal focus. KDE and wlroots compositors
    # share it only while an X11 window has focus, so wl-clipboard goes first there.
    gnome = "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    candidates = x_tools + wl_tools if gnome or not wl_tools else wl_tools + x_tools
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
            f"none found that works on this desktop ({desktop_name()})",
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
    checks.append(run_check("stop", _check_stop_switch))
    checks.append(run_check("service", _check_service, soft=True))
    checks.append(run_check("text-recognition", _check_tesseract, soft=True))
    checks.append(run_check("vision-model", _check_vision, soft=True))
    if sys.platform.startswith("linux"):
        checks.append(run_check("live-view", _check_live, soft=True))
    return checks


# --- Screenshot pipeline -----------------------------------------------------------------


def max_width_setting() -> int:
    if MAX_WIDTH_OVERRIDE is not None:
        return MAX_WIDTH_OVERRIDE
    raw = os.environ.get("AI_DESKTOP_MAX_WIDTH", "").strip()
    if not raw:
        return DEFAULT_MAX_WIDTH
    try:
        return _max_width(raw)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        raise CliError(f"AI_DESKTOP_MAX_WIDTH must be 0 (never shrink) or at least {MIN_MAX_WIDTH} px, not {raw!r}") from exc


def sidecar_path(path: str | Path) -> Path:
    """Where the un-gridded copy of a screenshot lives: `<name>.raw.png` next to it."""
    path = Path(path)
    return path if path.name.endswith(RAW_SUFFIX) else path.with_name(path.stem + RAW_SUFFIX)


def zoom_box(
    center: tuple[int, int],
    radius: int,
    monitors: list[Monitor],
    capture_rect: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """The screen area (left, top, right, bottom) a zoom shows: `radius` px around
    `center`, kept on the centre's monitor so it never straddles a gap."""
    x, y = center
    monitor = monitor_at(monitors, x, y)
    if monitor is None:
        raise CliError(f"--zoom {x},{y} is on no monitor; run: ai-desktop monitors")
    left, top, right, bottom = capture_rect
    if not (left <= x < right and top <= y < bottom):
        raise CliError(
            f"--zoom {x},{y} is outside the captured area ({left},{top} to {right - 1},{bottom - 1}); "
            "use --monitor all or the monitor it is on"
        )
    ml, mt, mr, mb = monitor.rect()
    return max(x - radius, ml, left), max(y - radius, mt, top), min(x + radius, mr, right), min(y + radius, mb, bottom)


def zoom_crop(cap: Capture, box: tuple[int, int, int, int], scale: float) -> Image.Image:
    """The zoom area resized to `scale` image px per screen px, from the physical pixels
    when there are more of them (HiDPI), so small text is actually sharper, not just bigger."""
    left, top, right, bottom = box
    local = (left - cap.x, top - cap.y, right - cap.x, bottom - cap.y)
    src = cap.raw.crop(_scaled(local, cap.scale)) if cap.raw is not None else cap.image.crop(local)
    size = (max(1, round((right - left) * scale)), max(1, round((bottom - top) * scale)))
    return src.resize(size, Image.Resampling.LANCZOS)


def draw_pointer_marker(img: Image.Image, xy: tuple[int, int], source: str, k: float = 1.0) -> Image.Image:
    """A crosshair at the pointer's hotspot (left clear), labelled "ptr", or "ptr?" when
    the position is the one ai-desktop last set rather than read live."""
    x, y = xy
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    arm, gap, line_w = max(6, round(12 * k)), max(3, round(4 * k)), max(2, round(2 * k))
    arms = (
        ((x - gap - arm, y), (x - gap, y)),
        ((x + gap, y), (x + gap + arm, y)),
        ((x, y - gap - arm), (x, y - gap)),
        ((x, y + gap), (x, y + gap + arm)),
    )
    for color, w in (((0, 0, 0, 255), line_w + 2), ((255, 255, 255, 255), line_w)):
        for start, end in arms:
            draw.line([start, end], fill=color, width=w)
    font = grid_font(base.size, k)
    pad = max(LABEL_PAD, round(LABEL_PAD * k))
    label = "ptr" if source == "live" else "ptr?"
    w = _text_w(font, label) + 2 * pad
    _, top, _, bottom = font.getbbox(label)
    h = bottom - top + 2 * pad
    offset = gap + arm
    left = x + offset if x + offset + w < base.width else x - offset - w
    top = y + offset if y + offset + h < base.height else y - offset - h
    _draw_label(draw, label, font, left, top, pad)
    return Image.alpha_composite(base, overlay).convert("RGB")


def take_screenshot(
    backend: Backend,
    *,
    monitor: str = "all",
    grid: bool = False,
    spacing: int | None = None,
    zoom: tuple[int, int] | None = None,
    radius: int = ZOOM_RADIUS,
    factor: int = ZOOM_FACTOR,
    cursor: str = "auto",
    max_width: int | None = None,
    output: str | None = None,
    cap: Capture | None = None,
    region: tuple[int, int, int, int] | None = None,
) -> dict:
    """Capture (or reuse `cap`), zoom, grid, mark the pointer, shrink and save; returns the
    status fields. `region` (x, y, width, height in screen coordinates) crops to that area,
    enlarged `factor` times, like a zoom with exact edges.

    Besides the image the agent reads, the un-gridded capture is saved next to it as
    `<name>.raw.png` (the reference for --seen and `wait --changed`); both carry the
    origin, scale and pointer in an `ai-desktop` PNG text chunk. Grid labels are global
    screen coordinates whatever the zoom or shrink: image pixel (px, py) is screen point
    (x + px / image_scale, y + py / image_scale).
    """
    if output is not None and str(output).endswith(RAW_SUFFIX):
        raise CliError(f"-o must not end in {RAW_SUFFIX}: that name is kept for the un-gridded copy")
    limit = max_width_setting() if max_width is None else max_width
    if cap is None:
        cap = capture_screen(backend, monitor, cursor=cursor == "auto")
    monitors = list_monitors(backend)
    capture_rect = (cap.x, cap.y, cap.x + cap.image.width, cap.y + cap.image.height)
    if region is not None:
        rx, ry, rw, rh = region
        box = (max(rx, capture_rect[0]), max(ry, capture_rect[1]), min(rx + rw, capture_rect[2]), min(ry + rh, capture_rect[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise CliError(f"--region {rx},{ry},{rw},{rh} is outside the captured area")
    if zoom is not None or region is not None:
        # Enlarged straight to its final size: never bigger than --max-width (or
        # ZOOM_MAX_EDGE when that is 0), so a big radius x factor can't eat the memory.
        if zoom is not None:
            box = zoom_box(zoom, radius, monitors, capture_rect)
        width, height = (box[2] - box[0]) * factor, (box[3] - box[1]) * factor
        fit = min(view_scale(width, limit), ZOOM_MAX_EDGE / max(width, height), 1.0)
        scale = factor * fit
        img, ox, oy, grid, shrink = zoom_crop(cap, box, scale), box[0], box[1], True, 1.0
    else:
        img, ox, oy, scale = cap.image, cap.x, cap.y, 1
        shrink = view_scale(img.width, limit)
    k = 1 / shrink  # lines and labels are drawn this much bigger, then the image shrinks

    resolved = None
    if grid:
        resolved = spacing
        if resolved is None:
            pad = max(LABEL_PAD, round(LABEL_PAD * k))
            box_w, _ = label_metrics(grid_font(img.size, k), (ox, oy), (img.width / scale, img.height / scale), pad)
            candidates = ZOOM_SPACINGS if zoom is not None or region is not None else AUTO_SPACINGS
            resolved = auto_spacing(max(img.size) / scale, box_w, k, scale, candidates)
        img = draw_grid(img, resolved, (ox, oy), k, pixel_scale=scale)

    pointer, source = known_pointer(backend, monitors)
    drawn = "native" if cap.cursor == "native" else "none"
    if drawn == "none" and cursor != "none" and pointer is not None:
        local = (round((pointer[0] - ox) * scale), round((pointer[1] - oy) * scale))
        if 0 <= local[0] < img.width and 0 <= local[1] < img.height:
            img = draw_pointer_marker(img, local, source, k)
            drawn = "marker"
    if shrink < 1:
        size = (max(1, round(img.width * shrink)), max(1, round(img.height * shrink)))
        img = img.resize(size, Image.Resampling.LANCZOS)
    image_scale = round(scale * shrink, 4)

    tag = ("" if cap.monitor == "all" else f"-m{cap.monitor}") + ("-zoom" if zoom is not None else "-region" if region is not None else "")
    target = output or new_screenshot_path(grid, resolved or DEFAULT_SPACING, tag)
    pointer_fields = {
        "pointer": source,
        "pointer_x": pointer[0] if pointer else "unknown",
        "pointer_y": pointer[1] if pointer else "unknown",
    }
    raw_name = sidecar_path(target).name
    meta = {
        "version": 1, "x": ox, "y": oy, "width": img.width, "height": img.height,
        "image_scale": image_scale, "spacing": resolved, "monitor": cap.monitor,
        "zoom": list(zoom) if zoom else None, "region": list(region) if region else None,
        "cursor": drawn, "raw": raw_name, **pointer_fields,
    }
    raw_target = sidecar_path(Path(target).expanduser().resolve())
    if output is not None and raw_target.exists() and (read_image_meta(raw_target) or {}).get("kind") != "raw":
        raise CliError(f"{raw_target} exists and is not an ai-desktop copy; choose another -o path")
    path = save_image(img, target, meta)
    raw_meta = {
        "version": 1, "kind": "raw", "x": cap.x, "y": cap.y, "width": cap.image.width,
        "height": cap.image.height, "monitor": cap.monitor, "cursor": cap.cursor,
        "image": Path(path).name, **pointer_fields,
    }
    raw_path = save_image(cap.image, sidecar_path(path), raw_meta, compress_level=1)
    # Prune after saving: the capture has already succeeded, and the new files are never
    # candidates. Explicit -o paths live outside the store and are never pruned.
    pruned = prune_if_due(keep=(path, raw_path)) if output is None else None
    fields = dict(
        path=path,
        width=img.width,
        height=img.height,
        x=ox,
        y=oy,
        monitor=cap.monitor,
        spacing=resolved if grid else "none",
        scale=round(cap.scale, 3),
        backend=backend.name,
        pruned="skipped" if pruned is None else len(pruned),
        image_scale=image_scale,
        cursor=drawn,
        **pointer_fields,
    )
    if zoom is not None:
        fields.update(zoom=f"{zoom[0]},{zoom[1]}", radius=radius, factor=factor)
    if region is not None:
        fields.update(region=f"{box[0]},{box[1]},{box[2] - box[0]},{box[3] - box[1]}", factor=factor)
    return fields


# --- Stale-screen guard and waiting ------------------------------------------------------

# --seen compares the screen now with the un-gridded copy of the screenshot the agent read.
# Around a target point: the share of pixels in a 129x129 patch whose colour moved by
# more than PIXEL_TOLERANCE in any channel. Elsewhere (keys, typing): the share of cells
# of a coarse 64x36 grey fingerprint whose mean moved by more than CELL_TOLERANCE.
GUARD_RADIUS = 64
PIXEL_TOLERANCE = 24
GUARD_PATCH_MAX = 0.05  # a blinking caret is ~0.2%; a moved button or a menu is far more
FINGERPRINT_CELLS = (64, 36)
CELL_TOLERANCE = 12
GUARD_SCREEN_MAX = 0.10  # a clock tick is ~0, a toast ~3%, a dialog ~20%
STABLE_MAX = 0.005  # wait --stable: two captures in a row this close
CHANGED_MIN = 0.02  # wait --changed: more than this differs
CURSOR_MASK = (16, 40)  # px around a drawn pointer's hotspot (up/left, down/right) ignored
WAIT_MAX = 300  # seconds
SHOT_SETTLE = 0.4  # seconds between an action and its --shot (AI_DESKTOP_SHOT_DELAY)


class Reference(NamedTuple):
    image: Image.Image  # the un-gridded capture, logical px
    x: int
    y: int
    monitor: str
    cursor: str
    pointer: tuple[int, int] | None
    path: str

    def masks(self, *pointers: tuple[int, int] | None) -> list[tuple[int, int, int, int]]:
        """Local rectangles to ignore when the capture tool drew the pointer: around its
        recorded position and any other known positions (it may have moved since)."""
        if self.cursor != "native":
            return []
        before, after = CURSOR_MASK
        rects = []
        for pos in {p for p in (self.pointer, *pointers) if p is not None}:
            px, py = pos[0] - self.x, pos[1] - self.y
            rects.append((px - before, py - before, px + after, py + after))
        return rects


def resolve_seen(spec: str) -> Path:
    if spec == "latest":
        files = store_files()
        if not files:
            raise CliError(f"--seen latest: no screenshots in {store_dir()} yet; take one first")
        return files[0][0]
    return Path(spec).expanduser()


def load_reference(spec: str) -> Reference:
    """The un-gridded capture behind a screenshot path (or "latest"), with its origin."""
    path = resolve_seen(spec)
    if not path.is_file():
        raise CliError(f"--seen file not found: {path}")
    raw = sidecar_path(path)
    meta = read_image_meta(raw) if raw.is_file() else None
    if meta is None:
        raise CliError(
            f"no {raw.name} next to {path}: --seen needs a screenshot taken by "
            "'ai-desktop screenshot' (older files cannot be checked); take a new one"
        )
    if raw != path and meta.get("image") not in (None, path.name):
        raise CliError(f"{raw.name} next to {path} belongs to {meta['image']}; take a new screenshot")
    try:
        with Image.open(raw) as opened:
            image = opened.convert("RGB")
    except OSError as exc:
        raise CliError(f"cannot read {raw}: {exc}") from exc
    px, py = meta.get("pointer_x"), meta.get("pointer_y")
    pointer = (px, py) if isinstance(px, int) and isinstance(py, int) else None
    return Reference(
        image, int(meta.get("x", 0)), int(meta.get("y", 0)), str(meta.get("monitor", "all")),
        str(meta.get("cursor", "none")), pointer, str(path),
    )


def patch_box(size: tuple[int, int], center: tuple[int, int], radius: int = GUARD_RADIUS) -> tuple[int, int, int, int]:
    cx, cy = center
    width, height = size
    return max(0, cx - radius), max(0, cy - radius), min(width, cx + radius + 1), min(height, cy + radius + 1)


def patch_diff(
    a: Image.Image,
    b: Image.Image,
    center: tuple[int, int],
    radius: int = GUARD_RADIUS,
    masks: list[tuple[int, int, int, int]] = (),  # type: ignore[assignment]
    tolerance: int = PIXEL_TOLERANCE,
) -> float:
    """Share of pixels around `center` whose colour differs by more than `tolerance` in
    any channel (so a red-to-green swap of equal brightness counts), outside `masks`."""
    box = patch_box(a.size, center, radius)
    diff = ImageChops.difference(a.crop(box).convert("RGB"), b.crop(box).convert("RGB"))
    red, green, blue = diff.split()
    changed = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(lambda v: 255 if v > tolerance else 0)
    valid = Image.new("L", changed.size, 255)
    for left, top, right, bottom in masks:
        rect = (left - box[0], top - box[1], right - box[0], bottom - box[1])
        ImageDraw.Draw(changed).rectangle(rect, fill=0)
        ImageDraw.Draw(valid).rectangle(rect, fill=0)
    total = valid.histogram()[255]
    return changed.histogram()[255] / total if total else 0.0


def fingerprint(
    img: Image.Image,
    cells: tuple[int, int] = FINGERPRINT_CELLS,
    masks: list[tuple[int, int, int, int]] = (),  # type: ignore[assignment]
) -> list[int | None]:
    """Mean grey of each cell of a coarse grid over the image; masked cells are None."""
    cols, rows = cells
    values: list[int | None] = list(img.convert("L").resize(cells, Image.Resampling.BOX).tobytes())
    for left, top, right, bottom in masks:
        x0, x1 = max(0, int(left * cols / img.width)), min(cols, math.ceil(right * cols / img.width))
        y0, y1 = max(0, int(top * rows / img.height)), min(rows, math.ceil(bottom * rows / img.height))
        for row in range(y0, y1):
            for col in range(x0, x1):
                values[row * cols + col] = None
    return values


def fingerprint_diff(a: list[int | None], b: list[int | None], tolerance: int = CELL_TOLERANCE) -> float:
    """Share of cells (masked in neither) whose mean differs by more than `tolerance`."""
    pairs = [(p, q) for p, q in zip(a, b) if p is not None and q is not None]
    if not pairs:
        return 0.0
    return sum(1 for p, q in pairs if abs(p - q) > tolerance) / len(pairs)


GUARDED_GLIDE_MAX = 0.4  # seconds: a guarded glide stays short, so the check stays fresh


@contextlib.contextmanager
def guarded(args: argparse.Namespace, backend: Backend, kind: str):
    """Wrap a --seen check and the action it protects. The input device is opened first and
    the glide shortened, so little time passes between the check and the action (on
    Wayland a new device alone takes AI_DESKTOP_UINPUT_DELAY, 0.5 s)."""
    if getattr(args, "seen", None) is None:
        yield
        return
    backend.glide_max = GUARDED_GLIDE_MAX
    with keep_devices():
        backend.prepare(kind)
        yield


def guard_seen(args: argparse.Namespace, backend: Backend, *points: tuple[int, int] | None) -> dict:
    """With --seen, abort before acting if the screen no longer looks like the screenshot
    the agent read: around each of `points` for coordinate commands (both ends of a drag),
    over the whole capture for keys and typing (no points; None entries are skipped).
    Returns the `seen` fields for the OK line ({} without --seen); `seen_diff` is the
    largest difference found."""
    spec = getattr(args, "seen", None)
    if spec is None:
        return {}
    targets = [p for p in points if p is not None]
    ref = load_reference(spec)
    # Capture the pointer the way the reference did: a pointer at rest then matches
    # itself even where its position is unknown (Wayland), and the masks cover it
    # where it is known.
    fresh = capture_screen(backend, ref.monitor, cursor=ref.cursor == "native")
    width, height = ref.image.size
    if fresh.image.size != ref.image.size or (fresh.x, fresh.y) != (ref.x, ref.y):
        raise CliError(
            f"screen layout changed since {ref.path}: it covered {width}x{height} at ({ref.x}, {ref.y}), "
            f"the screen is now {fresh.image.width}x{fresh.image.height} at ({fresh.x}, {fresh.y}); "
            "take a new screenshot",
            seen=ref.path,
        )
    masks = ref.masks(known_pointer(backend, list_monitors(backend))[0])
    if targets:
        diff = 0.0
        for x, y in targets:
            local = (x - ref.x, y - ref.y)
            if not (0 <= local[0] < width and 0 <= local[1] < height):
                raise CliError(
                    f"target ({x}, {y}) is outside the seen screenshot, which covers "
                    f"{ref.x}..{ref.x + width - 1}, {ref.y}..{ref.y + height - 1}",
                    seen=ref.path,
                )
            here = patch_diff(ref.image, fresh.image, local, GUARD_RADIUS, masks)
            if here > GUARD_PATCH_MAX:
                left, top, right, bottom = patch_box(ref.image.size, local)
                raise CliError(
                    f"screen changed near ({x}, {y}) since {ref.path}: {here:.0%} of the "
                    f"{right - left}x{bottom - top} px area around it differs (limit {GUARD_PATCH_MAX:.0%}); "
                    "take a new screenshot before acting",
                    seen=ref.path, changed=round(here, 3), limit=GUARD_PATCH_MAX,
                )
            diff = max(diff, here)
    else:
        diff = fingerprint_diff(fingerprint(ref.image, masks=masks), fingerprint(fresh.image, masks=masks))
        if diff > GUARD_SCREEN_MAX:
            raise CliError(
                f"screen changed since {ref.path}: {diff:.0%} of the screen differs "
                f"(limit {GUARD_SCREEN_MAX:.0%}); take a new screenshot before acting",
                seen=ref.path, changed=round(diff, 3), limit=GUARD_SCREEN_MAX,
            )
        # A dialog or menu takes the keyboard but may cover only a few percent of a big
        # screen, so any compact changed area of dialog size counts too. Toasts, clocks
        # and carets are smaller.
        dialog_w, dialog_h = GUARD_BLOCK_MIN
        for left, top, w, h in changed_blocks(ref.image, fresh.image, masks):
            if w >= dialog_w and h >= dialog_h:
                region = [ref.x + left, ref.y + top, w, h]
                raise CliError(
                    f"screen changed since {ref.path}: a {w}x{h} px area at ({region[0]}, {region[1]}) "
                    "is new or different (a dialog or menu?), so keys would go somewhere else; "
                    "take a new screenshot before acting",
                    seen=ref.path, changed=round(diff, 3), limit=GUARD_SCREEN_MAX, region=region,
                )
    return {"seen": ref.path, "seen_diff": round(diff, 3)}


GUARD_BLOCK_CELL = 16  # px per cell of the grid changed_blocks compares
GUARD_BLOCK_MIN = (200, 150)  # the smallest changed area (w, h) a keyboard action refuses


def changed_blocks(
    a: Image.Image,
    b: Image.Image,
    masks: list[tuple[int, int, int, int]] = (),  # type: ignore[assignment]
    cell: int = GUARD_BLOCK_CELL,
    tolerance: int = CELL_TOLERANCE,
) -> list[tuple[int, int, int, int]]:
    """Connected areas where `b` differs from `a`, as (left, top, width, height) in image px,
    measured on a grid of `cell`-px cells (so each side is rounded out to whole cells)."""
    cols, rows = max(1, a.width // cell), max(1, a.height // cell)
    fa, fb = fingerprint(a, (cols, rows), masks), fingerprint(b, (cols, rows), masks)
    changed = {
        i for i, (p, q) in enumerate(zip(fa, fb)) if p is not None and q is not None and abs(p - q) > tolerance
    }
    sx, sy = a.width / cols, a.height / rows
    blocks = []
    while changed:
        stack = [changed.pop()]
        c0 = c1 = stack[0] % cols
        r0 = r1 = stack[0] // cols
        while stack:
            i = stack.pop()
            r, c = divmod(i, cols)
            c0, c1, r0, r1 = min(c0, c), max(c1, c), min(r0, r), max(r1, r)
            for n, ok in ((i - 1, c > 0), (i + 1, c < cols - 1), (i - cols, r > 0), (i + cols, r < rows - 1)):
                if ok and n in changed:
                    changed.remove(n)
                    stack.append(n)
        left, top = round(c0 * sx), round(r0 * sy)
        blocks.append((left, top, round((c1 + 1) * sx) - left, round((r1 + 1) * sy) - top))
    return blocks


def poll_until(
    capture, done, timeout: float, interval: float, min_polls: int = 1
) -> tuple[Capture, int, float, bool]:
    """Capture until done(capture) is true or `timeout` seconds pass, but at least
    `min_polls` times (`wait --stable` needs two captures to compare).

    Returns (last capture, number of captures, seconds taken, whether done).
    """
    start = _now()
    deadline = start + timeout
    polls = 0
    while True:
        cap = capture()
        polls += 1
        if done(cap):
            return cap, polls, _now() - start, True
        if polls >= min_polls and _now() >= deadline:
            return cap, polls, _now() - start, False
        check_cancel()
        _sleep(min(interval, max(0.0, deadline - _now())))  # never sleep past the deadline


SHOT_SETTLE_MAX = 10.0


def shot_settle() -> float:
    try:
        value = float(os.environ.get("AI_DESKTOP_SHOT_DELAY", SHOT_SETTLE))
    except ValueError:
        return SHOT_SETTLE  # a typo here must not fail an action that already happened
    return min(max(0.0, value), SHOT_SETTLE_MAX) if math.isfinite(value) else SHOT_SETTLE


def shot_fields(backend_for, monitor: str, cap: Capture | None = None) -> dict:
    """A gridded screenshot taken after an action, as the `shot` fields of its OK line.

    `backend_for` returns the backend (it is only built here, inside the try). The action
    has already happened, so any failure is reported, not raised: an agent that saw an
    error would retry and click twice.
    """
    try:
        info = take_screenshot(backend_for(), monitor=monitor, grid=True, cap=cap)
    except Exception as exc:
        return {"shot": "error", "shot_error": str(exc) or type(exc).__name__}
    return {"shot": info["path"], "screenshot": info}


def live_module():
    """ai_desktop_live, bound to this module (which may be __main__ when run as a script)."""
    import ai_desktop_live

    ai_desktop_live.bind(sys.modules[__name__])
    return ai_desktop_live


OBSERVE_PREPARED = None  # the screen just before an action with --observe (set by execute)


def finish(args: argparse.Namespace, cmd: str, backend: Backend | None = None, **fields) -> None:
    """The OK line of an action, plus what it changed with --observe, or a fresh screenshot
    with --shot."""
    monitor = getattr(args, "shot", None)
    if getattr(args, "observe", False) and OBSERVE_PREPARED is not None:
        try:
            fields.update(live_module().after_action(backend or get_backend(args.backend), OBSERVE_PREPARED))
        except CliError:
            raise
        except Exception as exc:  # the action happened: report, don't fail
            fields.update(changed="error", observe_error=f"{type(exc).__name__}: {exc}")
    elif monitor is not None:
        _sleep(shot_settle())
        fields.update(shot_fields(lambda: backend or get_backend(args.backend), monitor))
    status(cmd, **fields)


# --- Commands ----------------------------------------------------------------------------


def changed_region(ref: Reference, cap: Capture, masks: list) -> list[int] | None:
    """[x, y, width, height] in screen coordinates around every pixel of `cap` that differs
    from the reference by more than PIXEL_TOLERANCE (outside `masks`), or None when nothing
    does. Pixel-exact on purpose: a ticked checkbox must count. A different layout counts
    as the whole capture."""
    if cap.image.size != ref.image.size or (cap.x, cap.y) != (ref.x, ref.y) or cap.monitor != ref.monitor:
        return [cap.x, cap.y, cap.image.width, cap.image.height]
    diff = ImageChops.difference(ref.image, cap.image.convert("RGB"))
    changed = ImageChops.lighter(ImageChops.lighter(*diff.split()[:2]), diff.split()[2])
    changed = changed.point(lambda v: 255 if v > PIXEL_TOLERANCE else 0)
    for rect in masks:
        ImageDraw.Draw(changed).rectangle(rect, fill=0)
    box = changed.getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    return [ref.x + left, ref.y + top, right - left, bottom - top]


def cmd_screenshot(args: argparse.Namespace) -> None:
    if args.zoom is not None and args.region is not None:
        raise CliError("give --zoom or --region, not both", code=2)
    factor = args.factor or (1 if args.region is not None else ZOOM_FACTOR)
    backend = get_backend(args.backend)
    cap = None
    marked = None
    if args.marks:
        ui = ui_module()
        cap = capture_screen(backend, args.monitor, cursor=args.cursor == "auto")
        snap = ui.snapshot("active", backend, cap if args.monitor == "all" else None)
        marked = [e for e in snap["elements"] if e["where"] != "window"]
        cap = cap._replace(image=ui.draw_marks(cap.image, cap.x, cap.y, marked))
    if args.if_changed is not None:
        if args.zoom is not None or args.region is not None:
            raise CliError("--if-changed compares whole screenshots; leave out --zoom and --region", code=2)
        try:
            ref = load_reference(args.if_changed)
        except CliError:
            ref = None  # nothing to compare with: take one
        cap = capture_screen(backend, args.monitor, cursor=args.cursor == "auto")
        region = None
        if ref is not None:
            region = changed_region(ref, cap, ref.masks(known_pointer(backend, list_monitors(backend))[0]))
            if region is None:
                status("screenshot", unchanged=True, path=ref.path, x=ref.x, y=ref.y, monitor=ref.monitor)
                return
    fields = take_screenshot(
        backend,
        cap=cap,
        monitor=args.monitor,
        grid=args.grid or args.zoom is not None or args.region is not None,
        spacing=args.spacing,
        zoom=args.zoom,
        radius=args.radius,
        factor=factor,
        region=args.region,
        cursor=args.cursor,
        max_width=args.max_width,
        output=args.output,
    )
    if marked is not None:
        fields["marks"] = len(marked)
    if args.if_changed is not None:
        fields = {"unchanged": False, **fields}
        if region is not None:  # where, so an agent can look closer at just that part
            fields.update(changed_x=region[0], changed_y=region[1], changed_width=region[2], changed_height=region[3])
    status("screenshot", **fields)


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
            mb=round(sum(st.st_size for _, st in store_files(include_raw=True)) / 1048576, 1),
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
        files = store_files(include_raw=True)  # sidecars are deleted and counted too
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


def cmd_mcp(args: argparse.Namespace) -> None:
    try:
        import ai_desktop_mcp
    except ImportError as exc:
        raise CliError(
            "the MCP server needs Python 3.10+ and the mcp extra: pip install 'ai-desktop[mcp]'"
        ) from exc
    # Each tool call parses only its own arguments, so global flags given to `mcp` are
    # handed on through the environment they also read.
    os.environ["AI_DESKTOP_BACKEND"] = args.backend
    if getattr(args, "instant", False):
        os.environ["AI_DESKTOP_HUMAN"] = "0"
    ai_desktop_mcp.serve(max_width=args.max_width)


def cmd_log(args: argparse.Namespace) -> None:
    entries = read_log_entries(args.limit)
    status("log", path=str(log_dir()), count=len(entries), entries=entries)
    if text_mode():
        for entry in entries:
            print(format_log_entry(entry))


def regrid_source(src: Path) -> tuple[Image.Image, tuple[int, int], float]:
    """(image, global origin, image px per screen px) to draw a fresh grid on.

    A screenshot from this tool is re-gridded from its un-gridded copy (cut back to the
    zoomed area for a zoom), so the labels stay screen coordinates; an un-gridded copy
    or a screenshot without one uses its own metadata. Anything else starts at (0, 0).
    """
    own = read_image_meta(src) or {}
    raw = sidecar_path(src)
    linked = False
    if not raw.is_file() and isinstance(own.get("raw"), str) and (src.parent / own["raw"]).is_file():
        raw, linked = src.parent / own["raw"], True  # a `grid` output points at the original copy
    raw_meta = read_image_meta(raw) if raw != src and raw.is_file() else None
    if raw_meta is not None and not linked and raw_meta.get("image") not in (None, src.name):
        raw_meta = None  # that copy belongs to another screenshot with the same stem
    if raw_meta is not None:
        with Image.open(raw) as opened:
            img = opened.convert("RGB")
        origin = (int(raw_meta["x"]), int(raw_meta["y"]))
        if not own.get("zoom") and not own.get("region"):
            return img, origin, 1.0
        scale = float(own["image_scale"])
        box = (
            int(own["x"]), int(own["y"]),
            int(own["x"]) + round(own["width"] / scale), int(own["y"]) + round(own["height"] / scale),
        )
        cap = Capture(img, origin[0], origin[1], 1.0, "all")
        return zoom_crop(cap, box, scale), (box[0], box[1]), scale
    with Image.open(src) as opened:
        img = opened.convert("RGB")
    if own:
        return img, (int(own.get("x", 0)), int(own.get("y", 0))), float(own.get("image_scale", 1.0))
    return img, (0, 0), 1.0


def cmd_grid(args: argparse.Namespace) -> None:
    src = Path(args.input).expanduser()
    if not src.is_file():
        raise CliError(f"input image not found: {src}")
    img, origin, scale = regrid_source(src)
    limit = max_width_setting() if args.max_width is None else args.max_width
    shrink = view_scale(img.width, limit)
    k = 1 / shrink
    spacing = args.spacing
    if spacing is None:
        pad = max(LABEL_PAD, round(LABEL_PAD * k))
        box_w, _ = label_metrics(grid_font(img.size, k), origin, (img.width / scale, img.height / scale), pad)
        candidates = AUTO_SPACINGS if scale == 1 else ZOOM_SPACINGS
        spacing = auto_spacing(max(img.size) / scale, box_w, k, scale, candidates)
    gridded = draw_grid(img, spacing, origin, k, pixel_scale=scale)
    if shrink < 1:
        size = (max(1, round(gridded.width * shrink)), max(1, round(gridded.height * shrink)))
        gridded = gridded.resize(size, Image.Resampling.LANCZOS)
    own = read_image_meta(src) or {}
    raw = sidecar_path(src)
    if raw == src:  # the input is an un-gridded copy itself
        raw_name = src.name
    elif raw.is_file():  # a screenshot next to its copy
        raw_name = raw.name
    else:  # an earlier grid output, which links to the copy (or nothing)
        raw_name = own.get("raw") if isinstance(own.get("raw"), str) else None
    meta = {
        "version": 1, "x": origin[0], "y": origin[1], "width": gridded.width, "height": gridded.height,
        "image_scale": round(scale * shrink, 4), "spacing": spacing, "monitor": own.get("monitor"),
        "zoom": own.get("zoom"), "raw": raw_name, "kind": "grid",
    }
    target = Path(args.output).expanduser() if args.output else src.with_name(f"{src.stem}_grid.png")
    # The link to the raw copy is a bare name next to the output; elsewhere it isn't found.
    if raw_name and target.expanduser().resolve().parent != src.resolve().parent:
        meta["raw"] = None
    path = save_image(gridded, target, meta)
    status(
        "grid", path=path, width=gridded.width, height=gridded.height, spacing=spacing,
        x=origin[0], y=origin[1], image_scale=round(scale * shrink, 4),
    )


def ui_module():
    import ai_desktop_ui

    ai_desktop_ui.bind(sys.modules[__name__])
    return ai_desktop_ui


TARGET_OPTIONS = ("ref", "name", "text", "target", "mark")


def resolve_target(args: argparse.Namespace, backend: Backend) -> dict:
    """Where a click by --ref/--name/--text/--target/--mark goes: {"x", "y", "found_by", ...}
    for a screen point, or {"element": ...} for an element only accessibility can press."""
    ui = ui_module()
    given = [o for o in TARGET_OPTIONS if getattr(args, o, None) is not None]
    if len(given) > 1:
        raise CliError("give one of --ref, --name, --text, --target, --mark", code=2)
    kind = given[0]

    def from_element(element, found_by):
        if element["where"] == "window":
            return {"element": element, "found_by": found_by}
        x, y = ui.center(element["rect"])
        return {"x": x, "y": y, "found_by": found_by, "ref": element["id"], "name": element["name"][:60]}

    def from_text(query):
        cap = capture_screen(backend, "all")
        matches = ui.find_text(ui.screen_text(cap), query)
        nth = getattr(args, "nth", None)
        if nth is not None:
            if not 1 <= nth <= len(matches):
                raise CliError(f"--nth {nth}: {len(matches)} match(es) for {query!r}")
            matches = [matches[nth - 1]]
        if len(matches) > 1:
            listing = "; ".join(f'{m["id"]} {json.dumps(m["text"][:40])} {m["x"]},{m["y"]}' for m in matches[:6])
            raise CliError(f"{len(matches)} places show {query!r}; add --nth N or be more precise: {listing}")
        if not matches:
            return None
        m = matches[0]
        return {"x": m["x"] + m["w"] // 2, "y": m["y"] + m["h"] // 2, "found_by": "text", "text": m["text"][:60]}

    if kind in ("ref", "mark"):
        return from_element(ui.by_ref(str(getattr(args, kind))), "accessibility")
    if kind == "name":
        return from_element(ui.by_name(args.name, getattr(args, "role", None), backend=backend), "accessibility")
    if kind == "text":
        found = from_text(args.text)
        if found is None:
            raise CliError(f"no text {args.text!r} on the screen")
        return found
    # --target: accessibility, then text, then the local vision model
    tried = []
    if ui.a11y_supported() is None:
        try:
            return from_element(ui.by_name(args.target, getattr(args, "role", None), backend=backend), "accessibility")
        except CliError as exc:
            tried.append(f"accessibility: {exc}")
    try:
        found = from_text(args.target)
        if found is not None:
            return found
        tried.append("text: not on the screen")
    except CliError as exc:
        tried.append(f"text: {exc}")
    try:
        found = vision_module().locate_target(backend, args.target)
        if found is not None:
            return found
        tried.append("vision: not found")
    except CliError as exc:
        tried.append(f"vision: {exc}")
    raise CliError(f"could not find {args.target!r} (" + "; ".join(tried) + ")")


def vision_module():
    import ai_desktop_vision

    ai_desktop_vision.bind(sys.modules[__name__])
    return ai_desktop_vision


def cmd_click(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    found = {}
    if any(getattr(args, o, None) is not None for o in TARGET_OPTIONS):
        if args.x is not None or args.y is not None:
            raise CliError("give --x/--y or a target (--ref, --name, --text, --target, --mark), not both", code=2)
        found = resolve_target(args, backend)
        if "element" in found:  # no screen position: press it through accessibility
            element = found.pop("element")
            with guarded(args, backend, "pointer"):
                seen = guard_seen(args, backend)
                done = ui_module().act(element, "press")
            finish(args, "click", backend, ref=element["id"], name=element["name"][:60], via="accessibility",
                   did=done.get("did"), **found, **seen)
            return
        args.x, args.y, args.monitor = found.pop("x"), found.pop("y"), None
    elif args.x is None or args.y is None:
        raise CliError("give --x and --y, or a target: --ref, --name, --text, --target or --mark", code=2)
    x, y = to_global(backend, args.monitor, args.x, args.y)
    backend.check_point(x, y)
    clicks = 2 if args.double else 1
    with guarded(args, backend, "pointer"):
        seen = guard_seen(args, backend, (x, y))
        backend.click(x, y, args.button, clicks)
    finish(
        args, "click", backend, x=x, y=y, button=args.button, clicks=clicks,
        **_monitor_fields(args), **found, **seen,
    )


def cmd_move(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    x, y = to_global(backend, args.monitor, args.x, args.y)
    backend.check_point(x, y)
    with guarded(args, backend, "pointer"):
        seen = guard_seen(args, backend, (x, y))
        backend.move(x, y)
    finish(args, "move", backend, x=x, y=y, **_monitor_fields(args), **seen)


def cmd_drag(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    x1, y1 = to_global(backend, args.monitor, args.x1, args.y1)
    x2, y2 = to_global(backend, args.monitor, args.x2, args.y2)
    backend.check_point(x1, y1)
    backend.check_point(x2, y2)
    with guarded(args, backend, "pointer"):
        seen = guard_seen(args, backend, (x1, y1), (x2, y2))
        backend.drag(x1, y1, x2, y2, args.button, args.duration)
    finish(
        args, "drag", backend, x1=x1, y1=y1, x2=x2, y2=y2, button=args.button,
        duration=args.duration, **_monitor_fields(args), **seen,
    )


def check_typeable(text: str) -> None:
    bad = sorted({c for c in text if c not in TYPEABLE})
    if bad:
        raise CliError("cannot type these characters (ASCII only): " + " ".join(map(repr, bad)))


def hotkey_keys(backend: Backend, spec: str) -> list:
    """The backend's keys for a combination like ctrl+shift+t."""
    names = spec.split("+")
    if any(not n.strip() for n in names):
        raise CliError(f"bad hotkey {spec!r}; use names joined by '+', e.g. ctrl+shift+t")
    names = [n.strip() for n in names]
    # A single capital letter means shift+letter (for `key` and typing), but in a
    # combination "Ctrl+C" means ctrl+c: Ctrl+Shift+C would open Chrome's inspector and
    # Ctrl+Shift+W close the whole window. An explicit "shift" still adds shift.
    if any(normalize_key(n) in ("ctrl", "alt", "meta") for n in names):
        names = [n.lower() if len(n) == 1 else n for n in names]
    return [backend.resolve_key(n) for n in names]


def scroll_point(args: argparse.Namespace, backend: Backend) -> tuple[int | None, int | None]:
    if (args.x is None) != (args.y is None):
        raise CliError("give both --x and --y, or neither")
    if args.monitor is not None and args.x is None:
        raise CliError("--monitor needs --x and --y")
    if args.x is None:
        return None, None
    x, y = to_global(backend, args.monitor, args.x, args.y)
    backend.check_point(x, y)
    return x, y


def precheck(args: argparse.Namespace, backend_for) -> None:
    """Every check a command makes before it acts, without acting: `run` calls this for
    all steps first, so a bad later step fails before an earlier one has clicked."""
    cmd = args.command
    if cmd == "type":
        check_typeable(args.text)
    elif cmd == "key":
        backend_for().resolve_key(args.key)
    elif cmd == "hotkey":
        hotkey_keys(backend_for(), args.keys)
    elif cmd in ("click", "move") and args.x is not None and args.y is not None:
        backend = backend_for()
        backend.check_point(*to_global(backend, args.monitor, args.x, args.y))
    elif cmd == "drag":
        backend = backend_for()
        backend.check_point(*to_global(backend, args.monitor, args.x1, args.y1))
        backend.check_point(*to_global(backend, args.monitor, args.x2, args.y2))
    elif cmd == "scroll":
        scroll_point(args, backend_for())
    seen = getattr(args, "seen", None)
    if seen is not None and seen != "latest" and not Path(seen).expanduser().is_file():
        raise CliError(f"--seen file not found: {seen}")  # "latest" may come from an earlier step


def cmd_type(args: argparse.Namespace) -> None:
    check_typeable(args.text)
    backend = get_backend(args.backend)
    with guarded(args, backend, "keyboard"):
        seen = guard_seen(args, backend)
        backend.type_text(args.text, args.interval)
    finish(args, "type", backend, chars=len(args.text), **seen)


def cmd_key(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    key = backend.resolve_key(args.key)
    with guarded(args, backend, "keyboard"):
        seen = guard_seen(args, backend)
        backend.press(key)
    finish(args, "key", backend, key=args.key, **seen)


def cmd_hotkey(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    keys = hotkey_keys(backend, args.keys)
    with guarded(args, backend, "keyboard"):
        seen = guard_seen(args, backend)
        backend.hotkey(keys)
    finish(args, "hotkey", backend, keys=args.keys, **seen)


def cmd_scroll(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    x, y = scroll_point(args, backend)
    notches = args.amount if args.direction == "up" else -args.amount
    with guarded(args, backend, "pointer"):
        seen = guard_seen(args, backend, (x, y) if x is not None else None)
        backend.scroll(notches, x, y)
    finish(
        args,
        "scroll",
        backend,
        direction=args.direction,
        amount=args.amount,
        x=x if x is not None else "current",
        y=y if y is not None else "current",
        **_monitor_fields(args),
        **seen,
    )


def _copy_with_sentinel(backend: Backend, keys: list) -> tuple[str, str]:
    """Press copy and return (clipboard text, sentinel). A sentinel is parked on the
    clipboard first so stale contents can't pass for a fresh copy; whatever goes wrong,
    the user's previous clipboard text comes back unless something new was copied."""
    previous = get_clipboard()
    sentinel = f"ai-desktop-copy-{uuid.uuid4().hex}"
    set_clipboard(sentinel)
    text = sentinel
    try:
        time.sleep(0.2)
        backend.hotkey(keys)
        deadline = time.monotonic() + 2
        while text == sentinel and time.monotonic() < deadline:
            time.sleep(0.1)
            text = get_clipboard()
    finally:
        if text == sentinel:
            try:
                set_clipboard(previous)
            except Exception:
                pass  # the original error matters more
    return text, sentinel


def cmd_copy(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    keys = _shortcut(backend, "c")
    with keep_devices():
        # The keyboard first: if it can't be opened, the clipboard hasn't been touched.
        backend.prepare("keyboard")
        text, sentinel = _copy_with_sentinel(backend, keys)
    if text == sentinel:
        raise CliError(
            "nothing was copied; select something first (terminals copy with ctrl+shift+c: "
            "use hotkey, then clipboard get)"
        )
    print_text("copy", text)


def cmd_paste(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    keys = _shortcut(backend, "v")
    with guarded(args, backend, "keyboard"):
        seen = guard_seen(args, backend)
        if args.text is not None:
            set_clipboard(args.text)
            time.sleep(0.2)  # let the compositor pick up the new clipboard owner
        backend.hotkey(keys)
    finish(args, "paste", backend, chars=len(args.text) if args.text is not None else "clipboard", **seen)


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
    url = normalize_url(args.url)
    profile = resolve_chrome_profile(args.profile)
    if sys.platform == "darwin":
        launch_chrome_macos(url, profile)
        return
    launcher, failed = "exec", None
    if sys.platform.startswith("linux") and not profile and not args.a11y:
        # Going through the desktop entry gives the launch an activation token, so the
        # window manager may bring the new window to the front instead of opening it
        # behind the terminal; it also finds a Flatpak Chrome, which has no binary on
        # PATH. Desktop entries can't carry --profile-directory.
        entry = find_desktop_entry(CHROME_DESKTOP_IDS)
        extra = [url] if url else []
        argv = None
        if entry and shutil.which("gtk-launch"):
            argv, launcher = ["gtk-launch", entry[0], *extra], "gtk-launch"
        elif entry and shutil.which("gio"):
            argv, launcher = ["gio", "launch", entry[1], *extra], "gio"
        if argv is not None:
            failed = run_launcher(argv)
            if failed is None:
                status(
                    "launch-chrome", exe=f"desktop entry {entry[0]}", user_data_dir="default",
                    profile="last-used", url=url or "none", launcher=launcher,
                )
                return
    exe = find_chrome_executable()
    argv = [exe]
    user_data = None
    if sys.platform == "win32":
        # Point at the existing signed-in data so cookies, logins and extensions carry over.
        found = chrome_user_data_dir()
        if found is not None:
            user_data = str(found)
            argv.append(f"--user-data-dir={user_data}")
    if profile:
        argv.append(f"--profile-directory={profile}")
    if args.a11y:
        argv.append("--force-renderer-accessibility")
    if url:
        argv.append(url)
    spawn_detached(argv)
    fields = dict(
        exe=exe, user_data_dir=user_data or "default", profile=profile or "last-used",
        url=url or "none", launcher="exec",
    )
    if failed is not None:
        fields["launcher_error"] = f"{launcher}: {failed}"
    status("launch-chrome", **fields)


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


class Settle:
    """Tracks whether captures have matched each other for `quiet` seconds in a row."""

    def __init__(self, quiet: float) -> None:
        self.quiet = quiet
        self.previous: list | None = None
        self.since: float | None = None  # when the current run of matching captures began
        self.last_at = 0.0
        self.diff = 0.0

    def feed(self, fp: list) -> bool:
        now = _now()
        if self.previous is not None:
            self.diff = fingerprint_diff(self.previous, fp)
            if self.diff <= STABLE_MAX:
                if self.since is None:
                    self.since = self.last_at
            else:
                self.since = None
        self.previous, self.last_at = fp, now
        return self.since is not None and now - self.since >= self.quiet


def cmd_wait(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    timeout = min(args.timeout, WAIT_MAX)
    quiet = min(args.quiet, timeout)  # never ask for more quiet than the time allowed
    settle = Settle(quiet)
    last = {"diff": 0.0, "changed": False}
    if args.settle and args.changed is None:
        raise CliError("--settle goes with --changed (--stable always waits for quiet)", code=2)
    if args.changed is not None:
        ref = load_reference(args.changed)
        monitor, mode = ref.monitor, "changed-then-stable" if args.settle else "changed"
        masks = ref.masks(known_pointer(backend, list_monitors(backend))[0])
        base = fingerprint(ref.image, masks=masks)

        def done(cap: Capture) -> bool:
            if not last["changed"]:
                last["diff"] = fingerprint_diff(base, fingerprint(cap.image, masks=masks))
                last["changed"] = last["diff"] > CHANGED_MIN
                if not (last["changed"] and args.settle):
                    return last["changed"]
            ready = settle.feed(fingerprint(cap.image))
            last["diff"] = settle.diff
            return ready

    else:
        monitor, mode = args.monitor, "stable"

        def done(cap: Capture) -> bool:
            ready = settle.feed(fingerprint(cap.image))
            last["diff"] = settle.diff
            return ready

    # --changed captures the pointer the way the reference did, so a pointer at rest
    # matches itself; --stable compares captures with each other and leaves it out.
    cursor = args.changed is not None and ref.cursor == "native"
    cap, polls, elapsed, ok = poll_until(
        lambda: capture_screen(backend, monitor, cursor=cursor),
        done, timeout, args.interval, min_polls=2 if mode == "stable" else 1,
    )
    fields = dict(mode=mode, polls=polls, elapsed=round(elapsed, 1), diff=round(last["diff"], 3))
    if not ok:
        if mode == "changed" or not last["changed"] and mode != "stable":
            message = (
                f"screen unchanged after {timeout:g} s ({last['diff']:.1%} differs, "
                f"needs more than {CHANGED_MIN:.0%})"
            )
        else:
            message = f"screen still changing after {timeout:g} s (last diff {last['diff']:.1%})"
        raise CliError(message, **fields)
    shot = getattr(args, "shot", None)
    if shot is not None:  # the last capture is the screenshot, unless it covers another monitor
        fields.update(shot_fields(lambda: backend, shot, cap if shot == monitor else None))
    status("wait", **fields)


RUN_FORBIDDEN = ("run", "mcp", "resume")


def split_step(step: str, i: int) -> list[str]:
    """Split a command-string step like a shell would. Quotes work the same everywhere;
    on Windows a backslash is an ordinary character, so paths keep theirs (POSIX rules
    would treat it as an escape and eat it)."""
    lex = shlex.shlex(step, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    if sys.platform == "win32":
        lex.escape = ""
    try:
        return list(lex)
    except ValueError as exc:
        # Name only the command: the rest of the step is often typed text, and this error
        # goes to the action log, which keeps typed text out.
        command = step.split(None, 1)[0] if step.split() else "?"
        raise CliError(
            f"step {i} ({command}): cannot split the command string ({exc}); "
            "quote it properly or use a list of arguments",
            code=2,
        ) from exc


def load_steps(args: argparse.Namespace) -> list[list[str]]:
    """The steps of `run`: a JSON list whose items are argv lists or command strings."""
    if args.steps is not None:
        text = args.steps
    elif args.file == "-":
        if QUIET:
            raise CliError("run --file - can't read stdin here (it is the MCP transport); pass --steps")
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).expanduser().read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CliError(f"cannot read --file {args.file}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise CliError(f"cannot read --file {args.file}: it is not UTF-8 text ({exc.reason})", code=2) from exc
    example = 'e.g. [["click","--x","500","--y","300"],["key","--key","enter"]]'
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CliError(f"steps must be a JSON list of commands ({exc}); {example}", code=2) from exc
    if not isinstance(data, list) or not data:
        raise CliError(f"steps must be a non-empty JSON list of commands; {example}", code=2)
    steps = []
    for i, step in enumerate(data, 1):
        if isinstance(step, str):
            argv = split_step(step, i)
        elif isinstance(step, list) and all(isinstance(a, (str, int, float)) for a in step):
            argv = [str(a) for a in step]
        else:
            raise CliError(f"step {i} must be a list of arguments or a command string; {example}", code=2)
        if not argv:
            raise CliError(f"step {i} is empty", code=2)
        steps.append(argv)
    return steps


def cmd_run(args: argparse.Namespace) -> None:
    """Run several commands in this one process, stopping at the first failure.

    Every step is checked before any runs. The uinput devices stay open between steps, so
    on Wayland only the first action of each kind pays the device delay. Each step is
    logged like a normal command (via "cli/run" or "mcp/run").
    """
    global LOG_VIA
    steps = load_steps(args)
    args.steps = [argv[0] for argv in steps]  # the log keeps only the command names
    prefix = ["--backend", args.backend] + (["--instant"] if getattr(args, "instant", False) else [])
    parsed_steps: list[argparse.Namespace] = []
    for i, argv in enumerate(steps, 1):
        parsed, payload = parse_quiet(prefix + argv)
        if parsed is None:
            problem = payload.get("error") or "--help/--version is not an action"
            if "unrecognized arguments" in problem:
                # Don't echo them: they are often the rest of an unquoted --text, and the
                # error goes to the action log.
                problem = (
                    "bad arguments: unrecognized arguments; in a command string quote text "
                    "that has spaces, or use a list of arguments"
                )
            raise CliError(f"step {i} ({argv[0]}): {problem}", code=2)
        if parsed.command in RUN_FORBIDDEN:
            raise CliError(f"step {i}: {parsed.command} can't be a step of run", code=2)
        parsed_steps.append(parsed)
    backends: dict[str, Backend] = {}
    for i, parsed in enumerate(parsed_steps, 1):
        def backend_for(name=parsed.backend):
            if name not in backends:
                backends[name] = get_backend(name)
            return backends[name]
        try:
            precheck(parsed, backend_for)
        except CliError as exc:
            raise CliError(f"step {i} ({parsed.command}): {exc}", code=exc.code) from exc

    results: list[dict] = []
    failed: tuple[int, int, str] | None = None
    outer_via = LOG_VIA
    LOG_VIA = f"{outer_via}/run"
    try:
        with keep_devices():
            for i, argv in enumerate(steps, 1):
                try:
                    code, payload = execute(prefix + argv, quiet=True)
                except Exception as exc:  # execute logs it, then re-raises unexpected errors
                    code, payload = EXIT_ERROR, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                payload = payload or {"ok": code == 0}
                results.append({
                    "step": i,
                    "command": argv[0],
                    "ok": code == 0,
                    "code": code,
                    "result": {k: v for k, v in payload.items() if k not in ("ok", "command", "code")},
                })
                if code != 0:
                    failed = (i, code, str(payload.get("error") or f"exit code {code}"))
                    break
    finally:
        LOG_VIA = outer_via
    if failed is not None:
        i, code, error = failed
        raise CliError(
            f"step {i} ({steps[i - 1][0]}) failed: {error}",
            code=code, steps=len(steps), done=i - 1, failed=i, results=results,
        )
    finish(args, "run", None, steps=len(steps), done=len(steps), results=results)
    if text_mode():  # like copy and clipboard get: the text after the first line
        for result in results:
            if isinstance(result["result"].get("text"), str):
                print(result["result"]["text"])


def cmd_stop(args: argparse.Namespace) -> None:
    path = stop_path()
    was_stopped = path.exists()
    private_dir(path.parent)
    note = {"since": datetime.now().isoformat(timespec="seconds"), "reason": args.reason or ""}
    with private_file(path) as fh:
        fh.write(json.dumps(note) + "\n")
    status("stop", stopped=True, was_stopped=was_stopped, path=str(path))


def cmd_resume(args: argparse.Namespace) -> None:
    path = stop_path()
    if not path.exists():
        status("resume", stopped=False, was_stopped=False)
        return
    # The stop is the human's brake, so only the human may release it: an agent's shell,
    # `run` and the MCP server have no terminal on stdin.
    if QUIET or sys.stdin is None or not sys.stdin.isatty():
        raise CliError(
            "resume must be run by the human in a terminal (ai-desktop resume); "
            "an agent must not release the stop",
            stopped=True,
        )
    path.unlink()
    status("resume", stopped=False, was_stopped=True)


def ensure_live(required: bool = False):
    """The live view of this process: started on demand inside the background service.
    Elsewhere (or where it can't stream) None, unless `required`."""
    live = live_module()
    if not IN_SERVICE:
        if required:
            raise CliError("the live view runs in the background service, which could not be started; "
                           "see ai-desktop service status")
        return None
    global FRAME_SOURCE
    if live.LIVE is None or not live.LIVE.running:
        try:
            source = live.choose_source()
        except CliError:
            if required:
                raise
            return None
        live.LIVE = live.LiveView(source)
        live.LIVE.start()
        FRAME_SOURCE = live.LIVE.frame
    return live.LIVE


def cmd_live(args: argparse.Namespace) -> None:
    global FRAME_SOURCE
    live = live_module()
    if args.action == "stop":
        if live.LIVE is not None:
            live.LIVE.stop()
            live.LIVE = None
        FRAME_SOURCE = None
        status("live", running=False)
        return
    if args.action == "status":
        if not IN_SERVICE or live.LIVE is None:
            status("live", running=False)
        else:
            status("live", **live.LIVE.status())
        return
    view = ensure_live(required=True)
    if args.action == "serve":
        if view.feed is None:
            view.feed = live.Feed(view, port=args.port)
        status("live", **view.status(), url=view.feed.url)
        return
    status("live", **view.status())


def cmd_observe(args: argparse.Namespace) -> None:
    live = live_module()
    backend = get_backend(args.backend)
    view = ensure_live() or live.view_for(backend)
    base = None
    if args.since is not None:
        cap = view.history.get(args.since)
        if cap is None:
            raise CliError(f"--since {args.since} is no longer known (only the last {live.HISTORY} frames are kept); "
                           "observe without --since")
        base = (args.since, cap)
    result = live.observe(view, base=base, timeout=min(args.timeout, WAIT_MAX), quiet=args.quiet, min_cells=args.min_cells)
    if args.describe and result.get("changed"):
        cap = result["_cap"]
        box = (result["changed_x"] - cap.x, result["changed_y"] - cap.y,
               result["changed_x"] - cap.x + result["changed_width"], result["changed_y"] - cap.y + result["changed_height"])
        try:
            result["description"] = vision_module().describe(cap.image.crop(box))
        except CliError as exc:
            result["description_error"] = str(exc)
    if args.no_shot:
        result.pop("_cap", None)
    else:
        result = live.change_shot(backend, result)
    status("observe", **result)


def cmd_watch(args: argparse.Namespace) -> None:
    """One line per settled change until --limit, --duration, Ctrl+C or the stop switch."""
    seq = None
    seen = 0
    end = None if args.duration is None else _now() + args.duration
    last_line = 0.0
    while args.limit is None or seen < args.limit:
        if end is not None and _now() >= end:
            break
        timeout = 30.0 if end is None else max(0.0, min(30.0, end - _now()))
        argv = ["--json", "observe", "--timeout", str(timeout), "--quiet", str(args.quiet)]
        argv += ["--min-cells", str(args.min_cells)] + (["--since", str(seq)] if seq is not None else [])
        argv += ["--no-shot"] if args.no_shot else []
        argv += ["--describe"] if args.describe else []
        payload = observe_via_service(argv)
        if not payload.get("ok"):
            raise CliError(payload.get("error") or "observe failed", code=int(payload.get("code") or 1))
        seq = payload.get("seq", seq)
        if payload.get("changed") is True:
            wait = args.gap - (_now() - last_line)
            if wait > 0:
                _sleep(wait)  # at most one line per --gap seconds, so a busy screen can't flood
            fields = {k: v for k, v in payload.items() if k not in ("ok", "command")}
            status("watch", **fields)
            last_line = _now()
            seen += 1
    status("watch", done=True, changes=seen)


def observe_via_service(argv: list[str]) -> dict:
    """Run an observe in the background service if it's there, else here; its payload."""
    try:
        import ai_desktop_service as svc

        result = svc.call(argv) if svc.enabled() else None
    except Exception:
        result = None
    if result is not None and result.get("stdout"):
        try:
            return json.loads(result["stdout"].splitlines()[0])
        except (ValueError, IndexError):
            pass
    code, payload = execute(argv, quiet=True)
    return payload or {"ok": code == 0, "code": code}


def cmd_locate(args: argparse.Namespace) -> None:
    vision = vision_module()
    backend = get_backend(args.backend)
    found = vision.locate(capture_screen(backend, "all"), args.what)
    if found.get("found") is False:
        raise CliError(f"could not find {args.what!r}: {found['reason']}", **found)
    status("locate", **found)


def cmd_describe(args: argparse.Namespace) -> None:
    vision = vision_module()
    backend = get_backend(args.backend)
    cap = capture_screen(backend, "all")
    img = cap.image
    if args.region is not None:
        x, y, w, h = args.region
        img = img.crop((x - cap.x, y - cap.y, x - cap.x + w, y - cap.y + h))
    print_text("describe", vision.describe(img, args.question or ""))


def cmd_text(args: argparse.Namespace) -> None:
    ui = ui_module()
    backend = get_backend(args.backend)
    phrases = ui.screen_text(capture_screen(backend, args.monitor), args.region)
    if args.find is not None:
        phrases = ui.find_text(phrases, args.find)
    status("text", count=len(phrases), items=phrases)
    if text_mode():
        for p in phrases:
            print(f'{p["id"]} {json.dumps(p["text"], ensure_ascii=False)} {p["x"]},{p["y"]} {p["w"]}x{p["h"]}')


def cmd_ui(args: argparse.Namespace) -> None:
    ui = ui_module()
    if args.action in ("enable", "disable"):
        done = ui.enable_accessibility(args.action == "enable")
        status("ui", action=args.action, **done, note="apps already running may need a restart to show their content")
        return
    why = ui.a11y_supported()
    if why:
        raise CliError(why)
    snap = ui.snapshot("all" if args.all else "active", get_backend(args.backend))
    elements = snap["elements"]
    if args.find is not None:
        q = ui.norm(args.find)
        elements = [e for e in elements if q in ui.norm(e["name"])]
    if args.role is not None:
        elements = [e for e in elements if e["role"] == args.role]
    windows = [f'{w["app"]}: {w["title"][:60]}' for w in snap["windows"]]
    status("ui", count=len(elements), windows=len(windows), elements=elements)
    if text_mode():
        for w in windows:
            print(f"# {w}")
        for e in elements:
            print(ui.element_line(e))


def _element_for(args, backend):
    ui = ui_module()
    if (args.ref is None) == (args.name is None):
        raise CliError("give --ref or --name", code=2)
    return ui.by_ref(args.ref) if args.ref is not None else ui.by_name(args.name, args.role, backend=backend)


def cmd_read(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    element = _element_for(args, backend)
    info = ui_module().act(element, "read")
    info.pop("ok", None)
    print_text("read", str(info.pop("text", "")), ref=element["id"], **info)


def cmd_set_text(args: argparse.Namespace) -> None:
    backend = get_backend(args.backend)
    element = _element_for(args, backend)
    with guarded(args, backend, "keyboard"):
        seen = guard_seen(args, backend)
        ui_module().act(element, "set-text", text=args.value)
    finish(args, "set-text", backend, ref=element["id"], name=element["name"][:60], chars=len(args.value), **seen)


def cmd_service(args: argparse.Namespace) -> None:
    try:
        import ai_desktop_service as svc
    except Exception as exc:
        raise CliError(f"the service module can't be loaded: {exc}") from exc
    if not svc.enabled():
        raise CliError("the service is off here (AI_DESKTOP_SERVICE=off, or no Unix sockets on this system)")
    info = svc.status()
    if args.action == "status":
        if info is None:
            status("service", running=False, socket=str(svc.socket_path()))
            return
        current = info.get("build") == svc.build_id()
        status("service", running=True, pid=info["pid"], requests=info["requests"], uptime=info["uptime"],
               current=current, socket=str(svc.socket_path()))
        return
    if args.action == "stop":
        if info is None:
            status("service", running=False, stopped=False)
            return
        svc.stop()
        status("service", running=False, stopped=True, pid=info["pid"])
        return
    if info is not None and info.get("build") == svc.build_id():  # start
        status("service", running=True, started=False, pid=info["pid"])
        return
    if info is not None:
        svc.stop()  # older code
        time.sleep(0.3)
    with contextlib.suppress(OSError):
        (svc.runtime_dir() / "spawned").unlink()  # an explicit start ignores the respawn gap
    if not svc.spawn():
        raise CliError(f"could not start the service; see {home_dir() / 'service.log'}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        info = svc.status()
        if info is not None:
            status("service", running=True, started=True, pid=info["pid"])
            return
        time.sleep(0.1)
    raise CliError(f"the service did not come up within 5 s; see {home_dir() / 'service.log'}")


def _check_service():
    try:
        import ai_desktop_service as svc
    except Exception as exc:
        return "warn", f"module missing: {exc}", "reinstall ai-desktop (pip install -e . in a checkout)"
    if not svc.enabled():
        return "ok", "off (AI_DESKTOP_SERVICE=off, or no Unix sockets here): every command runs by itself"
    info = svc.status()
    if info is None:
        return "ok", "not running; starts by itself on the next action"
    if info.get("build") != svc.build_id():
        return "ok", f"pid {info['pid']} runs older code; the next action replaces it"
    return "ok", f"pid {info['pid']}, {info['requests']} commands, up {info['uptime']} s"


def _check_tesseract():
    exe = shutil.which("tesseract")
    if exe:
        return "ok", exe
    return "warn", "not installed: text, click --text and click --target can't read the screen", "sudo apt install tesseract-ocr"


def _check_vision():
    info = vision_module().status()
    detail = f"{info['model']} at {info['url']}"
    if "size_gb" in info:
        detail += f" ({info['size_gb']} GB)"
        free = _available_gb()
        if free is not None and free < info["size_gb"] * 1.2:
            return "warn", detail + f", but only {free:.1f} GB of memory is free", "close programs before using locate or describe"
    return "ok", detail


def _available_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    except (OSError, ValueError):
        return None
    return None


def _check_live():
    why = live_module().stream_available()
    if why is None:
        return "ok", "GNOME screen cast available (ai-desktop live start)"
    if not is_wayland():
        return "ok", "polled captures (X11)"
    return "warn", f"no live stream: {why}"


def _check_stop_switch():
    path = stop_path()
    if not path.exists():
        return "ok", "not stopped"
    return "warn", f"STOPPED ({path})", "every action refuses to run; resume in a terminal: ai-desktop resume"


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


def _spacing(value: str) -> int | None:
    if value.strip().lower() == "auto":
        return None
    spacing = int(value)
    if spacing < MIN_SPACING:
        raise argparse.ArgumentTypeError(f"must be at least {MIN_SPACING}, or auto")
    return spacing


def _point(value: str) -> tuple[int, int]:
    try:
        x, y = (int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must look like X,Y, e.g. 1311,712") from exc
    return x, y


def _region(value: str) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must look like X,Y,W,H, e.g. 400,300,600,200") from exc
    if w < 1 or h < 1:
        raise argparse.ArgumentTypeError("width and height must be at least 1")
    return x, y, w, h


def _bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse


def _max_width(value: str) -> int:
    number = int(value)
    if number != 0 and number < MIN_MAX_WIDTH:
        raise argparse.ArgumentTypeError(f"must be 0 (never shrink) or at least {MIN_MAX_WIDTH}")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a number, not negative")
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
    flags.add_argument(
        "--instant",
        action="store_true",
        default=argparse.SUPPRESS,
        help="move the mouse straight to the target and type at a fixed --interval instead of "
        "the human-like path and pauses (env: AI_DESKTOP_HUMAN=0)",
    )
    flags.add_argument(
        "--seed",
        type=int,
        default=argparse.SUPPRESS,
        help="seed the random mouse path and typing pauses so a command repeats exactly "
        "(env: AI_DESKTOP_SEED)",
    )
    parser = argparse.ArgumentParser(
        prog="ai-desktop",
        description="See the desktop with a coordinate grid and control the mouse and keyboard.",
        epilog="WARNING: this gives the caller full control of the desktop and logged-in accounts.",
        parents=[flags],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # Options of the commands that act on the screen. SUPPRESS keeps them out of the
    # parsed arguments (and the action log) unless given.
    action_flags = argparse.ArgumentParser(add_help=False)
    action_flags.add_argument(
        "--seen",
        default=argparse.SUPPRESS,
        metavar="SCREENSHOT",
        help="the screenshot you read (a path, or latest): abort without acting if the screen "
        "around the target, or for keys the whole screen, no longer looks like it",
    )
    action_flags.add_argument(
        "--shot",
        nargs="?",
        const="all",
        default=argparse.SUPPRESS,
        metavar="MONITOR",
        help="take a gridded screenshot right after acting and print its path as shot= "
        "(optionally of one monitor)",
    )
    action_flags.add_argument(
        "--observe",
        action="store_true",
        default=argparse.SUPPRESS,
        help="after acting, wait (up to 3 s) for the screen to change and settle, and return "
        "what changed: changed_x/_y/_width/_height and shot= a screenshot of just that area",
    )

    def add(name: str, extra=(), **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[flags, *extra], **kwargs)

    p = add("screenshot", help="capture the screen, optionally with a coordinate grid")
    p.add_argument("--grid", action="store_true", help="overlay labeled grid lines")
    p.add_argument(
        "--marks", action="store_true",
        help="number the buttons, links and fields of the active window (from the accessibility "
        "tree) on the image; click one with click --mark N",
    )
    p.add_argument(
        "--region", type=_region, metavar="X,Y,W,H",
        help="only this area (screen coordinates), gridded with screen-coordinate labels; "
        "--factor enlarges it (default 1)",
    )
    p.add_argument(
        "--if-changed", nargs="?", const="latest", metavar="SCREENSHOT",
        help="if nothing changed since that screenshot (default: the newest), save nothing and "
        "print unchanged=True with its path",
    )
    p.add_argument(
        "--spacing",
        type=_spacing,
        default=None,
        help="grid spacing in screen px, or auto (default: 100 on ordinary screens, more on "
        "big ones, finer in a zoom)",
    )
    p.add_argument(
        "--monitor",
        default="all",
        help="all (default: every monitor in one image), primary, or an index from 'ai-desktop monitors'",
    )
    p.add_argument(
        "--zoom",
        type=_point,
        metavar="X,Y",
        help="show only the area around this screen point, enlarged, with a fine grid "
        "(labels are still screen coordinates); implies --grid",
    )
    p.add_argument(
        "--radius", type=_bounded_int(10, 2000), default=ZOOM_RADIUS,
        help=f"--zoom: screen px around the point (default {ZOOM_RADIUS})",
    )
    p.add_argument(
        "--factor", type=_bounded_int(1, 8), default=None,
        help=f"enlargement for --zoom (default {ZOOM_FACTOR}) or --region (default 1)",
    )
    p.add_argument(
        "--cursor",
        choices=("auto", "marker", "none"),
        default="auto",
        help="show the mouse pointer: auto (default) has the capture tool draw it where it "
        "can and draws a 'ptr' crosshair otherwise; marker always draws the crosshair",
    )
    p.add_argument(
        "--max-width",
        type=_max_width,
        default=None,
        help=f"shrink wider images to this width, labels kept legible (default "
        f"{DEFAULT_MAX_WIDTH} or AI_DESKTOP_MAX_WIDTH; 0 = never)",
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
    p.add_argument("--spacing", type=_spacing, default=None, help="grid spacing in px, or auto (default)")
    p.add_argument(
        "--max-width", type=_max_width, default=None,
        help=f"shrink wider images to this width (default {DEFAULT_MAX_WIDTH} or AI_DESKTOP_MAX_WIDTH; 0 = never)",
    )
    p.set_defaults(func=cmd_grid)

    acting = [action_flags]
    p = add("click", acting, help="click at a screen coordinate, or at something found by name or text "
            "(the mouse travels there along a human-like path)")
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    hidden = argparse.SUPPRESS  # left out of the parsed arguments (and the log) unless given
    p.add_argument("--ref", default=hidden, help="an element from 'ai-desktop ui', e.g. e12")
    p.add_argument("--mark", type=int, default=hidden, help="a numbered box from 'screenshot --marks' (same as --ref eN)")
    p.add_argument("--name", default=hidden, help="the accessible name of a button, link or field in the active window")
    p.add_argument("--role", default=hidden, help="with --name or --target: only elements of this role, e.g. 'push button'")
    p.add_argument("--text", default=hidden, help="text shown on the screen (found by text recognition)")
    p.add_argument("--nth", type=_positive_int, default=hidden, help="with --text: the N-th place it appears, top to bottom")
    p.add_argument("--target", default=hidden, help="a name, text or short description: tries accessibility, then "
                   "on-screen text, then the local vision model")
    p.add_argument("--button", choices=tuple(MOUSE_BUTTONS), default="left")
    p.add_argument("--double", action="store_true", help="double-click")
    p.add_argument(
        "--monitor",
        help="treat the coordinates as relative to this monitor's top-left "
        "(primary or an index from 'ai-desktop monitors'; default: global coordinates)",
    )
    p.set_defaults(func=cmd_click)

    p = add("move", acting, help="move the mouse to a screen coordinate along a human-like path")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.add_argument(
        "--monitor",
        help="treat the coordinates as relative to this monitor's top-left "
        "(primary or an index from 'ai-desktop monitors'; default: global coordinates)",
    )
    p.set_defaults(func=cmd_move)

    p = add(
        "drag", acting,
        help="press a mouse button at one point, move to another, release (the mouse travels "
        "to the start point along a human-like path)",
    )
    p.add_argument("--x1", type=int, required=True, help="start x")
    p.add_argument("--y1", type=int, required=True, help="start y")
    p.add_argument("--x2", type=int, required=True, help="end x")
    p.add_argument("--y2", type=int, required=True, help="end y")
    p.add_argument("--button", choices=tuple(MOUSE_BUTTONS), default="left")
    p.add_argument(
        "--duration", type=_non_negative_float, default=0.5,
        help="seconds the move with the button held takes (default 0.5)",
    )
    p.add_argument(
        "--monitor",
        help="treat the coordinates as relative to this monitor's top-left "
        "(primary or an index from 'ai-desktop monitors'; default: global coordinates)",
    )
    p.set_defaults(func=cmd_drag)

    p = add("type", acting, help="type text one key at a time with human-like pauses (printable ASCII, newline, tab)")
    p.add_argument("--text", required=True)
    p.add_argument(
        "--interval",
        type=_non_negative_float,
        default=0.02,
        help="seconds between keystrokes with --instant (default 0.02); otherwise 0.1-0.15 s "
        "per key and 0.25-0.5 s after a space, newline or tab",
    )
    p.set_defaults(func=cmd_type)

    p = add("key", acting, help="press one key, e.g. enter, tab, esc, space, backspace")
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_key)

    p = add("hotkey", acting, help="press a key combination, e.g. ctrl+c, cmd+t, alt+tab")
    p.add_argument("--keys", required=True)
    p.set_defaults(func=cmd_hotkey)

    p = add("scroll", acting, help="scroll the mouse wheel")
    p.add_argument("--direction", choices=("up", "down"), default="down")
    p.add_argument("--amount", type=_positive_int, default=3, help="wheel notches (default 3)")
    p.add_argument("--x", type=int, help="move here (along a human-like path) before scrolling")
    p.add_argument("--y", type=int, help="move here (along a human-like path) before scrolling")
    p.add_argument(
        "--monitor",
        help="treat the coordinates as relative to this monitor's top-left "
        "(primary or an index from 'ai-desktop monitors'; default: global coordinates)",
    )
    p.set_defaults(func=cmd_scroll)

    p = add("copy", help="press the copy shortcut and print what was copied")
    p.set_defaults(func=cmd_copy)

    p = add("paste", acting, help="press the paste shortcut, optionally putting --text on the clipboard first")
    p.add_argument("--text", help="text to paste; any characters, unlike type")
    p.set_defaults(func=cmd_paste)

    p = add("wait", help="wait until the screen stops changing (--stable) or changes (--changed)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stable", action="store_true", help="until two captures in a row match")
    mode.add_argument(
        "--changed", metavar="SCREENSHOT",
        help="until the screen differs from this screenshot (a path, or latest)",
    )
    p.add_argument(
        "--timeout", type=_non_negative_float, default=10.0,
        help=f"give up (exit 1) after this many seconds (default 10, at most {WAIT_MAX})",
    )
    p.add_argument(
        "--interval", type=_non_negative_float, default=0.5,
        help="seconds between captures (default 0.5; each capture also takes time)",
    )
    p.add_argument(
        "--quiet", type=_non_negative_float, default=1.0,
        help="seconds the screen must stay unchanged to count as stable (default 1)",
    )
    p.add_argument(
        "--settle", action="store_true",
        help="with --changed: after the change, also wait until the screen is stable",
    )
    p.add_argument("--monitor", default="all", help="--stable: all (default), primary, or an index")
    p.add_argument(
        "--shot", nargs="?", const="all", default=argparse.SUPPRESS, metavar="MONITOR",
        help="save the final capture as a gridded screenshot and print its path as shot=",
    )
    p.set_defaults(func=cmd_wait)

    p = add(
        "run",
        help="run several commands in one process, stopping at the first failure (faster: "
        "no start-up per step, and on Wayland the input devices stay open)",
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--steps",
        help='JSON list of commands, e.g. \'[["click","--x","500","--y","300"],["key","--key","enter"]]\'',
    )
    source.add_argument("--file", help="read the JSON steps from this file, or - for stdin")
    p.add_argument(
        "--shot", nargs="?", const="all", default=argparse.SUPPRESS, metavar="MONITOR",
        help="take a gridded screenshot after the last step and print its path as shot=",
    )
    p.set_defaults(func=cmd_run)

    p = add("clipboard", help="read or write the clipboard without pressing keys")
    p.add_argument("action", choices=("get", "set"))
    p.add_argument("--text", help="text for clipboard set")
    p.set_defaults(func=cmd_clipboard)

    p = add("position", help="print the mouse position and screen size")
    p.set_defaults(func=cmd_position)

    p = add("launch-chrome", help="open Google Chrome with your signed-in profile")
    p.add_argument("--url", help="page to open")
    p.add_argument("--profile", help="profile directory, e.g. 'Profile 1' (default: last used)")
    p.add_argument("--a11y", action="store_true",
                   help="turn on Chrome's accessibility tree, so 'ai-desktop ui' sees the page")
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

    p = add(
        "stop",
        help="stop ai-desktop: every action refuses to run, and a running one aborts at its next "
        "pause (works everywhere, Wayland included); undo with resume",
    )
    p.add_argument("--reason", help="a note kept in the stop file")
    p.set_defaults(func=cmd_stop)

    p = add("resume", help="lift a stop; only works from a terminal, so an agent can't lift it")
    p.set_defaults(func=cmd_resume)

    p = add("locate", help="find something without a name or text by its description (local vision model)")
    p.add_argument("what", help='e.g. "the gear icon at the top right"')
    p.set_defaults(func=cmd_locate)

    p = add("describe", help="describe the screen (or --region) in words (local vision model)")
    p.add_argument("--region", type=_region, metavar="X,Y,W,H")
    p.add_argument("--question", help="ask this instead of a general description")
    p.set_defaults(func=cmd_describe)

    p = add("text", help="the text on the screen with positions (text recognition; needs Tesseract)")
    p.add_argument("--region", type=_region, metavar="X,Y,W,H", help="only this area")
    p.add_argument("--find", help="only phrases equal to (or containing) this text")
    p.add_argument("--monitor", default="all", help="all (default), primary, or an index")
    p.set_defaults(func=cmd_text)

    p = add("ui", help="buttons, links and fields of the active window from the accessibility tree")
    p.add_argument("action", nargs="?", choices=("list", "enable", "disable"), default="list",
                   help="enable/disable: GNOME's accessibility switch, which apps such as Chrome need")
    p.add_argument("--all", action="store_true", help="every visible window, not only the active one")
    p.add_argument("--find", help="only elements whose name contains this")
    p.add_argument("--role", help="only elements of this role, e.g. 'push button', 'link', 'entry'")
    p.set_defaults(func=cmd_ui)

    p = add("read", help="the text, value and state of an element (--ref from 'ai-desktop ui', or --name)")
    p.add_argument("--ref")
    p.add_argument("--name")
    p.add_argument("--role")
    p.set_defaults(func=cmd_read)

    p = add("set-text", acting, help="replace the text of a field through accessibility (any characters, any layout)")
    p.add_argument("--ref")
    p.add_argument("--name")
    p.add_argument("--role")
    p.add_argument("--value", required=True, help="the new text")
    p.set_defaults(func=cmd_set_text)

    p = add("live", help="the live view: a continuous screen stream in the background service")
    p.add_argument("action", choices=("start", "stop", "status", "serve"),
                   help="serve: also publish the screen and a viewer with a Stop button on 127.0.0.1")
    p.add_argument("--port", type=_bounded_int(0, 65535), default=0, help="serve: port (default: any free one)")
    p.set_defaults(func=cmd_live)

    p = add("observe", help="wait until the screen changes and settles; report what changed")
    p.add_argument("--since", type=int, help="compare with this earlier seq (from a previous observe)")
    p.add_argument("--timeout", type=_non_negative_float, default=10.0, help="seconds to wait for a change (default 10)")
    p.add_argument("--quiet", type=_non_negative_float, default=0.5, help="seconds without change that count as settled (default 0.5)")
    p.add_argument("--min-cells", type=_bounded_int(1, 10000), default=3,
                   help="ignore changes smaller than this many 16 px cells, like a blinking caret (default 3)")
    p.add_argument("--no-shot", action="store_true", help="report the area only, without a screenshot of it")
    p.add_argument("--describe", action="store_true", help="also describe what changed in words (local vision model)")
    p.set_defaults(func=cmd_observe)

    p = add("watch", help="print one line per settled change on the screen, until Ctrl+C")
    p.add_argument("--limit", type=_positive_int, help="stop after this many changes")
    p.add_argument("--duration", type=_non_negative_float, help="stop after this many seconds")
    p.add_argument("--quiet", type=_non_negative_float, default=0.5, help="seconds without change that count as settled")
    p.add_argument("--min-cells", type=_bounded_int(1, 10000), default=3, help="ignore smaller changes (16 px cells)")
    p.add_argument("--gap", type=_non_negative_float, default=1.0, help="at most one line per this many seconds (default 1)")
    p.add_argument("--no-shot", action="store_true", help="report areas only, without screenshots")
    p.add_argument("--describe", action="store_true", help="describe each change in words (local vision model)")
    p.set_defaults(func=cmd_watch)

    p = add(
        "service",
        help="the background service that keeps the input devices open between commands "
        "(starts by itself; AI_DESKTOP_SERVICE=off turns it off)",
    )
    p.add_argument("action", choices=("status", "start", "stop"))
    p.set_defaults(func=cmd_service)

    p = add("doctor", help="check that everything this platform needs is installed and permitted")
    p.set_defaults(func=cmd_doctor)

    p = add("log", help="show recent entries from the action log, newest first")
    p.add_argument("--limit", type=_positive_int, default=20, help="how many entries (default 20)")
    p.set_defaults(func=cmd_log)

    p = add("mcp", help="run as an MCP server over stdio (needs: pip install 'ai-desktop[mcp]')")
    p.add_argument(
        "--max-width",
        type=_max_width,
        default=None,
        help="shrink screenshots, including those of shot=true, wider than this many px "
        f"(default: AI_DESKTOP_MAX_WIDTH or {DEFAULT_MAX_WIDTH}; 0 = never)",
    )
    p.set_defaults(func=cmd_mcp)
    return parser


# Options whose value may begin with a dash: text to type or paste, window titles, a zoom
# point left of the primary monitor (-1420,400), the minus key. argparse would read such a
# value as another option, so `--text -la` is joined into the one token `--text=-la`.
DASH_VALUE_OPTIONS = ("--text", "--value", "--name", "--target", "--find", "--zoom", "--region", "--title", "--app", "--id", "--url", "--key", "--keys", "--reason", "--profile")


def join_dash_values(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in DASH_VALUE_OPTIONS and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            out.append(f"{token}={argv[i + 1]}")
            i += 2
            continue
        out.append(token)
        i += 1
    return out


def _argparse_error(text: str) -> str:
    """The reason in argparse's usage-plus-error output, on one line, with where to look."""
    for line in reversed(text.splitlines()):
        prog, sep, reason = line.partition(": error: ")
        if sep:
            return f"{reason.strip()} (see: {prog.strip()} --help)"
    return " ".join(text.split())


def parse_quiet(argv: list[str] | None) -> tuple[argparse.Namespace | None, dict | None]:
    """Parse without printing: (args, None), or (None, payload) for --help, --version and
    bad arguments. The payload carries `code` unless it is the 0 of --help/--version."""
    buffer = io.StringIO()
    argv = join_dash_values(sys.argv[1:] if argv is None else list(argv))
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            return build_parser().parse_args(argv), None
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        text = buffer.getvalue().strip()
        if code == 0:  # --help / --version
            return None, {"ok": True, "command": None, "text": text}
        return None, {"ok": False, "command": None, "error": f"bad arguments: {_argparse_error(text)}", "code": code}


def execute(argv: list[str] | None = None, quiet: bool = False) -> tuple[int, dict | None]:
    """Run one invocation and return (exit code, result payload).

    With quiet=True nothing is printed and bad arguments come back as a payload instead
    of a SystemExit; the MCP server runs commands this way. The action log entry is
    written here whatever the outcome.
    """
    global JSON_OUTPUT, QUIET, CURRENT_COMMAND, LAST_PAYLOAD, HUMAN_INPUT, _MONITOR_CACHE, OBSERVE_PREPARED
    saved = (JSON_OUTPUT, QUIET, CURRENT_COMMAND, HUMAN_INPUT, _MONITOR_CACHE, OBSERVE_PREPARED)
    OBSERVE_PREPARED = None
    _MONITOR_CACHE = {}
    started = time.monotonic()
    args = None
    code = 0
    LAST_PAYLOAD = None
    QUIET = quiet
    try:
        if quiet:
            args, payload = parse_quiet(argv)
            if args is None:
                LAST_PAYLOAD = payload
                code = payload.get("code", 0)
                return code, LAST_PAYLOAD
        else:
            args = build_parser().parse_args(join_dash_values(sys.argv[1:] if argv is None else list(argv)))
        args.backend = getattr(args, "backend", None) or os.environ.get("AI_DESKTOP_BACKEND", "auto")
        JSON_OUTPUT = bool(getattr(args, "json", False)) or os.environ.get(
            "AI_DESKTOP_JSON", ""
        ).strip().lower() in ("1", "true", "yes")
        CURRENT_COMMAND = args.command
        HUMAN_INPUT = human_enabled(args)
        try:
            if args.command not in STOP_EXEMPT:
                check_stop()
            seed_rng(args)
            if getattr(args, "observe", False):
                OBSERVE_PREPARED = live_module().before_action(get_backend(args.backend))
            args.func(args)
        except CliError as exc:
            code = fail(str(exc), exc.code, **exc.fields)
        except OSError as exc:
            code = fail(str(exc), EXIT_ERROR)
        except KeyboardInterrupt:
            code = fail("interrupted", 130)
        except subprocess.TimeoutExpired as exc:
            tool = exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) and exc.cmd else str(exc.cmd)
            code = fail(
                f"{Path(str(tool)).name} did not finish within {exc.timeout:g} s "
                "(a permission prompt, a locked screen, or a hung tool?)",
                EXIT_ERROR,
            )
        except Exception as exc:
            gui = sys.modules.get("pyautogui")
            if gui is not None and isinstance(exc, getattr(gui, "FailSafeException", ())):
                code = fail(
                    "failsafe triggered: the mouse is in a screen corner, so the action was "
                    "aborted. Move it away from the corners to continue.",
                    EXIT_FAILSAFE,
                )
            else:
                # Never a bare traceback: the caller still gets one ERROR line (or one JSON
                # object). AI_DESKTOP_DEBUG=1 adds the traceback on stderr.
                if os.environ.get("AI_DESKTOP_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
                    traceback.print_exc(file=sys.stderr)
                code = fail(f"internal error: {type(exc).__name__}: {exc}", EXIT_ERROR)
        return code, LAST_PAYLOAD
    finally:
        if args is not None:
            log_action(args, code, LAST_PAYLOAD, started)
        JSON_OUTPUT, QUIET, CURRENT_COMMAND, HUMAN_INPUT, _MONITOR_CACHE, OBSERVE_PREPARED = saved


def utf8_console() -> None:
    """Print UTF-8 whatever the console's encoding (a Windows pipe is cp1252 before
    Python 3.15), so a window title or copied text can't crash the output after the
    action has run."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass


def forward_to_service(argv: list[str] | None) -> int | None:
    """Hand the command to the background service, if one can take it (see
    ai_desktop_service.py); None means: run it here. Never fails a command."""
    if os.environ.get("AI_DESKTOP_SERVICE", "").strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        import ai_desktop_service
    except Exception:
        return None
    try:
        return ai_desktop_service.try_forward(argv)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    code = forward_to_service(argv)
    if code is not None:
        return code
    code = execute(argv)[0]
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader closed the pipe early (`ai-desktop windows | head -1`): that's fine,
        # but Python would print a warning at exit; send the rest of the output nowhere.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
