import json
import types

import pytest
from PIL import Image, ImageDraw

import ai_desktop as ad

WHITE = (255, 255, 255)


def frame(*boxes):
    img = Image.new("RGB", (1920, 1080), WHITE)
    draw = ImageDraw.Draw(img)
    for left, top, right, bottom in boxes:
        draw.rectangle([left, top, right - 1, bottom - 1], fill=(0, 0, 0))
    return img


@pytest.fixture
def frames(monkeypatch):
    """Captures come from `.queue` in order (the last one repeats); `.asked` records the
    `cursor` argument of each capture."""
    state = types.SimpleNamespace(queue=[frame()], asked=[])

    def capture(backend, monitor="all", cursor=False):
        state.asked.append(cursor)
        img = state.queue.pop(0) if len(state.queue) > 1 else state.queue[0]
        return ad.Capture(img.copy(), 0, 0, 1.0, "all")

    monkeypatch.setattr(ad, "capture_screen", capture)
    return state


# --- --shot ------------------------------------------------------------------------------


def test_shot_adds_a_screenshot_to_the_action(run, fake_gui, frames, sleeps):
    code, out, _ = run("click", "--x", "500", "--y", "300", "--shot")
    assert code == 0 and out.startswith("OK click x=500 y=300 button=left clicks=1 shot=")
    path = out.split("shot=")[1].split()[0]
    assert ad.store_files()[0][0] == ad.Path(path) and path.endswith("-grid100.png")
    assert sleeps[-1] == 0.4  # settle before the capture
    _, out, _ = run("--json", "key", "--key", "enter", "--shot")
    data = json.loads(out)
    assert data["shot"] == data["screenshot"]["path"] and data["screenshot"]["spacing"] == 100


def test_a_failed_shot_does_not_fail_the_action(run, fake_gui, monkeypatch):
    def broken(backend, monitor="all", cursor=False):
        raise ad.CliError("gnome-screenshot failed: busy")

    monkeypatch.setattr(ad, "capture_screen", broken)
    code, out, _ = run("click", "--x", "500", "--y", "300", "--shot")
    assert code == 0 and ' shot=error shot_error="gnome-screenshot failed: busy"' in out
    assert fake_gui.calls[-1][0] == "click"


def test_shot_settle_delay_is_configurable(run, fake_gui, frames, sleeps, monkeypatch):
    monkeypatch.setenv("AI_DESKTOP_SHOT_DELAY", "1.5")
    run("key", "--key", "enter", "--shot")
    assert sleeps[-1] == 1.5
    monkeypatch.setenv("AI_DESKTOP_SHOT_DELAY", "soon")
    code, _, _ = run("key", "--key", "enter", "--shot")
    assert code == 0 and sleeps[-1] == 0.4


# --- wait --------------------------------------------------------------------------------


def test_wait_stable_returns_once_two_captures_match(run, fake_gui, frames, sleeps):
    frames.queue[:] = [frame((0, 0, 900, 500)), frame(), frame()]
    code, out, _ = run("wait", "--stable")
    assert code == 0 and out.startswith("OK wait mode=stable polls=3 elapsed=1.0 diff=0.0")
    assert sleeps == [0.5, 0.5] and frames.asked == [False, False, False]


def test_wait_stable_gives_up_on_a_screen_that_keeps_changing(run, fake_gui, monkeypatch):
    count = [0]

    def capture(backend, monitor="all", cursor=False):
        count[0] += 1
        return ad.Capture(frame((0, 0, 900, 500)) if count[0] % 2 else frame(), 0, 0, 1.0, "all")

    monkeypatch.setattr(ad, "capture_screen", capture)
    code, _, err = run("wait", "--stable", "--timeout", "2")
    assert code == 1 and err.startswith("ERROR screen still changing after 2 s (last diff")
    assert count[0] == 5  # at 0, 0.5, 1, 1.5 and 2 s


def test_wait_changed_ignores_small_changes(run, fake_gui, frames):
    path = run("screenshot", "--grid")[1].split("path=")[1].split()[0]
    frames.queue[:] = [frame((1800, 5, 1860, 25)), frame((1800, 5, 1860, 25)), frame((0, 0, 900, 500))]
    code, out, _ = run("wait", "--changed", path)
    assert code == 0 and "mode=changed polls=3 " in out
    frames.queue[:] = [frame((1800, 5, 1860, 25))]
    code, _, err = run("wait", "--changed", "latest", "--timeout", "1")
    assert code == 1 and "screen unchanged after 1 s" in err


