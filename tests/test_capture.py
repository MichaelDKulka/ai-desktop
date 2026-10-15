import sys
import types

import pytest
from PIL import Image

import ai_desktop as ad


class FakeBackend(ad.Backend):
    name = "fake"

    def size(self):
        return (1920, 1080)


class FakeGui:
    @staticmethod
    def screenshot():
        return Image.new("RGBA", (3840, 2160), (9, 9, 9, 255))


def test_capture_normalizes_hidpi_to_logical_size(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad, "grab_linux", lambda cursor=False: FakeGui.screenshot().convert("RGB"))
    cap = ad.capture_screen(FakeBackend())
    assert cap.image.size == (1920, 1080) and cap.image.mode == "RGB"
    assert cap.scale == 2.0 and (cap.x, cap.y) == (0, 0) and cap.monitor == "all"


def test_find_screenshot_tool(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda n: "/usr/bin/grim" if n == "grim" else None)
    assert ad.find_screenshot_tool() == ["grim"]
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    assert ad.find_screenshot_tool() is None


def test_grab_linux_on_x11_falls_back_to_pillow(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    monkeypatch.setenv("DISPLAY", ":9")
    seen = {}

    def grab(xdisplay=None):
        seen["xdisplay"] = xdisplay
        return Image.new("RGBA", (3840, 2160))

    monkeypatch.setattr(ad.ImageGrab, "grab", grab)
    img = ad.grab_linux()
    assert img.mode == "RGB" and img.size == (3840, 2160)
    assert seen["xdisplay"] == ":9"


def test_grab_linux_on_x11_reports_capture_errors(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)

    def grab(xdisplay=None):
        raise OSError("Pillow was built without XCB support")

    monkeypatch.setattr(ad.ImageGrab, "grab", grab)
    with pytest.raises(ad.CliError, match="cannot capture the X11 screen"):
        ad.grab_linux()


@pytest.fixture
def fake_tool(monkeypatch):
    """Pretend one screenshot tool is installed; returns the argv lists it was run with."""
    runs = []

    def install(name):
        monkeypatch.setattr(ad.shutil, "which", lambda n: f"/usr/bin/{n}" if n == name else None)

    def fake_run(argv, **kwargs):
        runs.append(argv)
        Image.new("RGB", (8, 6), (1, 2, 3)).save(argv[-1])
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    install.runs = runs
    return install


@pytest.mark.parametrize(
    "tool,with_cursor",
    [
        ("gnome-screenshot", ["gnome-screenshot", "-p", "-f"]),
        ("grim", ["grim", "-c"]),
        ("spectacle", ["spectacle", "-p", "-b", "-n", "-o"]),
    ],
)
def test_grab_linux_asks_the_tool_for_the_pointer(fake_tool, tool, with_cursor):
    fake_tool(tool)
    ad.grab_linux(cursor=True)
    assert fake_tool.runs[-1][:-1] == with_cursor
    ad.grab_linux()
    assert fake_tool.runs[-1][:-1] == [a for a in with_cursor if a not in ("-p", "-c")]


def test_linux_capture_reports_a_native_cursor_only_when_drawn(fake_tool, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    fake_tool("gnome-screenshot")
    backend = FakeBackend()
    assert ad.capture_screen(backend, cursor=True).cursor == "native"
    assert ad.capture_screen(backend).cursor == "none"
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)  # Pillow's X11 fallback can't draw it
    monkeypatch.setattr(ad.ImageGrab, "grab", lambda xdisplay=None: Image.new("RGB", (1920, 1080)))
    assert ad.capture_screen(backend, cursor=True).cursor == "none"


def test_macos_screencapture_draws_the_pointer_with_C(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        Image.new("RGB", (4, 4)).save(argv[-1])
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(ad.subprocess, "run", fake_run)
    ad._screencapture_rect(0, 0, 4, 4, cursor=True)
    assert "-C" in seen[-1] and seen[-1][-2] == "-R0,0,4,4"
    ad._screencapture_rect(0, 0, 4, 4)
    assert "-C" not in seen[-1]


def test_known_pointer(monkeypatch):
    monitors = [ad.Monitor(0, "a", 0, 0, 1920, 1080, True, 1.0)]

    class Live(FakeBackend):
        def position(self):
            return (5, 6)

    assert ad.known_pointer(Live(), monitors) == ((5, 6), "live")
    assert ad.known_pointer(FakeBackend(), monitors) == (None, "unknown")
    ad.remember_pointer(100, 200)
    assert ad.known_pointer(FakeBackend(), monitors) == ((100, 200), "remembered")
    ad.remember_pointer(-50, 200)  # no longer on any monitor
    assert ad.known_pointer(FakeBackend(), monitors) == (None, "unknown")


def test_pointer_marker_leaves_the_hotspot_clear():
    img = Image.new("RGB", (200, 200), (120, 120, 120))
    out = ad.draw_pointer_marker(img, (100, 100), "live")
    assert out.getpixel((100, 100)) == (120, 120, 120)  # the hotspot itself
    assert out.getpixel((110, 100)) == (255, 255, 255)  # a white arm
    assert out.getpixel((100, 90)) == (255, 255, 255)
    assert ad.draw_pointer_marker(img, (195, 195), "remembered").size == (200, 200)  # label flips inside


@pytest.fixture
def plain_capture(monkeypatch):
    calls = []

    def capture(backend, monitor="all", cursor=False):
        calls.append(cursor)
        return ad.Capture(Image.new("RGB", (640, 480), (120, 120, 120)), 0, 0, 1.0, "all")

    monkeypatch.setattr(ad, "capture_screen", capture)
    return calls


def test_screenshot_marks_the_live_pointer(run, fake_gui, plain_capture):
    code, out, _ = run("screenshot")
    assert code == 0 and plain_capture == [True]  # auto asks the tool first
    assert " cursor=marker pointer=live pointer_x=10 pointer_y=20" in out
    path = ad.store_files()[0][0]
    with Image.open(path) as img:
        assert img.getpixel((10, 30)) == (255, 255, 255)  # the arm below (10, 20)
    with Image.open(ad.sidecar_path(path)) as raw:
        assert raw.getpixel((10, 30)) == (120, 120, 120)  # the reference copy stays clean
    _, out, _ = run("screenshot", "--cursor", "none")
    assert plain_capture[-1] is False and " cursor=none " in out
    with Image.open(ad.store_files()[0][0]) as img:
        assert img.getpixel((10, 30)) == (120, 120, 120)


def test_screenshot_trusts_a_native_cursor(run, fake_gui, monkeypatch):
    monkeypatch.setattr(
        ad, "capture_screen",
        lambda backend, monitor="all", cursor=False: ad.Capture(
            Image.new("RGB", (640, 480), (120, 120, 120)), 0, 0, 1.0, "all", "native" if cursor else "none"
        ),
    )
    _, out, _ = run("screenshot")
    assert " cursor=native " in out
    with Image.open(ad.store_files()[0][0]) as img:
        assert img.getpixel((10, 30)) == (120, 120, 120)  # no marker over the real pointer


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uinput is Linux only")
def test_screenshot_marks_the_remembered_pointer_on_wayland(run, plain_capture, monkeypatch):
    monkeypatch.setenv("AI_DESKTOP_SCREEN_SIZE", "1920x1080")
    ad.remember_pointer(100, 100)
    code, out, _ = run("--backend", "uinput", "screenshot", "--cursor", "marker")
    assert code == 0 and plain_capture == [False]
    assert " cursor=marker pointer=remembered pointer_x=100 pointer_y=100" in out


def test_grab_linux_on_wayland_needs_a_tool(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    with pytest.raises(ad.CliError, match="screenshot tool"):
        ad.grab_linux()
