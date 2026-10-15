import json
import sys

import pytest
from PIL import Image

import ai_desktop as ad

ONE = " 0: +*eDP-1 1366/340x768/190+0+0  eDP-1\n"
TWO = (
    "Monitors: 2\n"
    " 0: +HDMI-1 1920/530x1080/300+1366+0  HDMI-1\n"
    " 1: *eDP-1 1366/340x768/190+0+0  eDP-1\n"
)
NEG = (
    " 0: +*DP-1 2560/600x1440/340+0+0  DP-1\n"
    " 1: HDMI-1 1920/530x1080/300+-1920+180  HDMI-1\n"
)


def mon(index, name, x, y, w, h, primary=False, scale=1.0):
    return ad.Monitor(index, name, x, y, w, h, primary, scale)


class SizedBackend(ad.Backend):
    name = "fake"

    def __init__(self, width, height):
        self._size = (width, height)

    def size(self):
        return self._size


def test_parse_xrandr_single_monitor():
    [m] = ad.parse_xrandr_monitors(ONE)
    assert m == mon(0, "eDP-1", 0, 0, 1366, 768, True)


def test_parse_xrandr_two_monitors_and_negative_offsets():
    found = ad.parse_xrandr_monitors(TWO)
    assert [(m.name, m.x, m.primary) for m in found] == [("HDMI-1", 1366, True), ("eDP-1", 0, False)]
    found = ad.parse_xrandr_monitors(NEG)
    assert found[1].x == -1920 and found[1].y == 180 and not found[1].primary


def test_order_monitors_primary_first_then_position():
    ordered = ad.order_monitors(ad.parse_xrandr_monitors(TWO))
    assert [m.index for m in ordered] == [0, 1]
    assert ordered[0].name == "HDMI-1" and ordered[0].primary and ordered[1].name == "eDP-1"
    ordered = ad.order_monitors([mon(0, "b", 100, 0, 10, 10), mon(0, "a", 0, 0, 10, 10)])
    assert ordered[0].name == "a" and ordered[0].primary  # nothing flagged: first by position


def test_linux_monitors_without_xrandr(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    assert ad._monitors_linux() == []


def test_detect_falls_back_to_one_screen(monkeypatch, real_detect_monitors):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad, "_monitors_linux", lambda: [])
    found, source = real_detect_monitors(SizedBackend(1280, 1024))
    assert source == "fallback" and found == [mon(0, "screen", 0, 0, 1280, 1024, True)]

    def boom():
        raise RuntimeError("no display")

    monkeypatch.setattr(ad, "_monitors_linux", boom)
    assert real_detect_monitors(SizedBackend(1280, 1024))[1] == "fallback"
    monkeypatch.setattr(ad, "_monitors_linux", lambda: ad.parse_xrandr_monitors(TWO))
    found, source = real_detect_monitors(SizedBackend(3286, 1080))
    assert source == "xrandr" and len(found) == 2


def test_union_rect_and_monitor_at():
    monitors = [mon(0, "a", 0, 0, 1920, 1080, True), mon(1, "b", -1920, 100, 1920, 1080)]
    assert ad.union_rect(monitors) == (-1920, 0, 1920, 1180)
    assert ad.monitor_at(monitors, -100, 500).name == "b"
    assert ad.monitor_at(monitors, 5, 5).name == "a"
    assert ad.monitor_at(monitors, -5, 50) is None  # gap above the left monitor


def test_check_point_accepts_any_monitor(monkeypatch, fake_gui):
    monitors = [mon(0, "a", 0, 0, 1920, 1080, True), mon(0, "b", -1920, 100, 1920, 1080)]
    monkeypatch.setattr(ad, "detect_monitors", lambda b: (monitors, "fake"))
    backend = ad.get_backend("pyautogui")
    backend.check_point(-100, 500)
    backend.check_point(-1920, 100)  # a corner of the secondary is not a failsafe corner
    with pytest.raises(ad.CliError, match="no monitor"):
        backend.check_point(-5, 50)
    with pytest.raises(ad.CliError, match="failsafe corner"):
        backend.check_point(0, 0)


def test_resolve_monitor():
    monitors = ad.order_monitors([mon(0, "a", 0, 0, 10, 10, True), mon(0, "b", 10, 0, 10, 10)])
    assert ad.resolve_monitor("all", monitors) is None
    assert ad.resolve_monitor("primary", monitors).name == "a"
    assert ad.resolve_monitor("1", monitors).name == "b"
    with pytest.raises(ad.CliError, match="no monitor 7"):
        ad.resolve_monitor("7", monitors)
    with pytest.raises(ad.CliError, match="--monitor must be"):
        ad.resolve_monitor("left", monitors)


def test_grid_with_negative_origin():
    out = ad.draw_grid(Image.new("RGB", (400, 300), (255, 255, 255)), 100, origin=(-1920, 0))
    for local_x in (20, 120, 220):  # global -1900, -1800, -1700
        r, g, b = out.getpixel((local_x, 150))
        assert r > g and b > g, local_x
    assert out.getpixel((70, 150)) == (255, 255, 255)
    assert out.size == (400, 300)


