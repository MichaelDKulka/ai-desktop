"""Shared fixtures. Nothing here opens a display, /dev/uinput or the real clipboard."""
from __future__ import annotations

import os
import sys
import types

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
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_desktop, "JSON_OUTPUT", False)
    monkeypatch.setattr(ai_desktop, "CURRENT_COMMAND", None)
    monkeypatch.setattr(ai_desktop, "LOG_VIA", "cli")
    monkeypatch.setattr(ai_desktop, "detect_monitors", lambda backend: ([FAKE_MONITOR], "fake"))


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
    for name in ("moveTo", "mouseDown", "mouseUp", "click", "write", "press", "hotkey", "scroll"):
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
