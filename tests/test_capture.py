import sys

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
    monkeypatch.setattr(ad, "grab_linux", lambda: FakeGui.screenshot().convert("RGB"))
    monkeypatch.setattr(ad, "load_pyautogui", lambda: FakeGui)
    img, scale = ad.capture_screen(FakeBackend())
    assert img.size == (1920, 1080) and img.mode == "RGB"
    assert scale == 2.0


def test_find_screenshot_tool(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda n: "/usr/bin/grim" if n == "grim" else None)
    assert ad.find_screenshot_tool() == ["grim"]
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    assert ad.find_screenshot_tool() is None


def test_grab_linux_on_x11_falls_back_to_pyautogui(monkeypatch):
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    monkeypatch.setattr(ad, "load_pyautogui", lambda: FakeGui)
    img = ad.grab_linux()
    assert img.mode == "RGB" and img.size == (3840, 2160)


def test_grab_linux_on_wayland_needs_a_tool(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    with pytest.raises(ad.CliError, match="screenshot tool"):
        ad.grab_linux()
