"""MCP server for ai-desktop: every CLI command as a tool, screenshots returned inline.

Start it with `ai-desktop mcp` (stdio transport). Commands run in-process through
`ai_desktop.execute(argv, quiet=True)`, so validation, error messages, exit codes and the
action log are exactly those of the CLI. Nothing in this module may write to stdout:
that is the MCP transport.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations
from PIL import Image as PILImage

try:  # an expected tool failure whose message is passed to the client as-is
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - older SDK layout
    ToolError = Exception  # type: ignore[assignment,misc]

import ai_desktop

DEFAULT_MAX_WIDTH = 1920

# Desktop actions must not interleave, and the CLI's module globals aren't thread-safe.
_LOCK = threading.Lock()

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

INSTRUCTIONS = """\
ai-desktop controls this computer's real desktop and its signed-in accounts.
Work in a loop: screenshot -> read the labelled grid -> ONE action -> screenshot again.
Grid labels are screen coordinates: pass them to click/move unchanged. On a multi-monitor
desktop use monitor="N" for a closer look at one screen.
Never type passwords, 2FA codes or payment details; ask the human first. Ask before
anything public or irreversible (posting, sending, buying, deleting, accepting terms).
An error mentioning the failsafe means the human aborted: stop and ask.
"""


class CommandError(ToolError):
    """A failed command; the SDK reports the message to the client as a tool error."""


def run_raw(argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Run one CLI command in-process; returns (exit code, payload) without raising."""
    ai_desktop.LOG_VIA = "mcp"
    with _LOCK, contextlib.redirect_stdout(sys.stderr):
        code, payload = ai_desktop.execute(argv, quiet=True)
    if payload is None:
        payload = {"ok": code == 0, "command": argv[0] if argv else None, "code": code}
    return code, payload


def run(argv: list[str]) -> dict[str, Any]:
    """Run one CLI command and return its payload; a failure becomes a tool error."""
    code, payload = run_raw(argv)
    if not payload.get("ok"):
        raise CommandError(payload.get("error") or f"exit code {code}")
    return payload


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _opt(flag: str, value: Any) -> list[str]:
    return [] if value is None else [flag, str(value)]


