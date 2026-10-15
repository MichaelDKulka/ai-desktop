import math
import random

import pytest

import ai_desktop as ad

SCREEN = ad.Monitor(0, "fake", 0, 0, 1920, 1080, True, 1.0)
CORNERS = {(0, 0), (1919, 0), (0, 1079), (1919, 1079)}


def on_screen(x, y):
    return 0 <= x < 1920 and 0 <= y < 1080


def test_human_path_invariants_over_seeds():
    for seed in range(100):
        rng = random.Random(seed)
        start = (rng.randrange(1920), rng.randrange(1080))
        end = (rng.randrange(1920), rng.randrange(1080))
        path = ad.human_path(start, end, [SCREEN], (1920, 1080), random.Random(seed))
        if math.dist(start, end) < 2:
            assert path == []
            continue
        assert len(path) <= 59
        assert all(pause >= 0 for _, _, pause in path)
        assert all(on_screen(x, y) and (x, y) not in CORNERS for x, y, _ in path)
        assert end not in [(x, y) for x, y, _ in path]
        assert path == ad.human_path(start, end, [SCREEN], (1920, 1080), random.Random(seed))


def test_long_path_has_many_steps_and_takes_human_time():
    path = ad.human_path((100, 100), (1500, 900), [SCREEN], (1920, 1080), random.Random(1))
    assert len(path) >= 50
    assert 0.15 <= sum(pause for _, _, pause in path) <= 1.5 * 1.4


def test_human_path_is_curved():
    start, end = (200, 500), (700, 500)
    path = ad.human_path(start, end, [SCREEN], (1920, 1080), random.Random(3))
    assert max(abs(y - 500) for _, y, _ in path) >= 5


def test_short_move_has_no_waypoints():
    assert ad.human_path((10, 10), (11, 10), [SCREEN], (1920, 1080), random.Random(0)) == []


def test_path_near_a_corner_never_touches_it():
    for seed in range(50):
        path = ad.human_path((300, 300), (1, 1), [SCREEN], (1920, 1080), random.Random(seed))
        assert all((x, y) not in CORNERS for x, y, _ in path)


def test_confine_point():
    left = ad.Monitor(1, "left", -1920, 100, 1920, 1080, False, 1.0)
    monitors = [SCREEN, left]
    assert ad.confine_point(0, 0, monitors, (1920, 1080)) == (1, 1)
    assert ad.confine_point(1919, 1079, monitors, (1920, 1080)) == (1918, 1078)
    assert ad.confine_point(-5, 50, monitors, (1920, 1080)) in ((0, 50), (-5, 100))
    assert ad.confine_point(-2500, 600, monitors, (1920, 1080)) == (-1920, 600)
    assert ad.confine_point(5000, 5000, monitors, (1920, 1080)) == (1918, 1078)
    assert ad.confine_point(800.4, 300.6, monitors, (1920, 1080)) == (800, 301)


def test_typing_plan():
    plan = ad.typing_plan("ab cd\n", random.Random(0))
    assert [ch for ch, _ in plan] == list("ab cd\n")
    pauses = dict(enumerate(pause for _, pause in plan))
    for i in (0, 1, 3, 4):  # a, b, c, d
        assert 0.10 <= pauses[i] <= 0.15
    assert 0.25 <= pauses[2] <= 0.5  # the space
    assert pauses[5] == 0.0  # nothing to wait for after the last key
    assert ad.typing_plan("", random.Random(0)) == []


def test_synthetic_start_stays_on_the_monitor():
    for seed in range(50):
        x, y = ad.synthetic_start((5, 5), SCREEN, random.Random(seed))
        assert on_screen(x, y) and (x, y) != (5, 5)


def test_cli_click_glides_then_clicks(run, fake_gui, sleeps):
    code, out, _ = run("click", "--x", "500", "--y", "300")
    assert code == 0 and out == "OK click x=500 y=300 button=left clicks=1\n"
    names = [call[0] for call in fake_gui.calls]
    assert names[-1] == "click" and set(names[:-1]) == {"moveTo"}
    assert len(sleeps) == len(names) - 1
    assert 0.15 * 0.6 <= sum(sleeps) <= 1.5 * 1.4


