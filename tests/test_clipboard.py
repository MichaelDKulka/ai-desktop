"""The clipboard layer: tool choice per desktop, and copy never losing the user's clipboard."""
import subprocess

import pytest

import ai_desktop as ad


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(ad.sys, "platform", "linux")
    monkeypatch.setattr(ad.shutil, "which", lambda name: f"/usr/bin/{name}")
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_gnome_wayland_prefers_xclip(linux):
    linux.setenv("DISPLAY", ":0")
    linux.setenv("WAYLAND_DISPLAY", "wayland-0")
    linux.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert ad._clipboard_tools()[0][0] == "xclip"


@pytest.mark.parametrize("desktop", ["KDE", "sway", "Hyprland", ""])
def test_other_wayland_desktops_prefer_wl_clipboard_with_a_text_type(linux, desktop):
    linux.setenv("DISPLAY", ":0")
    linux.setenv("WAYLAND_DISPLAY", "wayland-1")
    linux.setenv("XDG_CURRENT_DESKTOP", desktop)
    set_argv, get_argv = ad._clipboard_tools()
    assert set_argv == ["wl-copy", "--type", "text/plain"]
    assert get_argv == ["wl-paste", "--no-newline", "--type", "text"]


def test_x11_uses_xclip_and_nothing_found_is_an_error(linux):
    linux.setenv("DISPLAY", ":0")
    assert ad._clipboard_tools()[0][0] == "xclip"
    linux.setattr(ad.shutil, "which", lambda name: None)
    with pytest.raises(ad.CliError, match="no clipboard tool found"):
        ad._clipboard_tools()


class Clipboard:
    """A fake clipboard; `copies` is what the copy shortcut puts there (None: nothing)."""

    def __init__(self, monkeypatch, text="user's text", copies=None, hotkey_error=None):
        self.text = text
        self.copies = copies
        self.hotkey_error = hotkey_error
        monkeypatch.setattr(ad, "get_clipboard", lambda: self.text)
        monkeypatch.setattr(ad, "set_clipboard", self.set)
        monkeypatch.setattr(ad.time, "sleep", lambda s: None)

    def set(self, text):
        self.text = text

    def hotkey(self, keys):
        if self.hotkey_error:
            raise self.hotkey_error
        if self.copies is not None:
            self.text = self.copies


@pytest.fixture
def backend(fake_gui, monkeypatch):
    b = ad.get_backend("pyautogui")
    monkeypatch.setattr(ad, "get_backend", lambda name: b)
    return b


def test_copy_returns_the_new_text(run, backend, monkeypatch):
    clip = Clipboard(monkeypatch, copies="selected words")
    monkeypatch.setattr(backend, "hotkey", clip.hotkey)
    code, out, _ = run("copy")
    assert code == 0 and out.splitlines() == ["OK copy chars=14", "selected words"]


def test_nothing_copied_restores_the_clipboard(run, backend, monkeypatch):
    clip = Clipboard(monkeypatch, copies=None)
    clock = iter(range(0, 1000))
    monkeypatch.setattr(ad.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(backend, "hotkey", clip.hotkey)
    code, _, err = run("copy")
    assert code == 1 and "nothing was copied" in err and clip.text == "user's text"


def test_a_failing_key_press_restores_the_clipboard(run, backend, monkeypatch):
    clip = Clipboard(monkeypatch, hotkey_error=OSError("no /dev/uinput"))
    monkeypatch.setattr(backend, "hotkey", clip.hotkey)
    code, _, err = run("copy")
    assert code == 1 and "no /dev/uinput" in err
    assert clip.text == "user's text"  # not the ai-desktop-copy-... sentinel


def test_copy_does_not_touch_the_clipboard_when_the_keyboard_is_missing(run, backend, monkeypatch):
    Clipboard(monkeypatch, copies="x")  # fakes the clipboard reads
    touched = []
    monkeypatch.setattr(ad, "set_clipboard", lambda text: touched.append(text))

    def no_keyboard(kind):
        raise ad.CliError("no write access to /dev/uinput")

    monkeypatch.setattr(backend, "prepare", no_keyboard)
    assert run("copy")[0] == 1 and touched == []


def test_clipboard_timeout_is_an_error_line(run, linux, monkeypatch):
    linux.setenv("DISPLAY", ":0")

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 15)

    monkeypatch.setattr(ad.subprocess, "run", hang)
    code, out, err = run("clipboard", "get")
    assert code == 1 and out == "" and err.startswith("ERROR xclip did not finish within 15 s")


def test_windows_powershell_is_started_by_its_full_path(monkeypatch, tmp_path):
    # Windows searches the current folder before System32 for a bare "powershell".
    monkeypatch.setattr(ad.sys, "platform", "win32")
    system = tmp_path / "System32" / "WindowsPowerShell" / "v1.0"
    system.mkdir(parents=True)
    (system / "powershell.exe").write_bytes(b"")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    set_argv, get_argv = ad._clipboard_tools()
    assert set_argv[0] == get_argv[0] == str(system / "powershell.exe")
