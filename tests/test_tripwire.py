"""The test suite itself can't reach the real desktop."""
import os
import sys

import pytest

import ai_desktop as ad


def test_real_input_device_is_blocked():
    with pytest.raises(AssertionError, match="/dev/uinput"):
        ad.os.open("/dev/uinput", os.O_WRONLY)


@pytest.mark.parametrize("program", ["xclip", "gnome-screenshot", "/usr/bin/gtk-launch", "tesseract"])
def test_desktop_programs_are_blocked(program):
    with pytest.raises(AssertionError, match="real"):
        ad.subprocess.run([program, "--version"])
    with pytest.raises(AssertionError, match="real"):
        ad.subprocess.Popen([program])


def test_real_screen_grab_and_pyautogui_are_blocked():
    with pytest.raises(AssertionError, match="real screen"):
        ad.ImageGrab.grab()
    with pytest.raises(ad.CliError, match="cannot load pyautogui"):
        ad.load_pyautogui()


def test_ordinary_files_and_programs_still_work(tmp_path):
    fd = ad.os.open(tmp_path / "x", os.O_WRONLY | os.O_CREAT)
    os.close(fd)
    assert ad.subprocess.run([sys.executable, "-c", "pass"]).returncode == 0