def test_move_lands_exactly_on_the_target(run, fake_gui):
    run("move", "--x", "1234", "--y", "567")
    assert fake_gui.calls[-1] == ("moveTo", (1234, 567), {})


@pytest.mark.parametrize(
    "argv,env",
    [
        (("--instant", "click", "--x", "500", "--y", "300"), None),
        (("click", "--x", "500", "--y", "300", "--instant"), None),
        (("click", "--x", "500", "--y", "300"), "0"),
    ],
)
def test_instant_flag_and_env(run, fake_gui, sleeps, monkeypatch, argv, env):
    if env is not None:
        monkeypatch.setenv("AI_DESKTOP_HUMAN", env)
    code, _, _ = run(*argv)
    assert code == 0
    assert [call[0] for call in fake_gui.calls] == ["click"] and sleeps == []
    [entry] = ad.read_log_entries(5)
    assert entry["args"].get("instant") is (True if env is None else None)


def test_instant_does_not_leak_into_the_next_command(run, fake_gui):
    run("--instant", "move", "--x", "500", "--y", "300")
    run("move", "--x", "900", "--y", "700")
    assert len([c for c in fake_gui.calls if c[0] == "moveTo"]) > 2


def test_seed_makes_a_command_repeat(run, fake_gui, monkeypatch):
    run("--seed", "7", "move", "--x", "900", "--y", "600")
    first = list(fake_gui.calls)
    fake_gui.calls.clear()
    monkeypatch.setattr(ad, "RNG", random.Random(12345))  # a different state in between
    run("move", "--x", "900", "--y", "600", "--seed", "7")
    assert fake_gui.calls == first
    monkeypatch.setenv("AI_DESKTOP_SEED", "abc")
    code, _, err = run("move", "--x", "900", "--y", "600")
    assert code == 1 and "AI_DESKTOP_SEED" in err


class FakeDev:
    events: list = []
    created = 0
    closed = 0

    def __init__(self, name, keys=(), rel=(), abs_axes=()):
        self.name = name
        FakeDev.created += 1

    def emit(self, ev_type, code, value):
        FakeDev.events.append((ev_type, code, value))

    def close(self):
        FakeDev.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def uinput(monkeypatch):
    FakeDev.events = []
    FakeDev.created = FakeDev.closed = 0
    monkeypatch.setattr(ad, "_UInputDevice", FakeDev)
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    backend = ad.UInputBackend.__new__(ad.UInputBackend)  # skip the Linux-only __init__
    backend._size = (1920, 1080)
    return backend


def abs_points(events):
    points, x = [], None
    for ev_type, code, value in events:
        if ev_type == ad.EV_ABS and code == ad.ABS_X:
            x = value
        elif ev_type == ad.EV_ABS and code == ad.ABS_Y:
            points.append((x, value))
    return points


def test_uinput_click_glides_and_remembers(uinput):
    uinput.click(500, 300, "left", 1)
    points = abs_points(FakeDev.events)
    assert len(points) > 1 and points[-1] == (500, 300)
    assert all(on_screen(x, y) for x, y in points)
    last_abs = max(i for i, e in enumerate(FakeDev.events) if e[0] == ad.EV_ABS)
    assert FakeDev.events[last_abs + 1:] == [
        (ad.EV_KEY, ad.BTN_LEFT, 1), (ad.EV_KEY, ad.BTN_LEFT, 0),
    ]
    assert ad.recall_pointer() == (500, 300)


def test_uinput_starts_from_the_remembered_position(uinput):
    ad.remember_pointer(100, 100)
    uinput.move(900, 700)
    first = abs_points(FakeDev.events)[0]
    assert math.dist(first, (100, 100)) < 60
    assert ad.recall_pointer() == (900, 700)


def test_uinput_ignores_an_off_screen_memory(uinput):
    ad.remember_pointer(-5000, 100)
    uinput.move(900, 700)
    points = abs_points(FakeDev.events)
    assert points[0] != (900, 700) and all(on_screen(x, y) for x, y in points)


