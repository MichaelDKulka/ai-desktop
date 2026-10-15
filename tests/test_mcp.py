import asyncio
import base64
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


def test_action_tools_pass_instant(monkeypatch):
    calls = []
    monkeypatch.setattr(ad, "execute", lambda argv, quiet=False: (calls.append(argv), (0, {"ok": True}))[1])
    mcp = server.build_server()

    def call(name, **arguments):
        asyncio.run(mcp.call_tool(name, arguments))
        return calls[-1]

    assert "--instant" not in call("click", x=5, y=6)
    assert call("click", x=5, y=6, instant=True)[-1] == "--instant"
    assert call("move", x=5, y=6, instant=True) == ["move", "--x", "5", "--y", "6", "--instant"]
    assert call("scroll", x=5, y=6, instant=True)[-1] == "--instant"
    assert call("type", text="hi") == ["type", "--text=hi", "--interval", "0.02"]
    assert call("key", key="enter") == ["key", "--key", "enter"]


def test_action_tools_pass_seen_and_shot(monkeypatch):
    calls = []
    monkeypatch.setattr(ad, "execute", lambda argv, quiet=False: (calls.append(argv), (0, {"ok": True}))[1])
    mcp = server.build_server()

    def call(name, **arguments):
        asyncio.run(mcp.call_tool(name, arguments))
        return calls[-1]

    assert call("click", x=5, y=6, seen="/p", shot=True)[-2:] == ["--seen=/p", "--shot"]
    assert call("type", text="--force", seen="latest")[1:2] + call("type", text="hi", seen="latest")[-1:] == ["--text=--force", "--seen=latest"]
    assert call("hotkey", keys="ctrl+l", shot=True) == ["hotkey", "--keys", "ctrl+l", "--shot"]
    assert call("paste", text="x", shot=True)[-1] == "--shot"
    assert call("wait") == ["wait", "--stable", "--timeout", "10.0"]
    assert call("wait", changed="latest", timeout=99, shot=True) == [
        "wait", "--changed=latest", "--timeout", "30.0", "--shot",
    ]
    steps = [["click", "--x", "1", "--y", "2"], ["key", "--key", "enter"]]
    assert call("run", steps=steps) == ["run", "--steps", json.dumps(steps)]


def test_screenshot_and_shot_images_come_from_the_saved_file(monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    Image.new("RGB", (8, 4), (255, 0, 0)).save(shot)
    calls = []

    def fake_execute(argv, quiet=False):
        calls.append(argv)
        return 0, {"ok": True, "command": argv[0], "path": str(shot), "x": 0, "y": 0, "shot": str(shot)}

    monkeypatch.setattr(ad, "execute", fake_execute)
    mcp = server.build_server(default_max_width=1280)
    content = asyncio.run(mcp.call_tool("screenshot", {"zoom": "5,6", "cursor": "marker"}))
    blocks = content.content
    assert calls[-1] == [
        "screenshot", "--monitor", "all", "--max-width=1280", "--grid", "--zoom=5,6", "--cursor", "marker",
    ]
    assert [block.type for block in blocks] == ["text", "image"]
    assert blocks[1].data == base64.b64encode(shot.read_bytes()).decode()
    content = asyncio.run(mcp.call_tool("click", {"x": 1, "y": 2, "shot": True}))
    blocks = content.content
    assert [block.type for block in blocks] == ["text", "image"]


def test_serve_keeps_devices_open(monkeypatch):
    seen = {}

    class FakeServer:
        def run(self):
            seen["cache"] = ad._DEVICE_CACHE

    monkeypatch.setattr(server, "build_server", lambda max_width: FakeServer())
    server.serve(1280)
    assert seen["cache"] == {} and ad._DEVICE_CACHE is None


def test_server_max_width_applies_to_every_screenshot(monkeypatch):
    seen = {}

    class FakeServer:
        def run(self):
            seen["limit"] = ad.max_width_setting()

    monkeypatch.setattr(server, "build_server", lambda max_width: FakeServer())
    monkeypatch.setenv("AI_DESKTOP_MAX_WIDTH", "1600")
    server.serve(1280)  # shot=true screenshots go through max_width_setting() too
    assert seen["limit"] == 1280 and ad.max_width_setting() == 1600
    server.serve(None)
    assert seen["limit"] == 1600


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
        "monitors", "doctor", "screenshots_latest", "log", "wait", "run",
    } <= names
    screenshot = next(t for t in tools if t.name == "screenshot")
    assert screenshot.annotations.read_only_hint is True
    assert "max_width" in screenshot.input_schema["properties"]
    assert latest.is_error and "no screenshots" in latest.content[0].text
    # The failed screenshots_latest call is in the action log, marked as an MCP call.
    entries = json.loads(log.content[0].text)["entries"]
    assert not log.is_error and [e["command"] for e in entries] == ["screenshots"]
    assert entries[0]["via"] == "mcp" and entries[0]["ok"] is False


def test_negative_zoom_points_and_dash_text_reach_the_cli_intact(monkeypatch):
    seen = []
    real = ad.execute

    def check(argv, quiet=False):
        args, payload = ad.parse_quiet(argv)
        seen.append((args, payload))
        return 0, {"ok": True, "command": argv[0], "path": __file__, "x": 0, "y": 0}

    monkeypatch.setattr(ad, "execute", check)
    mcp = server.build_server()
    asyncio.run(mcp.call_tool("screenshot", {"zoom": "-1500,300"}))
    assert seen[-1][0].zoom == (-1500, 300)
    asyncio.run(mcp.call_tool("type", {"text": "--force"}))
    assert seen[-1][0].text == "--force"
    asyncio.run(mcp.call_tool("paste", {"text": "-x y"}))
    assert seen[-1][0].text == "-x y"
    monkeypatch.setattr(ad, "execute", real)