def inline_image(
    path: str, grid: bool, spacing: int, origin: tuple[int, int], max_width: int
) -> tuple[bytes, dict[str, Any]]:
    """PNG bytes for the client: gridded, and downscaled when wider than `max_width`.

    The file on disk is replaced by the gridded full-size image, so `screenshots latest`
    shows what the model saw. Labels are enlarged before downscaling so they stay
    legible; the labels, not the pixel positions, carry the coordinates.
    """
    with PILImage.open(path) as opened:
        img = opened.convert("RGB")
    width, height = img.size
    factor = max_width / width if max_width and width > max_width else 1.0
    if grid:
        img = ai_desktop.draw_grid(img, spacing, origin=origin, label_scale=1 / factor)
        img.save(path)
    if factor < 1:
        size = (max(1, round(width * factor)), max(1, round(height * factor)))
        img = img.resize(size, PILImage.Resampling.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    extra = {"inline_width": img.width, "inline_height": img.height, "inline_scale": round(factor, 4)}
    return buffer.getvalue(), extra


def build_server(default_max_width: int = DEFAULT_MAX_WIDTH) -> MCPServer:
    try:
        mcp = MCPServer("ai-desktop", instructions=INSTRUCTIONS)
    except TypeError:  # older/newer constructor without `instructions`
        mcp = MCPServer("ai-desktop")

    def tool(annotations: ToolAnnotations, **kwargs):
        return mcp.tool(annotations=annotations, structured_output=False, **kwargs)

    @tool(READ_ONLY)
    def screenshot(
        grid: bool = True, spacing: int = 100, monitor: str = "all", max_width: int | None = None
    ) -> list:
        """Capture the screen (all monitors by default) with a labelled coordinate grid.

        Returns the image inline plus JSON with the file path, its size and the global
        origin (x, y) of the capture. Grid labels ARE screen coordinates: pass them to
        click unchanged. monitor="1" (an index from the monitors tool) or "primary" zooms
        into one screen; spacing=50 draws finer lines. max_width only shrinks the inline
        copy (inline_scale tells by how much); the saved file stays full size.
        """
        limit = default_max_width if max_width is None else max_width
        payload = run(["screenshot", "--monitor", str(monitor)])
        png, extra = inline_image(payload["path"], grid, spacing, (payload["x"], payload["y"]), limit)
        payload.update(extra, grid=grid, spacing=spacing if grid else None)
        return [dumps(payload), Image(data=png, format="png")]

    @tool(ACTION)
    def click(
        x: int, y: int, button: str = "left", double: bool = False, monitor: str | None = None
    ) -> str:
        """Click at a screen coordinate (button: left, right or middle). With monitor="N",
        x and y are relative to that monitor's top-left instead of global."""
        argv = ["click", "--x", str(x), "--y", str(y), "--button", button] + _opt("--monitor", monitor)
        return dumps(run(argv + (["--double"] if double else [])))

    @tool(ACTION)
    def move(x: int, y: int, monitor: str | None = None) -> str:
        """Move the mouse to a screen coordinate without clicking (monitor-relative with monitor="N")."""
        return dumps(run(["move", "--x", str(x), "--y", str(y)] + _opt("--monitor", monitor)))

    @tool(ACTION)
    def drag(
        x1: int, y1: int, x2: int, y2: int, button: str = "left", duration: float = 0.5,
        monitor: str | None = None,
    ) -> str:
        """Press `button` at (x1, y1), move to (x2, y2) over `duration` seconds, release.
        Moves windows, sliders, selections. monitor="N" makes all points monitor-relative."""
        argv = ["drag", "--x1", str(x1), "--y1", str(y1), "--x2", str(x2), "--y2", str(y2)]
        argv += ["--button", button, "--duration", str(duration)] + _opt("--monitor", monitor)
        return dumps(run(argv))

    @tool(ACTION, name="type")
    def type_text(text: str, interval: float = 0.02) -> str:
        """Type printable ASCII text into the focused window. For other characters use paste."""
        return dumps(run(["type", "--text", text, "--interval", str(interval)]))

    @tool(ACTION)
    def key(key: str) -> str:
        """Press one key: enter, tab, esc, space, backspace, delete, arrows, home, end,
        pageup, pagedown, f1-f12, or a single character."""
        return dumps(run(["key", "--key", key]))

    @tool(ACTION)
    def hotkey(keys: str) -> str:
        """Press a combination joined with '+', e.g. ctrl+c, cmd+t, alt+tab, ctrl+shift+t."""
        return dumps(run(["hotkey", "--keys", keys]))

    @tool(ACTION)
    def scroll(
        direction: str = "down", amount: int = 3, x: int | None = None, y: int | None = None,
        monitor: str | None = None,
    ) -> str:
        """Scroll the mouse wheel `amount` notches up or down, optionally after moving to (x, y)
        (monitor-relative with monitor="N")."""
        argv = ["scroll", "--direction", direction, "--amount", str(amount)]
        return dumps(run(argv + _opt("--x", x) + _opt("--y", y) + _opt("--monitor", monitor)))

    @tool(ACTION)
    def copy() -> str:
        """Press the copy shortcut and return the copied text (error if nothing was selected)."""
        return dumps(run(["copy"]))

    @tool(ACTION)
    def paste(text: str | None = None) -> str:
        """Press the paste shortcut; with `text`, put it on the clipboard first (any characters)."""
        return dumps(run(["paste"] + _opt("--text", text)))

    @tool(READ_ONLY)
    def clipboard_get() -> str:
        """Read the clipboard text."""
        return dumps(run(["clipboard", "get"]))

    @tool(ACTION)
    def clipboard_set(text: str) -> str:
        """Put text on the clipboard without pressing any keys."""
        return dumps(run(["clipboard", "set", "--text", text]))

    @tool(READ_ONLY)
    def position() -> str:
        """Mouse position, which monitor it is on, and the primary screen size."""
        return dumps(run(["position"]))

    @tool(ACTION)
    def launch_chrome(url: str | None = None, profile: str | None = None) -> str:
        """Open the user's own signed-in Google Chrome, optionally at a URL or with a
        profile directory such as "Profile 1". Take a screenshot afterwards."""
        return dumps(run(["launch-chrome"] + _opt("--url", url) + _opt("--profile", profile)))

    @tool(READ_ONLY)
    def windows() -> str:
        """List open windows front-to-back with id, pid, app, title and global geometry."""
        return dumps(run(["windows"]))

    @tool(ACTION)
    def focus(
        title: str | None = None, app: str | None = None, pid: int | None = None, id: str | None = None
    ) -> str:
        """Bring a window to the front by a part of its title (or app name), app, pid or id."""
        argv = ["focus"] + _opt("--title", title) + _opt("--app", app) + _opt("--pid", pid) + _opt("--id", id)
        return dumps(run(argv))

    @tool(READ_ONLY)
    def monitors() -> str:
        """List monitors with their global coordinates; index 0 is the primary."""
        return dumps(run(["monitors"]))

    @tool(READ_ONLY)
    def doctor() -> str:
        """Check what this platform still needs (permissions, tools); rows with hints."""
        return dumps(run_raw(["doctor"])[1])

    @tool(READ_ONLY)
    def screenshots_latest() -> str:
        """Path of the newest screenshot in the store."""
        return dumps(run(["screenshots", "latest"]))

    @tool(READ_ONLY)
    def log(limit: int = 20) -> str:
        """Recent entries from the action log, newest first."""
        return dumps(run(["log", "--limit", str(limit)]))

    return mcp


def serve(max_width: int = DEFAULT_MAX_WIDTH) -> None:
    build_server(max_width).run()  # stdio
