"""The live view: frame sources, change tracking, observe, --observe, watch and the feed."""
import http.client
import json
import sys
import textwrap

import pytest
from PIL import Image, ImageDraw

import ai_desktop as ad
import ai_desktop_live as live

WHITE = (255, 255, 255)


def screen(*boxes, size=(1920, 1080)):
    img = Image.new("RGB", size, WHITE)
    draw = ImageDraw.Draw(img)
    for left, top, right, bottom in boxes:
        draw.rectangle([left, top, right - 1, bottom - 1], fill=(40, 40, 40))
    return img


class TimelineSource:
    """Frames that change at given times on the test clock: [(from_t, image), ...]."""

    name = "fake"

    def __init__(self, timeline):
        self.timeline = timeline
        self.started = 0

    def start(self):
        self.started += 1
        return {"ok": True}

    def stop(self):
        pass

    def capture(self):
        now = ad._now()
        index = max(i for i, (t, _) in enumerate(self.timeline) if t <= now)
        return index, ad.Capture(self.timeline[index][1], 0, 0, 1.0, "all")


@pytest.fixture
def view(monkeypatch):
    def make(timeline):
        t0 = ad._now()
        v = live.LiveView(TimelineSource([(t0 + t, img) for t, img in timeline]))
        v.running = True
        return v

    return make


def test_significant_blocks_ignore_a_caret():
    caret = screen((600, 400, 602, 420))
    assert live.significant_blocks(screen(), caret) == []
    dialog = screen((700, 400, 1100, 600))
    [(x, y, w, h)] = live.significant_blocks(screen(), dialog)
    assert abs(x - 700) <= 16 and abs(w - 400) <= 32


def test_observe_waits_for_the_change_to_settle(view):
    v = view([(0, screen()), (1.0, screen((700, 400, 900, 500))), (1.2, screen((700, 400, 1100, 600)))])
    result = live.observe(v, timeout=10, quiet=0.5)
    assert result["changed"] is True and result["seq"] == 2 and result["since"] == 0
    assert abs(result["changed_x"] - 700) <= 16 and abs(result["changed_width"] - 400) <= 32
    assert ad._now() - v.changed_at >= 0.5


def test_observe_ignores_a_blinking_caret_and_times_out(view):
    v = view([(0, screen()), (0.5, screen((600, 400, 602, 420))), (1.0, screen())])
    result = live.observe(v, timeout=3, quiet=0.5)
    assert result["changed"] is False


def test_change_shot_crops_small_changes_and_keeps_large_ones_whole(view, fake_gui):
    backend = ad.get_backend("pyautogui")
    small = {"changed": True, "changed_x": 700, "changed_y": 400, "changed_width": 100, "changed_height": 60,
             "_cap": ad.Capture(screen((700, 400, 800, 460)), 0, 0, 1.0, "all")}
    out = live.change_shot(backend, small)
    assert "-region-grid" in out["shot"] and out["screenshot"]["region"] == "676,376,148,108"
    big = {"changed": True, "changed_x": 0, "changed_y": 0, "changed_width": 1900, "changed_height": 1000,
           "_cap": ad.Capture(screen((0, 0, 1900, 1000)), 0, 0, 1.0, "all")}
    assert "-region" not in live.change_shot(backend, big)["shot"]


def test_frame_cuts_to_a_monitor_and_refuses_a_different_layout(view, fake_gui, monkeypatch):
    left = ad.Monitor(1, "left", -1920, 0, 1920, 1080, False, 1.0)
    main = ad.Monitor(0, "main", 0, 0, 1920, 1080, True, 1.0)
    monkeypatch.setattr(ad, "detect_monitors", lambda backend: ([main, left], "fake"))
    wide = Image.new("RGB", (3840, 1080), WHITE)
    v = live.LiveView(TimelineSource([(ad._now(), wide)]))
    v.running = True
    v.source.capture = lambda: (0, ad.Capture(wide, -1920, 0, 1.0, "all"))
    backend = ad.get_backend("pyautogui")
    cap = v.frame(backend, "1")
    assert (cap.x, cap.y, cap.image.size, cap.monitor) == (-1920, 0, (1920, 1080), "1")
    assert v.frame(backend, "all").image.size == (3840, 1080)
    monkeypatch.setattr(ad, "detect_monitors", lambda backend: ([main], "fake"))
    assert v.frame(backend, "all") is None  # stream and layout disagree: capture instead