def test_wait_shot_reuses_the_last_capture(run, fake_gui, frames):
    code, out, _ = run("wait", "--stable", "--shot")
    assert code == 0 and " shot=" in out and len(frames.asked) == 2  # two polls, no third capture
    assert ad.store_files()[0][0].name.endswith("-grid100.png")


def test_wait_needs_a_mode(run):
    with pytest.raises(SystemExit) as info:
        run("wait")
    assert info.value.code == 2


# --- run ---------------------------------------------------------------------------------


def test_run_does_every_step_and_logs_each(run, fake_gui, frames):
    steps = [["move", "--x", "500", "--y", "300"], ["type", "--text", "hunter2"], "key --key enter"]
    code, out, _ = run("run", "--steps", json.dumps(steps), "--shot")
    assert code == 0 and out.startswith("OK run steps=3 done=3 shot=")
    assert [c[0] for c in fake_gui.calls if c[0] != "moveTo"] == ["write"] * 7 + ["press"]
    entries = ad.read_log_entries(10)
    assert [(e["command"], e["via"]) for e in entries] == [
        ("run", "cli"), ("key", "cli/run"), ("type", "cli/run"), ("move", "cli/run"),
    ]
    assert entries[0]["args"]["steps"] == ["move", "type", "key"]
    assert "hunter2" not in ad.log_files()[0][0].read_text(encoding="utf-8")


def test_run_stops_at_the_first_failure(run, fake_gui):
    steps = [
        ["move", "--x", "500", "--y", "300"],
        ["type", "--text", "hunter2"],
        ["click", "--x", "0", "--y", "0"],
        ["key", "--key", "enter"],
    ]
    code, _, err = run("run", "--steps", json.dumps(steps))
    assert code == 1 and err.startswith("ERROR step 3 (click) failed: (0, 0) is a failsafe corner")
    assert not any(c[0] in ("click", "press") for c in fake_gui.calls)
    _, out, _ = run("--json", "run", "--steps", json.dumps(steps))
    data = json.loads(out)
    assert data["done"] == 2 and data["failed"] == 3 and data["results"][1]["result"] == {"chars": 7}
    assert "hunter2" not in ad.log_files()[0][0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "steps,message",
    [
        ([["run", "--steps", "[]"]], "step 1: run can't be a step of run"),
        ([["mcp"]], "step 1: mcp can't be a step of run"),
        ([["key", "--key", "enter"], ["bogus"]], "step 2 (bogus): bad arguments"),
        ([["key", "--help"]], "step 1 (key): --help/--version is not an action"),
        ([], "non-empty JSON list"),
        ([{"x": 1}], "step 1 must be a list"),
    ],
)
def test_run_checks_every_step_before_acting(run, fake_gui, steps, message):
    code, _, err = run("run", "--steps", json.dumps(steps))
    assert code == 2 and message in err
    assert fake_gui.calls == []


def test_run_rejects_bad_json_without_logging_it(run, fake_gui):
    code, _, err = run("run", "--steps", '[["type","--text","hunter2"]')
    assert code == 2 and "JSON" in err
    entry = ad.read_log_entries(1)[0]
    assert "steps" not in entry["args"] and entry["args"]["steps_chars"] > 0
    assert "hunter2" not in ad.log_files()[0][0].read_text(encoding="utf-8")


def test_run_passes_instant_and_backend_on(run, fake_gui, sleeps):
    code, _, _ = run("--instant", "run", "--steps", '[["click","--x","500","--y","300"]]')
    assert code == 0 and [c[0] for c in fake_gui.calls] == ["click"] and sleeps == []


def test_run_turns_a_crashing_step_into_a_failure(run, fake_gui, monkeypatch):
    def crash(*args, **kwargs):
        raise ValueError("boom")

    fake_gui.press = crash
    code, _, err = run("run", "--steps", '[["key","--key","enter"]]')
    assert code == 1 and "step 1 (key) failed: ValueError: boom" in err


