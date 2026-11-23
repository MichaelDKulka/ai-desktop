import json
import sys
import types

import pytest
from PIL import Image, ImageDraw

import ai_desktop as ad

WHITE = (255, 255, 255)


def screen_with(*boxes, size=(1366, 768)):
    """A white screen with filled rectangles: (left, top, right, bottom, colour)."""
    img = Image.new("RGB", size, WHITE)
    draw = ImageDraw.Draw(img)
    for left, top, right, bottom, colour in boxes:
        draw.rectangle([left, top, right - 1, bottom - 1], fill=colour)
    return img


# --- comparison helpers ------------------------------------------------------------------


def test_patch_diff():
    base = screen_with()
    assert ad.patch_diff(base, base.copy(), (500, 300)) == 0.0
    small = screen_with((495, 295, 505, 305, (0, 0, 0)))  # 10x10 of 129x129
    assert 0.005 < ad.patch_diff(base, small, (500, 300)) < 0.007
    big = screen_with((470, 270, 530, 330, (0, 0, 0)))  # 60x60
    assert 0.2 < ad.patch_diff(base, big, (500, 300)) < 0.23
    far = screen_with((700, 300, 760, 360, (0, 0, 0)))  # 200 px away
    assert ad.patch_diff(base, far, (500, 300)) == 0.0
    assert ad.patch_diff(base, big, (500, 300), masks=[(460, 260, 540, 340)]) == 0.0


def test_patch_diff_sees_colour_swaps_of_equal_brightness():
    red = screen_with((470, 270, 530, 330, (200, 0, 0)))
    green = screen_with((470, 270, 530, 330, (0, 200, 0)))
    assert ad.patch_diff(red, green, (500, 300)) > 0.2


def test_patch_diff_clamps_at_the_edge():
    base = screen_with()
    corner = screen_with((0, 0, 20, 20, (0, 0, 0)))
    assert ad.patch_box(base.size, (3, 3)) == (0, 0, 68, 68)
    assert 0.08 < ad.patch_diff(base, corner, (3, 3)) < 0.09  # 400 of 68x68


def test_fingerprint_diff_ignores_clocks_and_toasts_but_not_dialogs():
    base = ad.fingerprint(screen_with())
    clock = ad.fingerprint(screen_with((1250, 5, 1310, 25, (0, 0, 0))))
    toast = ad.fingerprint(screen_with((500, 20, 850, 100, (40, 40, 40))))
    dialog = ad.fingerprint(screen_with((383, 184, 983, 584, (60, 60, 60))))
    assert ad.fingerprint_diff(base, clock) < 0.005
    assert 0.02 < ad.fingerprint_diff(base, toast) < ad.GUARD_SCREEN_MAX
    assert ad.fingerprint_diff(base, dialog) > 0.2


def test_fingerprint_masks_cells():
    img = screen_with((0, 0, 100, 100, (0, 0, 0)))
    values = ad.fingerprint(img, masks=[(0, 0, 100, 100)])
    assert values[0] is None and values[-1] is not None
    assert ad.fingerprint_diff(ad.fingerprint(screen_with(), masks=[(0, 0, 100, 100)]), values) == 0.0


# --- the command line --------------------------------------------------------------------


@pytest.fixture
def desk(monkeypatch):
    """A fake 1920x1080 screen whose contents the test can change between captures."""
    state = {"image": Image.new("RGB", (1920, 1080), WHITE), "cursor": "none", "calls": []}

    def capture(backend, monitor="all", cursor=False):
        state["calls"].append(cursor)
        return ad.Capture(state["image"].copy(), 0, 0, 1.0, "all", state["cursor"] if cursor else "none")

    monkeypatch.setattr(ad, "capture_screen", capture)
    return state


def paint(state, left, top, right, bottom, colour=(0, 0, 0)):
    img = state["image"].copy()
    ImageDraw.Draw(img).rectangle([left, top, right - 1, bottom - 1], fill=colour)
    state["image"] = img


def shoot(run):
    code, out, _ = run("screenshot", "--grid")
    assert code == 0
    return out.split("path=")[1].split()[0]


