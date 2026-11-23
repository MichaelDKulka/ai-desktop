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
    assert call("wait") == ["wait", "--stable", "--quiet", "1.0", "--timeout", "10.0"]
    assert call("wait", changed="latest", settle=True, quiet=2) == [
        "wait", "--changed=latest", "--settle", "--quiet", "2.0", "--timeout", "10.0",
    ]
    assert call("wait", changed="latest", timeout=99, shot=True) == [
        "wait", "--changed=latest", "--quiet", "1.0", "--timeout", "30.0", "--shot",
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


@pytest.mark.parametrize("text", ["null", "123", "true", '{"a": 1}', "[1, 2]", "hello"])
def test_text_that_looks_like_json_is_passed_as_text(monkeypatch, text):
    # The SDK JSON-decodes arguments not typed as plain str: "null" became None, so
    # paste pasted the old clipboard, and '[1, 2]' was rejected.
    import asyncio

    import ai_desktop_mcp as server

    calls = []
    monkeypatch.setattr(server, "run", lambda argv: calls.append(argv) or {"ok": True})
    mcp = server.build_server()

    async def work():
        await mcp.call_tool("paste", {"text": text})
        await mcp.call_tool("paste", {})
        await mcp.call_tool("focus", {"title": text})
        await mcp.call_tool("launch_chrome", {"url": text})
        await mcp.call_tool("key", {"key": "enter", "seen": text})

    asyncio.run(work())
    assert calls[0] == ["paste", f"--text={text}"]
    assert calls[1] == ["paste"]
    assert calls[2] == ["focus", f"--title={text}"]
    assert calls[3] == ["launch-chrome", f"--url={text}"]
    assert calls[4] == ["key", "--key", "enter", f"--seen={text}"]


def test_failures_carry_the_whole_payload(monkeypatch):
    import ai_desktop_mcp as server

    payload = {"ok": False, "command": "run", "error": "step 2 (click) failed: screen changed", "code": 1,
               "done": 1, "results": [{"step": 1, "command": "copy", "ok": True, "result": {"chars": 5}}]}
    monkeypatch.setattr(server, "run_raw", lambda argv: (1, payload))
    with pytest.raises(server.CommandError) as info:
        server.run(["run", "--steps", "[]"])
    data = json.loads(str(info.value))
    assert data["error"].startswith("step 2") and data["done"] == 1 and data["results"][0]["command"] == "copy"


def test_wait_without_stable_or_changed_is_an_error(monkeypatch):
    import ai_desktop_mcp as server

    monkeypatch.setattr(server, "run", lambda argv: pytest.fail("ran"))
    mcp = server.build_server()
    with pytest.raises(Exception, match="give stable=true, or changed"):
        asyncio.run(mcp.call_tool("wait", {"stable": False}))


def test_a_cancelled_call_stops_at_its_next_pause(monkeypatch):
    """The client cancels a long type: the engine's next pause sees the token and stops."""
    import threading

    import anyio

    import ai_desktop_mcp as server

    started, typed = threading.Event(), []

    def slow_type(argv):
        token = server._CALL.token
        ad.CANCEL = token
        try:
            for ch in "hello world":
                ad.check_stop()
                typed.append(ch)
                started.set()
                token.wait(0.02)
        except ad.CliError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        finally:
            ad.CANCEL = None
        return {"ok": True}

    monkeypatch.setattr(server, "run", slow_type)

    async def work():
        with anyio.move_on_after(0.1):
            await server._run(["type", "--text", "hello world"])
        await anyio.to_thread.run_sync(lambda: started.wait(1))

    asyncio.run(work())
    deadline = __import__("time").monotonic() + 2
    while len(typed) < 11 and __import__("time").monotonic() < deadline:
        n = len(typed)
        __import__("time").sleep(0.1)
        if len(typed) == n:
            break
    assert 0 < len(typed) < 11  # it stopped part-way instead of finishing


def test_a_call_cancelled_while_queued_never_runs(monkeypatch):
    import anyio

    import ai_desktop_mcp as server

    ran = []
    monkeypatch.setattr(ad, "execute", lambda argv, quiet=False: ran.append(argv) or (0, {"ok": True}))
    server._LOCK.acquire()  # another call is busy

    async def work():
        with anyio.move_on_after(0.1):
            await server._run(["click", "--x", "1", "--y", "1"])

    try:
        asyncio.run(work())
    finally:
        server._LOCK.release()
    __import__("time").sleep(0.2)  # the abandoned worker gets the lock now
    assert ran == []


def test_cancel_check_raises_once_set():
    import threading

    token = threading.Event()
    ad.CANCEL = token
    try:
        ad.check_stop()
        token.set()
        with pytest.raises(ad.CliError) as info:
            ad.check_stop()
        assert info.value.code == 130 and "cancelled" in str(info.value)
    finally:
        ad.CANCEL = None


def test_big_inline_images_are_sent_as_jpeg_under_the_budget(tmp_path):
    import random

    import ai_desktop_mcp as server

    rng = random.Random(1)
    noisy = Image.frombytes("RGB", (1600, 900), bytes(rng.getrandbits(8) for _ in range(1600 * 900 * 3)))
    path = tmp_path / "noisy.png"
    noisy.save(path)
    assert path.stat().st_size > server.INLINE_MAX_BYTES
    image, info = server._inline(str(path))
    assert info["inline_format"] == "jpeg" and len(image.data) <= server.INLINE_MAX_BYTES
    small = tmp_path / "small.png"
    Image.new("RGB", (100, 100), (9, 9, 9)).save(small)
    image, info = server._inline(str(small))
    assert info == {} and image.data == small.read_bytes()


def test_screenshot_if_changed_returns_no_image_when_unchanged(monkeypatch, tmp_path):
    import ai_desktop_mcp as server

    calls = []
    monkeypatch.setattr(server, "run", lambda argv: calls.append(argv) or {"ok": True, "command": "screenshot", "unchanged": True, "path": "/x.png"})
    mcp = server.build_server()
    result = asyncio.run(mcp.call_tool("screenshot", {"if_changed": True}))
    assert "--if-changed" in calls[0]
    assert len(result.content) == 1 and json.loads(result.content[0].text)["unchanged"] is True


def test_observe_tool_and_observe_on_actions(monkeypatch):
    import ai_desktop_mcp as server

    calls = []
    monkeypatch.setattr(server, "run", lambda argv: calls.append(argv) or {"ok": True, "changed": False})
    mcp = server.build_server()

    async def work():
        await mcp.call_tool("observe", {"timeout": 99, "since": 4, "shot": False})
        await mcp.call_tool("click", {"x": 1, "y": 2, "observe": True})
        await mcp.call_tool("key", {"key": "enter", "observe": True})

    asyncio.run(work())
    assert calls[0] == ["observe", "--timeout", "30.0", "--quiet", "0.5", "--since", "4", "--no-shot"]
    assert calls[1][-1] == "--observe" and calls[2][-1] == "--observe"


def test_mcp_uses_the_background_service_when_it_can(monkeypatch):
    import ai_desktop_mcp as server
    import ai_desktop_service as svc

    monkeypatch.setattr(svc, "enabled", lambda: True)
    sent = []
    monkeypatch.setattr(svc, "call", lambda argv, cancel=None, via="cli": sent.append((argv, via)) or {
        "code": 0, "stdout": json.dumps({"ok": True, "command": "click", "x": 1}) + "\n", "stderr": ""})
    code, payload = server.run_raw(["click", "--x", "1", "--y", "2"])
    assert code == 0 and payload["x"] == 1 and sent == [(["--json", "click", "--x", "1", "--y", "2"], "mcp")]
    monkeypatch.setattr(svc, "call", lambda argv, cancel=None, via="cli": None)  # no service: run here
    monkeypatch.setattr(ad, "execute", lambda argv, quiet=False: (0, {"ok": True, "command": "click", "here": True}))
    assert server.run_raw(["click", "--x", "1", "--y", "2"])[1]["here"] is True


def test_find_by_name_tools(monkeypatch):
    import ai_desktop_mcp as server

    calls = []
    monkeypatch.setattr(server, "run", lambda argv: calls.append(argv) or {"ok": True})
    mcp = server.build_server()

    async def work():
        await mcp.call_tool("click", {"target": "Send", "observe": True})
        await mcp.call_tool("click", {"ref": "e12"})
        await mcp.call_tool("click", {"x": 5, "y": 6})
        await mcp.call_tool("ui", {"find": "save", "role": "push button"})
        await mcp.call_tool("text", {"find": "Inbox"})
        await mcp.call_tool("set_text", {"value": "-hello", "ref": "e3"})
        await mcp.call_tool("locate", {"what": "the gear icon"})
        await mcp.call_tool("launch_chrome", {"a11y": True})

    asyncio.run(work())
    assert calls[0][:2] == ["click", "--target=Send"] and calls[0][-1] == "--observe"
    assert "--ref=e12" in calls[1] and calls[2][:3] == ["click", "--x=5", "--y=6"]
    assert calls[3] == ["ui", "--find=save", "--role=push button"]
    assert calls[5][:2] == ["set-text", "--value=-hello"]
    assert calls[6] == ["locate", "the gear icon"] and calls[7][-1] == "--a11y"
