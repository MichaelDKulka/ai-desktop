"""MCP server for ai-desktop: every CLI command as a tool, screenshots returned inline.

Start it with `ai-desktop mcp` (stdio transport). Commands run in-process through
`ai_desktop.execute(argv, quiet=True)`, so validation, error messages, exit codes and the
action log are exactly those of the CLI. Nothing in this module may write to stdout:
that is the MCP transport.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

try:  # an expected tool failure whose message is passed to the client as-is
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - older SDK layout
    ToolError = Exception  # type: ignore[assignment,misc]

import ai_desktop


# Desktop actions must not interleave, and the CLI's module globals aren't thread-safe.
_LOCK = threading.Lock()

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

INSTRUCTIONS = """\
ai-desktop controls this computer's real desktop and its signed-in accounts.
Work in a loop: screenshot -> read the labelled grid -> ONE action -> look again.
Grid labels are screen coordinates: pass them to click/move unchanged, including the
labels of a zoom="X,Y" close-up, which is the way to hit small targets. On a
multi-monitor desktop use monitor="N" for a closer look at one screen.
Pass seen="latest" to an action to have it abort if the screen changed since your last
screenshot (a dialog, another window in front), and shot=true to get the next
screenshot back from the same call. Use wait(stable=true) after opening a page or app,
and run(...) for a short fixed sequence of steps.
Actions are paced like a human, and the server runs one at a time: prefer paste over type
for more than a sentence or two.
Never type passwords, 2FA codes or payment details; ask the human first. Ask before
anything public or irreversible (posting, sending, buying, deleting, accepting terms).
An error mentioning the failsafe means the human aborted: stop and ask.
"""


class CommandError(ToolError):
    """A failed command; the SDK reports the message to the client as a tool error."""


def run_raw(argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Run one CLI command in-process; returns (exit code, payload) without raising."""
    with _LOCK, contextlib.redirect_stdout(sys.stderr):
        ai_desktop.LOG_VIA = "mcp"  # under the lock: a running `run` has set it to mcp/run
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
    # One token, so a value that starts with "-" (text, or a zoom point left of the
    # primary monitor such as -1500,300) isn't mistaken for an option.
    return [] if value is None else [f"{flag}={value}"]


def _instant(flag: bool) -> list[str]:
    return ["--instant"] if flag else []


def _acting(seen: str | None, shot: bool) -> list[str]:
    return _opt("--seen", seen) + (["--shot"] if shot else [])


def _image(path: str) -> Image:
    return Image(data=Path(path).read_bytes(), format="png")


def _with_shot(payload: dict[str, Any]) -> list:
    """The payload as JSON, plus the --shot screenshot inline when one was taken."""
    shot = payload.get("shot")
    return [dumps(payload)] + ([_image(shot)] if shot and shot != "error" else [])