def test_capture_screen_uses_the_live_frame(fake_gui, monkeypatch):
    frame = ad.Capture(screen(), 0, 0, 1.0, "all")
    monkeypatch.setattr(ad, "FRAME_SOURCE", lambda backend, monitor, cursor: frame)
    assert ad.capture_screen(ad.get_backend("pyautogui")) is frame
    monkeypatch.setattr(ad, "FRAME_SOURCE", lambda backend, monitor, cursor: None)
    monkeypatch.setattr(ad, "capture_tool", lambda backend, monitor="all", cursor=False: "tool")
    assert ad.capture_screen(ad.get_backend("pyautogui")) == "tool"


class Screen:
    """capture_screen replaced by a screen the test (or a fake click) changes."""

    def __init__(self, monkeypatch):
        self.image = screen()
        monkeypatch.setattr(ad, "capture_screen", lambda backend, monitor="all", cursor=False: ad.Capture(self.image.copy(), 0, 0, 1.0, "all"))


def test_click_with_observe_returns_what_it_changed(run, fake_gui, monkeypatch):
    desk = Screen(monkeypatch)
    real_click = fake_gui.click

    def click_opens_a_menu(*args, **kwargs):
        real_click(*args, **kwargs)
        desk.image = screen((500, 300, 700, 520))

    fake_gui.click = click_opens_a_menu
    code, out, _ = run("--json", "click", "--x", "500", "--y", "300", "--observe", "--instant")
    data = json.loads(out)
    assert code == 0 and data["changed"] is True
    assert abs(data["changed_x"] - 500) <= 16 and abs(data["changed_height"] - 220) <= 32
    assert "-region-grid" in data["shot"]


def test_an_action_that_changes_nothing_says_so(run, fake_gui, monkeypatch):
    Screen(monkeypatch)
    code, out, _ = run("--json", "key", "--key", "enter", "--observe")
    data = json.loads(out)
    assert code == 0 and data["changed"] is False and "shot" not in data


def test_observe_command_in_process(run, fake_gui, monkeypatch):
    desk = Screen(monkeypatch)
    calls = {"n": 0}
    real_sleep = ad._sleep

    def sleep(seconds):
        calls["n"] += 1
        if calls["n"] == 20:
            desk.image = screen((100, 100, 500, 400))  # a window opens a moment later
        real_sleep(seconds)

    monkeypatch.setattr(ad, "_sleep", sleep)
    code, out, _ = run("observe", "--timeout", "10")
    assert code == 0 and "changed=True" in out and "shot=" in out
    code, out, _ = run("observe", "--timeout", "1", "--no-shot")
    assert code == 0 and "changed=False" in out and "shot=" not in out


def test_watch_prints_a_line_per_change(run, fake_gui, monkeypatch):
    payloads = iter([
        {"ok": True, "command": "observe", "changed": True, "seq": 3, "changed_x": 1, "changed_y": 2, "changed_width": 30, "changed_height": 40},
        {"ok": True, "command": "observe", "changed": False, "seq": 3},
        {"ok": True, "command": "observe", "changed": True, "seq": 5, "changed_x": 9, "changed_y": 9, "changed_width": 50, "changed_height": 50},
    ])
    asked = []
    monkeypatch.setattr(ad, "observe_via_service", lambda argv: asked.append(argv) or next(payloads))
    code, out, _ = run("watch", "--limit", "2")
    lines = out.splitlines()
    assert code == 0 and len(lines) == 3 and lines[0].startswith("OK watch changed=True seq=3")
    assert lines[2] == "OK watch done=True changes=2"
    assert "--since" not in asked[0] and asked[1][asked[1].index("--since") + 1] == "3"