def test_run_reads_steps_from_a_file(run, fake_gui, tmp_path):
    steps = tmp_path / "steps.json"
    steps.write_text('["key --key enter", "key --key tab"]', encoding="utf-8")
    code, out, _ = run("run", "--file", str(steps))
    assert code == 0 and "steps=2 done=2" in out
    code, _, err = run("run", "--file", str(tmp_path / "missing.json"))
    assert code == 1 and "cannot read --file" in err


def test_run_from_stdin_is_refused_in_quiet_mode(fake_gui):
    code, payload = ad.execute(["run", "--file", "-"], quiet=True)
    assert code == 1 and "MCP transport" in payload["error"]


def test_run_failsafe_keeps_its_exit_code(run, fake_gui):
    def corner(*args, **kwargs):
        raise fake_gui.FailSafeException("corner")

    fake_gui.press = corner
    code, _, err = run("run", "--steps", '[["key","--key","enter"]]')
    assert code == 3 and "failsafe" in err


def test_run_keeps_devices_for_its_steps(run, monkeypatch):
    seen = []
    real = ad.execute

    def spy(argv=None, quiet=False):
        seen.append(ad._DEVICE_CACHE is not None)
        return real(argv, quiet)

    monkeypatch.setattr(ad, "execute", spy)
    run("run", "--steps", '[["screenshots","dir"]]')  # touches nothing on the desktop
    assert seen == [False, True] and ad._DEVICE_CACHE is None  # the run itself, then its step


# --- regressions from review -------------------------------------------------------------


def test_wait_stable_compares_two_captures_even_with_no_time(run, fake_gui, frames):
    code, out, _ = run("wait", "--stable", "--timeout", "0")
    assert code == 0 and "polls=2 " in out


def test_wait_never_sleeps_past_its_deadline(run, fake_gui, monkeypatch, sleeps):
    count = [0]

    def capture(backend, monitor="all", cursor=False):
        count[0] += 1
        return ad.Capture(frame((0, 0, 900, 500)) if count[0] % 2 else frame(), 0, 0, 1.0, "all")

    monkeypatch.setattr(ad, "capture_screen", capture)
    code, _, _ = run("wait", "--stable", "--timeout", "5", "--interval", "600")
    assert code == 1 and sum(sleeps) == 5


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_wait_rejects_odd_numbers(run, value):
    with pytest.raises(SystemExit) as info:
        run("wait", "--stable", "--timeout", value)
    assert info.value.code == 2


def test_shot_survives_any_capture_failure(run, fake_gui, monkeypatch):
    def hangs(backend, monitor="all", cursor=False):
        raise ad.subprocess.TimeoutExpired("gnome-screenshot", 30)

    monkeypatch.setattr(ad, "capture_screen", hangs)
    code, out, _ = run("click", "--x", "500", "--y", "300", "--shot")
    assert code == 0 and " shot=error shot_error=" in out
    monkeypatch.setenv("AI_DESKTOP_SHOT_DELAY", "inf")
    assert ad.shot_settle() == 0.4


def test_run_reports_an_unbalanced_quote(run, fake_gui):
    code, _, err = run("run", "--steps", json.dumps(["type --text it's done"]))
    assert code == 2 and "step 1: cannot split" in err and fake_gui.calls == []


def test_run_keeps_unquoted_text_out_of_its_error(run, fake_gui):
    code, _, err = run("run", "--steps", json.dumps(["type --text my password is hunter2"]))
    assert code == 2 and "unrecognized arguments" in err and "hunter2" not in err
    assert "hunter2" not in ad.log_files()[0][0].read_text(encoding="utf-8")


def test_run_prints_copied_text_in_text_mode(run, fake_gui, monkeypatch):
    monkeypatch.setattr(ad, "get_clipboard", lambda: "copied words")
    code, out, _ = run("run", "--steps", '[["clipboard","get"]]')
    assert code == 0 and out.splitlines() == ["OK run steps=1 done=1", "copied words"]
    assert "copied words" not in ad.log_files()[0][0].read_text(encoding="utf-8")


def test_split_step_keeps_windows_paths(monkeypatch):
    assert ad.split_step('screenshot -o "a b.png"', 1) == ["screenshot", "-o", "a b.png"]
    monkeypatch.setattr(ad.sys, "platform", "win32")
    assert ad.split_step('screenshot -o C:\\tmp\\a.png --monitor "1"', 1) == [
        "screenshot", "-o", "C:\\tmp\\a.png", "--monitor", "1",
    ]
