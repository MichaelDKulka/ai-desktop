"""Shared fixtures. Nothing here opens a display, /dev/uinput or the real clipboard."""
from __future__ import annotations

import os
import random
import subprocess
import sys
import types
import urllib.request

import pytest
from PIL import Image

import ai_desktop


REAL_DETECT_MONITORS = ai_desktop.detect_monitors
FAKE_MONITOR = ai_desktop.Monitor(0, "fake", 0, 0, 1920, 1080, True, 1.0)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Keep every test off the real desktop and the real screenshot store.

    Monitor detection is replaced by one 1920x1080 primary (fake_gui's size), so no
    test spawns xrandr or queries the real display layout.
    """
    monkeypatch.setenv("AI_DESKTOP_HOME", str(tmp_path / "home"))
    for var in list(os.environ):
        if var.startswith(("AI_DESKTOP_", "SNAP")) and var != "AI_DESKTOP_HOME":
            monkeypatch.delenv(var)
    monkeypatch.setenv("AI_DESKTOP_SERVICE", "off")  # tests run commands in-process
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(var, raising=False)
    # Chrome's profile folders: never the real ones (they hold the user's account names).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setattr(ai_desktop, "JSON_OUTPUT", False)
    monkeypatch.setattr(ai_desktop, "CURRENT_COMMAND", None)
    monkeypatch.setattr(ai_desktop, "LOG_VIA", "cli")
    monkeypatch.setattr(ai_desktop, "detect_monitors", lambda backend: ([FAKE_MONITOR], "fake"))
    monkeypatch.setattr(ai_desktop, "RNG", random.Random(0))
    monkeypatch.setattr(ai_desktop, "HUMAN_INPUT", True)
    monkeypatch.setattr(ai_desktop, "MAX_WIDTH_OVERRIDE", None)
    monkeypatch.setattr(ai_desktop, "_LAST_CAPTURE_TOOL", None)


# Programs that would touch the real desktop, clipboard or browser.
DESKTOP_PROGRAMS = {
    "gnome-screenshot", "grim", "spectacle", "screencapture", "xclip", "xsel", "wl-copy",
    "wl-paste", "pbcopy", "pbpaste", "powershell", "powershell.exe", "xrandr", "gtk-launch",
    "gio", "osascript", "open", "google-chrome", "google-chrome-stable", "chromium",
    "chromium-browser", "xdotool", "ydotool", "tesseract", "gdbus", "busctl",
}


def _program(argv):
    first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
    return os.path.basename(str(first))


@pytest.fixture(autouse=True)
def tripwire(monkeypatch):
    """Fail any test that would reach the real desktop: the uinput device, a desktop
    program, a real screen grab or the real pyautogui. Tests replace these with fakes
    (monkeypatch after this fixture wins), and fake_gui supplies pyautogui."""
    real_open, real_run, real_popen = os.open, subprocess.run, subprocess.Popen

    def guarded_open(path, *args, **kwargs):
        if str(path) == "/dev/uinput":
            raise AssertionError("a test tried to open the real /dev/uinput")
        return real_open(path, *args, **kwargs)

    def guarded_run(argv, *args, **kwargs):
        if _program(argv) in DESKTOP_PROGRAMS:
            raise AssertionError(f"a test tried to run the real {_program(argv)}")
        return real_run(argv, *args, **kwargs)

    def guarded_popen(argv, *args, **kwargs):
        if _program(argv) in DESKTOP_PROGRAMS:
            raise AssertionError(f"a test tried to start the real {_program(argv)}")
        return real_popen(argv, *args, **kwargs)

    def no_grab(*args, **kwargs):
        raise AssertionError("a test tried to grab the real screen")

    monkeypatch.setattr(ai_desktop.os, "open", guarded_open)
    monkeypatch.setattr(ai_desktop.subprocess, "run", guarded_run)
    monkeypatch.setattr(ai_desktop.subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(ai_desktop.ImageGrab, "grab", no_grab)

    def no_network(*args, **kwargs):
        raise AssertionError("a test tried to reach a real server (a local vision model?)")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # import fails unless fake_gui is used


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record the pacing and polling waits instead of sleeping through them.

    A fake clock (`_now`) advances by each recorded wait, so `wait --timeout` loops end.
    """
    calls = []
    clock = [1000.0]

    def fake_sleep(seconds):
        calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(ai_desktop, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_desktop, "_now", lambda: clock[0])
    return calls


@pytest.fixture
def real_detect_monitors():
    return REAL_DETECT_MONITORS


def _recorder(calls, name):
    def record(*args, **kwargs):
        calls.append((name, args, kwargs))

    return record


@pytest.fixture
def fake_gui(monkeypatch):
    """A stand-in pyautogui module that records calls instead of touching the desktop."""
    fake = types.ModuleType("pyautogui")
    fake.calls = []

    class FailSafeException(Exception):
        pass

    fake.FailSafeException = FailSafeException
    fake.FAILSAFE = False
    fake.PAUSE = 0
    fake.__version__ = "0.0-fake"
    fake.size = lambda: (1920, 1080)
    fake.position = lambda: (10, 20)
    fake.isValidKey = lambda key: key in {
        "enter", "tab", "esc", "space", "ctrl", "shift", "alt", "win", "command", "pagedown",
    }
    fake.screenshot = lambda: Image.new("RGB", (1920, 1080), (1, 2, 3))
    for name in ("moveTo", "dragTo", "mouseDown", "mouseUp", "click", "write", "press", "hotkey", "scroll"):
        setattr(fake, name, _recorder(fake.calls, name))
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return fake


@pytest.fixture
def sample_png(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (300, 200), (230, 230, 230)).save(path)
    return path


@pytest.fixture
def run(capsys):
    """Run the CLI in-process: returns (exit_code, stdout, stderr)."""

    def _run(*argv):
        code = ai_desktop.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run