def test_live_needs_the_service(run):
    code, _, err = run("live", "start")
    assert code == 1 and "background service" in err


def test_stream_needs_gnome_wayland(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert "Mutter" in live.stream_available()


def test_compose_puts_monitors_on_one_canvas():
    parts = [
        {"x": 0, "y": 0, "scale": 2.0, "width": 400, "height": 200},
        {"x": -300, "y": 50, "scale": 1.0, "width": 300, "height": 100},
    ]
    blobs = [Image.new("RGB", (400, 200), (255, 0, 0)).tobytes(), Image.new("RGB", (300, 100), (0, 0, 255)).tobytes()]
    cap = live.compose(parts, blobs)
    assert (cap.x, cap.y, cap.image.size) == (-300, 0, (500, 150))
    assert cap.image.getpixel((350, 10)) == (255, 0, 0) and cap.image.getpixel((10, 60)) == (0, 0, 255)


FAKE_HELPER = textwrap.dedent('''
    import json, sys
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        op = json.loads(line)["op"]
        if op in ("start", "info"):
            out.write(json.dumps({"ok": True, "running": True, "seq": 7}).encode() + b"\\n")
        elif op == "frame":
            data = bytes([10, 20, 30]) * (4 * 2)
            head = {"ok": True, "seq": 7, "parts": [{"connector": "X", "x": 0, "y": 0, "scale": 1.0, "width": 4, "height": 2, "bytes": len(data)}]}
            out.write(json.dumps(head).encode() + b"\\n" + data)
        elif op == "stop":
            out.write(b'{"ok": true, "running": false}\\n')
        out.flush()
''')


def test_stream_source_speaks_the_helper_protocol(tmp_path, monkeypatch):
    script = tmp_path / "ai_desktop_helper.py"
    script.write_text(FAKE_HELPER)
    monkeypatch.setattr(ad, "__file__", str(tmp_path / "ai_desktop.py"))
    source = live.StreamSource(helper_factory=lambda: live.HelperProcess(python=sys.executable))
    assert source.start()["running"] is True
    seq, cap = source.capture()
    assert seq == 7 and cap.image.size == (4, 2) and cap.image.getpixel((0, 0)) == (10, 20, 30)
    source.stop()


@pytest.fixture
def feed(view, tmp_path):
    v = view([(0, screen())])
    f = live.Feed(v, port=0, home=ad.home_dir())
    yield f
    f.shutdown()


def get(feed, path, method="GET", headers=None):
    port = feed.server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body, resp.getheader("Content-Type")


def test_feed_needs_the_token_and_its_own_host_and_origin(feed):
    assert get(feed, "/")[0] == 403
    assert get(feed, "/?token=wrong")[0] == 403
    status, body, ctype = get(feed, f"/?token={feed.token}")
    assert status == 200 and b"STOP" in body and ctype.startswith("text/html")
    port = feed.server.server_address[1]
    assert get(feed, f"/?token={feed.token}", headers={"Host": f"evil.example:{port}"})[0] == 403  # DNS rebinding
    assert get(feed, f"/?token={feed.token}", headers={"Origin": "https://evil.example"})[0] == 403  # another site


def test_feed_serves_frames_and_the_stop_button_stops(feed):
    status, body, ctype = get(feed, f"/frame.jpg?token={feed.token}&max_width=640&grid=1")
    assert status == 200 and ctype == "image/jpeg" and body[:2] == b"\xff\xd8"
    assert not ad.stop_path().exists()
    status, _, _ = get(feed, "/stop", method="POST", headers={"Authorization": f"Bearer {feed.token}"})
    assert status == 200 and ad.stop_path().exists()
    status, body, _ = get(feed, f"/status?token={feed.token}")
    assert json.loads(body)["stopped"] is True