def test_click_with_an_unchanged_screen_goes_ahead(run, fake_gui, desk):
    path = shoot(run)
    code, out, _ = run("click", "--x", "500", "--y", "300", "--seen", path)
    assert code == 0 and out == f"OK click x=500 y=300 button=left clicks=1 seen={path} seen_diff=0.0\n"
    assert fake_gui.calls[-1][0] == "click"
    assert desk["calls"][-1] is False  # guard captures never include the pointer


def test_click_aborts_when_the_target_area_changed(run, fake_gui, desk):
    path = shoot(run)
    paint(desk, 450, 250, 560, 360)  # a dialog over the target
    fake_gui.calls.clear()
    code, _, err = run("click", "--x", "500", "--y", "300", "--seen", path)
    assert code == 1 and err.startswith("ERROR screen changed near (500, 300) since ")
    assert "of the 129x129 px area around it differs (limit 5%); take a new screenshot" in err
    assert fake_gui.calls == []  # not even a move


def test_click_ignores_changes_elsewhere(run, fake_gui, desk):
    path = shoot(run)
    paint(desk, 1500, 900, 1900, 1000)  # a toast far from the target
    code, out, _ = run("click", "--x", "500", "--y", "300", "--seen", path)
    assert code == 0 and "seen_diff=0.0" in out


def test_drag_checks_the_areas_around_both_ends(run, fake_gui, desk):
    path = shoot(run)
    drag = ("drag", "--x1", "300", "--y1", "300", "--x2", "1200", "--y2", "600", "--seen", path)
    paint(desk, 1150, 550, 1260, 660)  # something new over the drop target
    fake_gui.calls.clear()
    code, _, err = run(*drag)
    assert code == 1 and err.startswith("ERROR screen changed near (1200, 600) since ")
    assert fake_gui.calls == []
    paint(desk, 1150, 550, 1260, 660, colour=WHITE)
    code, out, _ = run(*drag)
    assert code == 0 and out.endswith(f"seen={path} seen_diff=0.0\n")
    assert len(desk["calls"]) == 3  # the screenshot and one capture per guarded drag


def test_typing_checks_the_whole_screen(run, fake_gui, desk):
    path = shoot(run)
    paint(desk, 1500, 950, 1850, 1030)  # a toast: fine
    assert run("type", "--text", "hi", "--seen", path)[0] == 0
    paint(desk, 500, 250, 1400, 850)  # a dialog: not fine
    code, _, err = run("key", "--key", "enter", "--seen", path)
    assert code == 1 and "of the screen differs (limit 10%)" in err


def test_seen_latest_and_the_json_fields(run, fake_gui, desk):
    shoot(run)
    paint(desk, 450, 250, 560, 360)
    code, out, _ = run("--json", "move", "--x", "500", "--y", "300", "--seen", "latest")
    data = json.loads(out)
    assert code == 1 and data["changed"] > 0.05 and data["limit"] == 0.05 and data["seen"].endswith(".png")


def test_a_native_pointer_in_the_reference_is_masked(run, fake_gui, desk, monkeypatch):
    monkeypatch.setattr(fake_gui, "position", lambda: (500, 300))
    for cursor, expected in (("native", 0), ("none", 1)):
        desk["cursor"] = cursor
        desk["image"] = Image.new("RGB", (1920, 1080), WHITE)
        paint(desk, 500, 300, 530, 340)  # a big pointer drawn at (500, 300): 7% of the patch
        path = shoot(run)
        desk["image"] = Image.new("RGB", (1920, 1080), WHITE)  # guard captures have no pointer
        code, _, _ = run("click", "--x", "505", "--y", "305", "--seen", path)
        assert code == expected, cursor  # masked only when the tool drew the pointer