def test_grid_origin_shifts_lines():
    out = ad.draw_grid(Image.new("RGB", (300, 200), (255, 255, 255)), 100, origin=(50, 30))
    r, g, b = out.getpixel((50, 150))  # global x 100 at local 50
    assert r > g and b > g
    r, g, b = out.getpixel((150, 70))  # global y 100 at local 70
    assert r > g and b > g
    assert out.getpixel((100, 150)) == (255, 255, 255)


def test_linux_capture_crops_one_monitor(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monitors = [mon(0, "a", 0, 0, 1920, 1080, True), mon(0, "b", 1920, 0, 1920, 1080)]
    monkeypatch.setattr(ad, "detect_monitors", lambda b: (monitors, "fake"))
    root = Image.new("RGB", (3840, 1080), (1, 1, 1))
    root.paste(Image.new("RGB", (1920, 1080), (9, 9, 9)), (1920, 0))
    monkeypatch.setattr(ad, "grab_linux", lambda cursor=False: root)
    backend = SizedBackend(3840, 1080)
    cap = ad.capture_screen(backend, "1")
    assert cap.image.size == (1920, 1080) and (cap.x, cap.y) == (1920, 0) and cap.monitor == "1"
    assert cap.image.getpixel((5, 5)) == (9, 9, 9)
    cap = ad.capture_screen(backend, "all")
    assert cap.image.size == (3840, 1080) and (cap.x, cap.y) == (0, 0) and cap.monitor == "all"


def test_macos_capture_stitches_retina_displays(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monitors = [mon(0, "a", 0, 0, 100, 50, True), mon(0, "b", -100, 10, 100, 50)]
    monkeypatch.setattr(ad, "detect_monitors", lambda b: (monitors, "fake"))
    monkeypatch.setattr(
        ad, "_screencapture_rect",
        lambda x, y, w, h, cursor=False: Image.new("RGB", (w * 2, h * 2), (x & 255, 0, 0)),
    )
    backend = SizedBackend(100, 50)
    cap = ad.capture_screen(backend, "all")
    assert cap.image.size == (200, 60) and (cap.x, cap.y) == (-100, 0) and cap.scale == 2.0
    assert cap.image.getpixel((150, 5)) == (0, 0, 0)  # primary lands at local x 100..200
    assert cap.image.getpixel((5, 15)) == (-100 & 255, 0, 0)  # secondary at local (0..100, 10..60)
    assert cap.image.getpixel((5, 5)) == (0, 0, 0)  # above the secondary: black canvas
    cap = ad.capture_screen(backend, "primary")
    assert cap.image.size == (100, 50) and cap.monitor == "0" and (cap.x, cap.y) == (0, 0)


def test_windows_capture_uses_the_virtual_screen_bbox(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monitors = [mon(0, "a", 0, 0, 100, 50, True), mon(0, "b", -100, 10, 100, 50)]
    monkeypatch.setattr(ad, "detect_monitors", lambda b: (monitors, "fake"))
    seen = {}

    def grab(bbox=None, all_screens=False, **kwargs):
        seen.update(bbox=bbox, all_screens=all_screens)
        left, top, right, bottom = bbox
        return Image.new("RGB", (right - left, bottom - top))

    monkeypatch.setattr(ad.ImageGrab, "grab", grab)
    cap = ad.capture_screen(SizedBackend(100, 50), "all")
    assert seen == {"bbox": (-100, 0, 100, 60), "all_screens": True}
    assert cap.image.size == (200, 60) and (cap.x, cap.y) == (-100, 0)
    cap = ad.capture_screen(SizedBackend(100, 50), "1")
    assert seen["bbox"] == (-100, 10, 0, 60) and cap.monitor == "1"


def test_store_names_carry_the_monitor_tag():
    assert ad.STORE_NAME_RE.match("20261012-101905-123-m1-grid100.png")
    assert ad.STORE_NAME_RE.match("20261012-101905-123-m1.png")
    assert ad.new_screenshot_path(True, 50, "-m2").name.endswith("-m2-grid50.png")


def test_monitors_command(run, fake_gui):
    code, out, _ = run("monitors")
    lines = out.splitlines()
    assert code == 0
    assert lines[0] == (
        "OK monitors count=1 primary=0 virtual_x=0 virtual_y=0 virtual_width=1920 "
        "virtual_height=1080 source=fake"
    )
    assert lines[1] == "0 fake 0,0 1920x1080 primary scale=1"
    _, out, _ = run("--json", "monitors")
    assert json.loads(out)["monitors"][0]["name"] == "fake"


def test_screenshot_reports_origin_and_monitor(run, fake_gui, monkeypatch):
    def capture(backend, monitor="all", **kwargs):
        label = "1" if monitor == "1" else "all"
        return ad.Capture(Image.new("RGB", (64, 48)), -64, 0, 1.0, label)

    monkeypatch.setattr(ad, "capture_screen", capture)
    code, out, _ = run("screenshot", "--monitor", "1", "--grid")
    assert code == 0 and "x=-64 y=0 monitor=1" in out
    assert ad.store_files()[0][0].name.endswith("-m1-grid100.png")
    code, out, _ = run("screenshot")
    assert code == 0 and "monitor=all" in out


def test_position_reports_the_monitor(run, fake_gui):
    _, out, _ = run("position")
    assert "x=10 y=20 monitor=0 " in out
