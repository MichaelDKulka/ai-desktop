import asyncio
import io
import json
import os
import sys

import pytest
from PIL import Image

pytest.importorskip("mcp")

import ai_desktop as ad  # noqa: E402
import ai_desktop_mcp as server  # noqa: E402


def test_run_passes_argv_quietly_and_raises_on_error(monkeypatch):
    calls = []

    def fake_execute(argv, quiet=False):
        calls.append((argv, quiet))
        if argv[:3] == ["click", "--x", "0"]:
            return 1, {"ok": False, "command": "click", "error": "failsafe corner", "code": 1}
        return 0, {"ok": True, "command": argv[0]}

    monkeypatch.setattr(ad, "execute", fake_execute)
    assert server.run(["position"]) == {"ok": True, "command": "position"}
    assert calls[-1] == (["position"], True)
    with pytest.raises(server.CommandError, match="failsafe corner"):
        server.run(["click", "--x", "0", "--y", "0"])
    assert ad.LOG_VIA == "mcp"


def test_inline_image_grids_the_file_and_downscales_the_copy(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (4000, 1000), (255, 255, 255)).save(path)
    png, extra = server.inline_image(str(path), True, 100, (-2000, 0), 1920)
    inline = Image.open(io.BytesIO(png))
    assert inline.size == (1920, 480) and extra == {"inline_width": 1920, "inline_height": 480, "inline_scale": 0.48}
    with Image.open(path) as saved:
        assert saved.size == (4000, 1000)
        r, g, b = saved.getpixel((2000, 500))  # global x 0 is a grid line, at local 2000
        assert r > g and b > g
    png, extra = server.inline_image(str(path), False, 100, (0, 0), 0)
    assert Image.open(io.BytesIO(png)).size == (4000, 1000) and extra["inline_scale"] == 1.0


def test_stdio_server_lists_tools_and_runs_commands():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ai_desktop", "mcp"], env=dict(os.environ)
    )

    async def session_work():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                latest = await session.call_tool("screenshots_latest", {})
                log = await session.call_tool("log", {"limit": 5})
                return tools, latest, log

    tools, latest, log = asyncio.run(session_work())
    names = {t.name for t in tools}
    assert {
        "screenshot", "click", "move", "type", "key", "hotkey", "scroll", "copy", "paste",
        "clipboard_get", "clipboard_set", "position", "launch_chrome", "windows", "focus",
        "monitors", "doctor", "screenshots_latest", "log",
    } <= names
    screenshot = next(t for t in tools if t.name == "screenshot")
    assert screenshot.annotations.read_only_hint is True
    assert "max_width" in screenshot.input_schema["properties"]
    assert latest.is_error and "no screenshots" in latest.content[0].text
    # The failed screenshots_latest call is in the action log, marked as an MCP call.
    entries = json.loads(log.content[0].text)["entries"]
    assert not log.is_error and [e["command"] for e in entries] == ["screenshots"]
    assert entries[0]["via"] == "mcp" and entries[0]["ok"] is False