def test_guard_errors_say_what_to_do(run, fake_gui, desk, monkeypatch, tmp_path):
    code, _, err = run("click", "--x", "5", "--y", "5", "--seen", str(tmp_path / "nope.png"))
    assert code == 1 and "--seen file not found" in err
    old = tmp_path / "old.png"
    Image.new("RGB", (10, 10)).save(old)
    code, _, err = run("click", "--x", "5", "--y", "5", "--seen", str(old))
    assert code == 1 and "no old.raw.png next to" in err and "take a new one" in err
    code, _, err = run("click", "--x", "5", "--y", "5", "--seen", "latest")
    assert code == 1 and "no screenshots" in err

    path = shoot(run)
    desk["image"] = Image.new("RGB", (1366, 768), WHITE)
    code, _, err = run("click", "--x", "5", "--y", "5", "--seen", path)
    assert code == 1 and "screen layout changed" in err

    monkeypatch.setattr(
        ad, "capture_screen",
        lambda backend, monitor="all", cursor=False: ad.Capture(Image.new("RGB", (400, 300), WHITE), 0, 0, 1.0, "all"),
    )
    path = shoot(run)
    code, _, err = run("click", "--x", "900", "--y", "500", "--seen", path)
    assert code == 1 and "outside the seen screenshot, which covers 0..399, 0..299" in err


def test_the_guard_is_logged(run, fake_gui, desk):
    path = shoot(run)
    run("click", "--x", "500", "--y", "300", "--seen", path)
    entry = ad.read_log_entries(1)[0]
    assert entry["args"]["seen"] == path and entry["result"]["seen_diff"] == 0.0


def test_the_guard_reads_a_zoom_screenshot_s_full_copy(run, fake_gui, desk):
    code, out, _ = run("screenshot", "--zoom", "500,300", "--radius", "50")
    path = out.split("path=")[1].split()[0]
    code, out, _ = run("click", "--x", "1500", "--y", "900", "--seen", path)  # outside the zoom
    assert code == 0 and "seen_diff=0.0" in out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uinput is Linux only")
