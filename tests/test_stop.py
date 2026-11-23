"""The stop switch: `ai-desktop stop` halts every action, also mid-way, until a human resumes."""
import json
import sys

import pytest

import ai_desktop as ad


def stop(run):
    code, out, _ = run("stop", "--reason", "testing")
    assert code == 0 and "stopped=True" in out
    assert json.loads(ad.stop_path().read_text(encoding="utf-8"))["reason"] == "testing"


def stop_after(monkeypatch, sleeps_before_stop):
    """Make the n-th pacing wait create the stop file, as a human would mid-action."""
    count = {"n": 0}

    def fake_sleep(seconds):
        count["n"] += 1
        if count["n"] == sleeps_before_stop:
            ad.stop_path().parent.mkdir(parents=True, exist_ok=True)
            ad.stop_path().write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ad, "_sleep", fake_sleep)


def test_stop_refuses_actions_with_exit_3(run, fake_gui):
    stop(run)
    code, _, err = run("click", "--x", "500", "--y", "300")
    assert code == ad.EXIT_FAILSAFE and "ai-desktop stop" in err and "do not resume" in err
    assert fake_gui.calls == []
    code, out, _ = run("--json", "type", "--text", "hi")
    data = json.loads(out)
    assert code == 3 and data["stopped"] is True and data["code"] == 3
    assert not any(c[0] == "write" for c in fake_gui.calls)


def test_read_only_commands_still_work_while_stopped(run, fake_gui):
    stop(run)
    assert run("monitors")[0] == 0
    assert run("log")[0] == 0
    assert run("position")[0] == 0
    code, out, _ = run("stop")  # stopping twice is fine
    assert code == 0 and "was_stopped=True" in out


@pytest.mark.parametrize("command", [
    ["clipboard", "set", "--text", "x"], ["copy"], ["paste", "--text", "x"], ["focus", "--title", "x"],
    ["launch-chrome"], ["run", "--steps", '[["key","--key","enter"]]'], ["key", "--key", "enter"],
    ["hotkey", "--keys", "ctrl+a"], ["scroll"], ["drag", "--x1", "1", "--y1", "1", "--x2", "5", "--y2", "5"],
])
def test_every_action_is_refused(run, fake_gui, command, monkeypatch):
    monkeypatch.setattr(ad, "set_clipboard", lambda text: pytest.fail("clipboard touched"))
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: pytest.fail("chrome launched"))
    stop(run)
    assert run(*command)[0] == 3
    assert fake_gui.calls == []


def test_resume_needs_a_terminal(run, fake_gui, monkeypatch):
    stop(run)
    monkeypatch.setattr(sys, "stdin", type("Pipe", (), {"isatty": lambda self: False})())
    code, _, err = run("resume")
    assert code == 1 and "must be run by the human in a terminal" in err and ad.is_stopped()
    monkeypatch.setattr(sys, "stdin", type("Tty", (), {"isatty": lambda self: True})())
    code, out, _ = run("resume")
    assert code == 0 and "was_stopped=True" in out and not ad.is_stopped()
    assert run("click", "--x", "500", "--y", "300")[0] == 0
    assert run("resume")[0] == 0  # nothing to resume is not an error


def test_resume_is_refused_in_quiet_mode_and_inside_run(fake_gui, monkeypatch, run):
    stop(run)
    monkeypatch.setattr(sys, "stdin", type("Tty", (), {"isatty": lambda self: True})())
    code, payload = ad.execute(["resume"], quiet=True)  # how the MCP server runs commands
    assert code == 1 and ad.is_stopped()
    code, _, err = run("run", "--steps", '[["resume"]]')
    assert code == 3  # run itself is an action, refused while stopped
    ad.stop_path().unlink()
    code, _, err = run("run", "--steps", '[["resume"]]')
    assert code == 2 and "resume can't be a step" in err


def test_stop_aborts_typing_between_characters(run, fake_gui, monkeypatch):
    stop_after(monkeypatch, 2)  # after the second character's pause
    code, _, err = run("type", "--text", "hello")
    assert code == 3 and "stopped" in err
    typed = [c[1][0] for c in fake_gui.calls if c[0] == "write"]
    assert typed == ["h", "e", "l"]  # the third pause found the stop file


def test_stop_aborts_a_glide_before_the_click(run, fake_gui, monkeypatch):
    stop_after(monkeypatch, 3)
    code, _, _ = run("click", "--x", "900", "--y", "500")
    assert code == 3
    assert any(c[0] == "moveTo" for c in fake_gui.calls)
    assert not any(c[0] == "click" for c in fake_gui.calls)


def test_run_stops_before_the_next_step(run, fake_gui, monkeypatch):
    original = ad.PyAutoGuiBackend.press

    def press_then_stop(self, key):
        original(self, key)
        ad.stop_path().parent.mkdir(parents=True, exist_ok=True)
        ad.stop_path().write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ad.PyAutoGuiBackend, "press", press_then_stop)
    code, _, err = run("run", "--steps", '[["key","--key","enter"],["key","--key","tab"]]')
    assert code == 3 and "step 2 (key) failed: stopped" in err
    assert [c[1] for c in fake_gui.calls if c[0] == "press"] == [("enter",)]


class FakeDev:
    def __init__(self, *args, **kwargs):
        self.events = []
        self.last_abs = None

    def emit(self, ev_type, code, value, sync=True):
        self.events.append((ev_type, code, value))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_stop_mid_drag_releases_the_button(monkeypatch):
    dev = FakeDev()
    monkeypatch.setattr(ad, "_UInputDevice", lambda *a, **k: dev)
    sleeps = {"n": 0}

    def sleep(seconds):
        sleeps["n"] += 1
        if sleeps["n"] == 5:
            ad.stop_path().parent.mkdir(parents=True, exist_ok=True)
            ad.stop_path().write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ad.time, "sleep", sleep)
    monkeypatch.setattr(ad, "HUMAN_INPUT", False)
    backend = ad.UInputBackend.__new__(ad.UInputBackend)
    backend._size = (1920, 1080)
    with pytest.raises(ad.CliError) as info:
        backend.drag(100, 100, 900, 100, "left", 0.5)
    assert info.value.code == 3
    assert dev.events[-1] == (ad.EV_KEY, ad.BTN_LEFT, 0)  # released
    moved = [v for t, c, v in dev.events if t == ad.EV_ABS and c == ad.ABS_X]
    assert moved[-1] < 900  # it did not finish the drag


def test_doctor_reports_the_stop(run):
    assert ad._check_stop_switch()[0] == "ok"
    stop(run)
    state, detail, hint = ad._check_stop_switch()
    assert state == "warn" and "STOPPED" in detail and "ai-desktop resume" in hint
