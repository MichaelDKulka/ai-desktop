"""drag and --monitor relative coordinates."""
import pytest

import ai_desktop as ad

LEFT = ad.Monitor(1, "left", -1920, 0, 1920, 1080, False, 1.0)
MAIN = ad.Monitor(0, "main", 0, 0, 1920, 1080, True, 1.0)


@pytest.fixture
def two_monitors(monkeypatch):
    monkeypatch.setattr(ad, "detect_monitors", lambda backend: ([MAIN, LEFT], "fake"))


def test_drag_presses_moves_and_releases(run, fake_gui):
    code, out, _ = run("drag", "--x1", "100", "--y1", "200", "--x2", "500", "--y2", "600", "--duration", "0.2")
    assert code == 0
    assert out == "OK drag x1=100 y1=200 x2=500 y2=600 button=left duration=0.2\n"
    assert fake_gui.calls == [
        ("moveTo", (100, 200), {}),
        ("mouseDown", (), {"button": "left"}),
        ("moveTo", (500, 600), {"duration": 0.2}),
        ("mouseUp", (), {"button": "left"}),
    ]


def test_drag_releases_even_if_the_move_fails(fake_gui):
    def boom(*args, **kwargs):
        if "duration" in kwargs:
            raise fake_gui.FailSafeException("corner")

    fake_gui.moveTo = boom
    backend = ad.get_backend("pyautogui")
    with pytest.raises(fake_gui.FailSafeException):
        backend.drag(1, 1, 50, 50, "right", 0.1)
    assert fake_gui.calls[-1] == ("mouseUp", (), {"button": "right"})


def test_drag_checks_both_points_before_acting(run, fake_gui):
    code, _, err = run("drag", "--x1", "10", "--y1", "10", "--x2", "5000", "--y2", "10")
    assert code == 1 and "no monitor" in err
    assert fake_gui.calls == []


def test_drag_default_duration_and_button(run, fake_gui):
    code, _, _ = run("drag", "--x1", "10", "--y1", "10", "--x2", "20", "--y2", "20", "--button", "middle")
    assert code == 0
    assert fake_gui.calls[2] == ("moveTo", (20, 20), {"duration": 0.5})
    assert fake_gui.calls[1] == ("mouseDown", (), {"button": "middle"})


def test_drag_rejects_negative_duration(run, fake_gui):
    with pytest.raises(SystemExit):
        run("drag", "--x1", "1", "--y1", "1", "--x2", "2", "--y2", "2", "--duration", "-1")


def test_to_global_without_monitor_is_identity(fake_gui):
    assert ad.to_global(ad.get_backend("pyautogui"), None, -5, 7) == (-5, 7)


def test_click_relative_to_monitor(run, fake_gui, two_monitors):
    code, out, _ = run("click", "--x", "100", "--y", "50", "--monitor", "1")
    assert code == 0 and out == "OK click x=-1820 y=50 button=left clicks=1 monitor=1\n"
    assert fake_gui.calls[-1][2]["x"] == -1820


def test_move_relative_to_primary(run, fake_gui, two_monitors):
    code, out, _ = run("move", "--x", "300", "--y", "400", "--monitor", "primary")
    assert code == 0 and fake_gui.calls[-1] == ("moveTo", (300, 400), {})
    assert "monitor=primary" in out


def test_drag_relative_to_monitor(run, fake_gui, two_monitors):
    code, _, _ = run("drag", "--x1", "10", "--y1", "20", "--x2", "1000", "--y2", "500", "--monitor", "1")
    assert code == 0
    assert fake_gui.calls[0] == ("moveTo", (-1910, 20), {})
    assert fake_gui.calls[2] == ("moveTo", (-920, 500), {"duration": 0.5})


def test_scroll_relative_to_monitor(run, fake_gui, two_monitors):
    code, _, _ = run("scroll", "--x", "5", "--y", "5", "--monitor", "1")
    assert code == 0 and fake_gui.calls[-1][2] == {"x": -1915, "y": 5}


def test_scroll_monitor_needs_coordinates(run, fake_gui, two_monitors):
    code, _, err = run("scroll", "--monitor", "1")
    assert code == 1 and "needs --x and --y" in err


def test_relative_point_outside_monitor_is_rejected(run, fake_gui, two_monitors):
    code, _, err = run("click", "--x", "1920", "--y", "10", "--monitor", "1")
    assert code == 1 and "outside monitor 1" in err
    assert not any(c[0] == "click" for c in fake_gui.calls)


def test_unknown_monitor_and_all_are_rejected(run, fake_gui, two_monitors):
    code, _, err = run("click", "--x", "1", "--y", "1", "--monitor", "7")
    assert code == 1 and "no monitor 7" in err
    code, _, err = run("click", "--x", "1", "--y", "1", "--monitor", "all")
    assert code == 1 and "no origin" in err


def test_global_default_unchanged(run, fake_gui, two_monitors):
    code, out, _ = run("click", "--x", "-1000", "--y", "10")
    assert code == 0 and out == "OK click x=-1000 y=10 button=left clicks=1\n"


def test_mcp_drag_and_monitor_argv(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    import ai_desktop_mcp as server

    calls = []
    monkeypatch.setattr(server, "run", lambda argv: calls.append(argv) or {"ok": True})
    mcp = server.build_server()

    async def work():
        await mcp.call_tool("drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "monitor": "1"})
        await mcp.call_tool("click", {"x": 5, "y": 6, "monitor": "primary"})
        await mcp.call_tool("move", {"x": 7, "y": 8})
        await mcp.call_tool("scroll", {"x": 9, "y": 10, "monitor": "0"})

    asyncio.run(work())
    assert calls[0] == [
        "drag", "--x1", "1", "--y1", "2", "--x2", "3", "--y2", "4",
        "--button", "left", "--duration", "0.5", "--monitor", "1",
    ]
    assert calls[1][calls[1].index("--monitor") + 1] == "primary"
    assert calls[2] == ["move", "--x", "7", "--y", "8"]
    assert calls[3][-2:] == ["--monitor", "0"]