def test_a_native_pointer_at_an_unknown_position_matches_itself(run, desk, monkeypatch):
    # Wayland: the capture tool draws the pointer, but nobody knows where it is.
    monkeypatch.setenv("AI_DESKTOP_SCREEN_SIZE", "1920x1080")
    monkeypatch.setattr(ad, "_UInputDevice", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no device")))
    desk["cursor"] = "native"
    paint(desk, 500, 300, 530, 340)  # the real pointer, resting on the target
    code, out, _ = run("--backend", "uinput", "screenshot", "--grid")
    assert code == 0 and "pointer=unknown" in out
    path = out.split("path=")[1].split()[0]
    assert ad.load_reference(path).masks() == []  # nothing to mask by position
    ref = ad.load_reference(path)
    state = {"asked": []}
    real = ad.capture_screen

    def spy(backend, monitor="all", cursor=False):
        state["asked"].append(cursor)
        return real(backend, monitor, cursor=cursor)

    monkeypatch.setattr(ad, "capture_screen", spy)
    diff = ad.guard_seen(types.SimpleNamespace(seen=path), ad.get_backend("uinput"), (505, 305))
    assert state["asked"] == [True] and diff["seen_diff"] == 0.0 and ref.cursor == "native"


@pytest.mark.parametrize("dialog", [(400, 200), (500, 250), (600, 300)])
def test_keyboard_guard_catches_a_small_dialog(run, fake_gui, desk, dialog):
    # Each of these changes less than 10% of a 1920x1080 screen, so the share alone passed.
    path = shoot(run)
    w, h = dialog
    left, top = (1920 - w) // 2, (1080 - h) // 2
    paint(desk, left, top, left + w, top + h, colour=(48, 48, 48))
    fake_gui.calls.clear()
    code, out, err = run("--json", "key", "--key", "enter", "--seen", path)
    data = json.loads(out)
    assert code == 1 and data["changed"] < ad.GUARD_SCREEN_MAX
    assert "is new or different" in data["error"] and "take a new screenshot" in data["error"]
    bx, by, bw, bh = data["region"]
    assert abs(bx - left) <= 16 and abs(by - top) <= 16 and abs(bw - w) <= 32 and abs(bh - h) <= 32
    assert fake_gui.calls == []


def test_keyboard_guard_lets_toasts_and_clocks_through(run, fake_gui, desk):
    path = shoot(run)
    paint(desk, 1500, 950, 1850, 1030, colour=(40, 40, 40))  # a 350x80 toast
    paint(desk, 1800, 4, 1900, 24)  # a clock
    paint(desk, 600, 400, 610, 420)  # a blinking caret
    code, out, _ = run("type", "--text", "hi", "--seen", path)
    assert code == 0 and "seen_diff=" in out


def test_changed_blocks_finds_each_region():
    a = Image.new("RGB", (1366, 768), WHITE)
    b = a.copy()
    draw = ImageDraw.Draw(b)
    draw.rectangle([100, 100, 499, 299], fill=(0, 0, 0))
    draw.rectangle([900, 500, 1000, 540], fill=(0, 0, 0))
    blocks = sorted(ad.changed_blocks(a, b), key=lambda r: r[2] * r[3], reverse=True)
    assert len(blocks) == 2
    x, y, w, h = blocks[0]
    assert 84 <= x <= 100 and 84 <= y <= 100 and 400 <= w <= 432 and 200 <= h <= 232
    assert ad.changed_blocks(a, a.copy()) == []


def test_a_guarded_glide_is_short(run, fake_gui, desk, sleeps):
    path = shoot(run)
    sleeps.clear()
    assert run("click", "--x", "1500", "--y", "900")[0] == 0
    unguarded = sum(sleeps)
    sleeps.clear()
    assert run("click", "--x", "1500", "--y", "900", "--seen", path)[0] == 0
    guarded = sum(sleeps) - ad.SHOT_SETTLE * 0  # no --shot here
    assert unguarded > 0.6 and guarded <= ad.GUARDED_GLIDE_MAX * 1.5


def test_the_input_device_is_ready_before_the_guard_looks(run, fake_gui, desk, monkeypatch):
    order = []

    class Dev:
        def __init__(self, name, **kwargs):
            order.append(("device", name))
            self.last_abs = (0, 0)

        def emit(self, *args, **kwargs):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    capture = ad.capture_screen
    monkeypatch.setattr(ad, "capture_screen", lambda *a, **k: order.append(("capture",)) or capture(*a, **k))
    monkeypatch.setattr(ad, "_UInputDevice", Dev)
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    monkeypatch.setenv("AI_DESKTOP_SCREEN_SIZE", "1920x1080")
    monkeypatch.setattr(ad.sys, "platform", "linux")
    path = shoot(run)
    order.clear()
    assert run("--backend", "uinput", "click", "--x", "500", "--y", "300", "--seen", path)[0] == 0
    assert order.index(("device", "ai-desktop pointer")) < order.index(("capture",))
    order.clear()
    assert run("--backend", "uinput", "key", "--key", "enter", "--seen", path)[0] == 0
    assert order.index(("device", "ai-desktop keyboard")) < order.index(("capture",))


def test_screenshot_if_changed_reuses_an_unchanged_screenshot(run, fake_gui, desk):
    first = shoot(run)
    count = len(ad.store_files())
    code, out, _ = run("screenshot", "--grid", "--if-changed")
    assert code == 0 and "unchanged=True" in out and f"path={first}" in out
    assert len(ad.store_files()) == count  # nothing new saved
    paint(desk, 700, 400, 712, 412)  # a ticked checkbox: small, but a change
    code, out, _ = run("screenshot", "--grid", "--if-changed", first)
    assert code == 0 and "unchanged=False" in out and f"path={first}" not in out
    assert "changed_x=700 changed_y=400 changed_width=12 changed_height=12" in out
    assert len(ad.store_files()) == count + 1


def test_if_changed_without_a_screenshot_takes_one(run, fake_gui, desk):
    code, out, _ = run("screenshot", "--if-changed")
    assert code == 0 and "unchanged=False" in out and len(ad.store_files()) == 1


def test_if_changed_and_zoom_do_not_mix(run, fake_gui, desk):
    shoot(run)
    code, _, err = run("screenshot", "--zoom", "500,300", "--if-changed")
    assert code == 2 and "--if-changed" in err