def test_uinput_instant_and_typing(uinput, monkeypatch, sleeps):
    monkeypatch.setattr(ad, "HUMAN_INPUT", False)
    uinput.move(900, 700)
    assert abs_points(FakeDev.events) == [(900, 700)]
    FakeDev.events.clear()
    monkeypatch.setattr(ad, "HUMAN_INPUT", True)
    uinput.type_text("a b", 0.02)
    presses = [code for ev_type, code, value in FakeDev.events if ev_type == ad.EV_KEY and value == 1]
    assert presses == [30, 57, 48]  # a, space, b
    assert len(sleeps) == 2 and 0.10 <= sleeps[0] <= 0.15 and 0.25 <= sleeps[1] <= 0.5


def test_uinput_clamps_to_the_axis_range(uinput):
    uinput._emit_abs(FakeDev("x"), -40, 5000)
    assert abs_points(FakeDev.events) == [(0, 1079)]


def test_one_device_per_action_by_default(uinput):
    uinput.click(500, 300, "left", 1)
    uinput.move(600, 300)
    assert FakeDev.created == 2 and FakeDev.closed == 2 and ad._DEVICE_CACHE is None


def test_keep_devices_reuses_one_pointer_and_one_keyboard(uinput):
    with ad.keep_devices():
        uinput.click(500, 300, "left", 1)
        uinput.move(600, 300)
        uinput.scroll(-2, 700, 300)
        assert FakeDev.created == 1 and FakeDev.closed == 0
        uinput.type_text("ab", 0.02)
        uinput.press((28, False))
        uinput.hotkey([(29, False), (38, False)])
        assert FakeDev.created == 2 and FakeDev.closed == 0
        with ad.keep_devices():  # nested: shares the outer session
            uinput.move(700, 300)
        assert FakeDev.created == 2 and FakeDev.closed == 0
    assert FakeDev.closed == 2 and ad._DEVICE_CACHE is None


def test_a_failed_action_destroys_its_cached_device(uinput, monkeypatch):
    with ad.keep_devices():
        uinput.press((28, False))
        original = FakeDev.emit

        def boom(self, ev_type, code, value):
            if code == 38:
                raise RuntimeError("write failed")
            original(self, ev_type, code, value)

        monkeypatch.setattr(FakeDev, "emit", boom)
        with pytest.raises(RuntimeError):
            uinput.hotkey([(29, False), (38, False)])  # ctrl is held when it fails
        assert FakeDev.closed == 1 and ("keyboard",) not in ad._DEVICE_CACHE
        monkeypatch.setattr(FakeDev, "emit", original)
        uinput.press((28, False))
        assert FakeDev.created == 2


def test_a_resized_screen_gets_a_new_pointer(uinput):
    with ad.keep_devices():
        uinput.move(500, 300)
        uinput._size = (1366, 768)
        uinput.move(400, 300)
        assert FakeDev.created == 2 and FakeDev.closed == 1


def test_reused_pointer_steps_aside_before_repeating_a_position(uinput, monkeypatch):
    class TrackingDev(FakeDev):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.last_abs = (0, 0)

    monkeypatch.setattr(ad, "_UInputDevice", TrackingDev)
    monkeypatch.setattr(ad, "HUMAN_INPUT", False)
    with ad.keep_devices():
        uinput.click(500, 300, "left", 1)
        FakeDev.events.clear()
        uinput.click(500, 300, "left", 1)
    xs = [value for ev_type, code, value in FakeDev.events if ev_type == ad.EV_ABS and code == ad.ABS_X]
    assert xs == [501, 500]
    FakeDev.events.clear()
    with ad.keep_devices():
        uinput.click(500, 300, "left", 1)
        uinput.click(600, 300, "left", 1)
    xs = [value for ev_type, code, value in FakeDev.events if ev_type == ad.EV_ABS and code == ad.ABS_X]
    assert xs == [500, 600]


def test_pointer_memory_survives_garbage(tmp_path):
    assert ad.recall_pointer() is None
    ad.home_dir().mkdir(parents=True, exist_ok=True)
    (ad.home_dir() / ad.POINTER_FILE).write_text("not json", encoding="utf-8")
    assert ad.recall_pointer() is None
