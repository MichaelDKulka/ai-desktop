import os
import sys

import pytest

import ai_desktop as ad


def test_linux_tries_names_in_order(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad.shutil, "which", lambda n: "/usr/bin/google-chrome" if n == "google-chrome" else None)
    assert ad.find_chrome_executable() == "/usr/bin/google-chrome"


def test_not_found_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    with pytest.raises(ad.CliError, match="not found"):
        ad.find_chrome_executable()


def test_windows_probes_program_files(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(ad, "_chrome_from_registry", lambda: [])
    monkeypatch.setattr(ad.shutil, "which", lambda n: None)
    assert ad.find_chrome_executable() == str(exe)


def test_host_env_strips_snap_injection(monkeypatch):
    monkeypatch.setenv("SNAP", "/snap/code/1")
    monkeypatch.setenv("SNAP_USER_DATA", "/home/u/snap/code/1")
    monkeypatch.setenv("GTK_PATH", "/snap/code/1/usr/lib/gtk")
    monkeypatch.setenv("KEEP_ME", "/usr/share/x")
    monkeypatch.setenv("XDG_DATA_DIRS", os.pathsep.join(["/home/u/snap/code/1/.local/share", "/usr/share"]))
    env = ad.host_env()
    assert "SNAP" not in env and "SNAP_USER_DATA" not in env and "GTK_PATH" not in env
    assert env["KEEP_ME"] == "/usr/share/x"
    assert env["XDG_DATA_DIRS"] == "/usr/share"


def test_host_env_is_a_copy_when_not_in_a_snap(monkeypatch):
    monkeypatch.setenv("KEEP_ME", "1")
    env = ad.host_env()
    assert env["KEEP_ME"] == "1" and env is not os.environ