def build_server(default_max_width: int | None = None) -> MCPServer:
    try:
        mcp = MCPServer("ai-desktop", instructions=INSTRUCTIONS)
    except TypeError:  # older/newer constructor without `instructions`
        mcp = MCPServer("ai-desktop")

    def tool(annotations: ToolAnnotations, **kwargs):
        return mcp.tool(annotations=annotations, structured_output=False, **kwargs)

    @tool(READ_ONLY)
    def screenshot(
        grid: bool = True,
        spacing: int | None = None,
        monitor: str = "all",
        max_width: int | None = None,
        zoom: str | None = None,
        cursor: str = "auto",
    ) -> list:
        """Capture the screen (all monitors by default) with a labelled coordinate grid.

        Returns the image inline plus JSON with the file path, its size and the global
        origin (x, y). Grid labels ARE screen coordinates: pass them to click unchanged.
        monitor="1" (an index from the monitors tool) or "primary" shows one screen.
        zoom="X,Y" shows the area around that point enlarged with a fine grid, for small
        targets; its labels are still screen coordinates. Images wider than max_width
        (default 1920) are shrunk with their labels kept legible; image_scale says by how
        much. cursor="marker" always draws a crosshair at the pointer, "none" never.
        """
        limit = default_max_width if max_width is None else max_width
        argv = ["screenshot", "--monitor", str(monitor)] + _opt("--max-width", limit)
        argv += (["--grid"] if grid else []) + _opt("--spacing", spacing) + _opt("--zoom", zoom)
        argv += [] if cursor == "auto" else ["--cursor", cursor]
        payload = run(argv)
        return [dumps(payload), _image(payload["path"])]

    @tool(ACTION)
    def click(
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        instant: bool = False,
        seen: str | None = None,
        shot: bool = False,
    ) -> list:
        """Click at a screen coordinate (button: left, right or middle).

        The pointer travels there along a human-like path (up to 1.5 s); instant=True jumps
        straight there. seen="latest" (or a screenshot path) aborts without clicking if
        the screen around (x, y) changed since that screenshot; shot=true returns a new
        screenshot taken right after the click."""
        argv = ["click", "--x", str(x), "--y", str(y), "--button", button]
        argv += (["--double"] if double else []) + _instant(instant) + _acting(seen, shot)
        return _with_shot(run(argv))

    @tool(ACTION)
    def move(x: int, y: int, instant: bool = False, seen: str | None = None, shot: bool = False) -> list:
        """Move the mouse to a screen coordinate without clicking.

        The pointer travels there along a human-like path (up to 1.5 s); instant=True jumps
        straight there. seen and shot work as for click."""
        argv = ["move", "--x", str(x), "--y", str(y)] + _instant(instant) + _acting(seen, shot)
        return _with_shot(run(argv))

    @tool(ACTION, name="type")
    def type_text(
        text: str, interval: float = 0.02, instant: bool = False, seen: str | None = None, shot: bool = False
    ) -> list:
        """Type printable ASCII text into the focused window. For other characters use paste.

        Typed one key at a time with human-like pauses (about 0.12 s per character and
        0.25-0.5 s per word, so ~8 s for a 50-character sentence); use paste for longer
        text, or instant=True to type at a fixed `interval`. seen="latest" aborts if the
        screen changed much since that screenshot; shot=true returns a new screenshot."""
        argv = ["type", f"--text={text}", "--interval", str(interval)]
        return _with_shot(run(argv + _instant(instant) + _acting(seen, shot)))

    @tool(ACTION)
    def key(key: str, seen: str | None = None, shot: bool = False) -> list:
        """Press one key: enter, tab, esc, space, backspace, delete, arrows, home, end,
        pageup, pagedown, f1-f12, or a single character. seen and shot work as for type."""
        return _with_shot(run(["key", "--key", key] + _acting(seen, shot)))

    @tool(ACTION)
    def hotkey(keys: str, seen: str | None = None, shot: bool = False) -> list:
        """Press a combination joined with '+', e.g. ctrl+c, cmd+t, alt+tab, ctrl+shift+t.
        seen and shot work as for type."""
        return _with_shot(run(["hotkey", "--keys", keys] + _acting(seen, shot)))

    @tool(ACTION)
    def scroll(
        direction: str = "down",
        amount: int = 3,
        x: int | None = None,
        y: int | None = None,
        instant: bool = False,
        seen: str | None = None,
        shot: bool = False,
    ) -> list:
        """Scroll the mouse wheel `amount` notches up or down, optionally after moving to (x, y).

        The move travels along a human-like path; instant=True jumps straight there. seen
        and shot work as for click."""
        argv = ["scroll", "--direction", direction, "--amount", str(amount)]
        argv += _opt("--x", x) + _opt("--y", y) + _instant(instant) + _acting(seen, shot)
        return _with_shot(run(argv))

    @tool(READ_ONLY)
    def wait(
        stable: bool = True, changed: str | None = None, timeout: float = 10.0, shot: bool = False
    ) -> list:
        """Wait until the screen stops changing (default), or until it differs from the
        screenshot `changed` ("latest" or a path). Fails after `timeout` seconds (at most
        30 here). shot=true returns the final screen as a gridded screenshot."""
        argv = ["wait"] + (_opt("--changed", changed) if changed else ["--stable"])
        argv += ["--timeout", str(min(max(timeout, 0.0), 30.0))] + (["--shot"] if shot else [])
        return _with_shot(run(argv))

    @tool(ACTION, name="run")
    def run_steps(steps: list[list[str]], shot: bool = False) -> list:
        """Run several commands in one call, stopping at the first failure. Each step is a
        CLI argument list, e.g. [["click","--x","500","--y","300"],["type","--text","hi"],
        ["key","--key","enter"]]. Faster than separate calls; shot=true returns a
        screenshot after the last step. Only for short sequences you are sure of: look at
        the screen before trusting the result."""
        return _with_shot(run(["run", "--steps", json.dumps(steps)] + (["--shot"] if shot else [])))

    @tool(ACTION)
    def copy() -> str:
        """Press the copy shortcut and return the copied text (error if nothing was selected)."""
        return dumps(run(["copy"]))

    @tool(ACTION)
    def paste(text: str | None = None, seen: str | None = None, shot: bool = False) -> list:
        """Press the paste shortcut; with `text`, put it on the clipboard first (any
        characters). seen and shot work as for type."""
        return _with_shot(run(["paste"] + _opt("--text", text) + _acting(seen, shot)))

    @tool(READ_ONLY)
    def clipboard_get() -> str:
        """Read the clipboard text."""
        return dumps(run(["clipboard", "get"]))

    @tool(ACTION)
    def clipboard_set(text: str) -> str:
        """Put text on the clipboard without pressing any keys."""
        return dumps(run(["clipboard", "set"] + _opt("--text", text)))

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


def serve(max_width: int | None = None) -> None:
    """Serve over stdio. `max_width` applies to every screenshot, including the ones
    actions return with shot=true; without it AI_DESKTOP_MAX_WIDTH or 1920 does."""
    saved = ai_desktop.MAX_WIDTH_OVERRIDE
    if max_width is not None:
        ai_desktop.MAX_WIDTH_OVERRIDE = max_width
    # The uinput devices stay open for the server's lifetime: only the first action of
    # each kind waits for the compositor to notice a new device.
    try:
        with ai_desktop.keep_devices():
            build_server(max_width).run()  # stdio
    finally:
        ai_desktop.MAX_WIDTH_OVERRIDE = saved
