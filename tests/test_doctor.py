import json

import pytest
from PIL import Image

import ai_desktop as ad


def test_run_check_turns_exceptions_into_rows():
    def boom():
        raise RuntimeError("nope")

    assert ad.run_check("x", boom) == ad.Check("x", "fail", "nope", "")
    assert ad.run_check("x", boom, soft=True).state == "warn"
    assert ad.run_check("y", lambda: ("ok", "fine")) == ad.Check("y", "ok", "fine", "")
    assert ad.run_check("z", lambda: ("warn", "meh", "do this")).hint == "do this"


@pytest.fixture
def quiet_doctor(monkeypatch, fake_gui):
    """Doctor with every desktop-touching check replaced by a harmless stand-in."""
    monkeypatch.setattr(ad, "capture_screen", lambda backend: (Image.new("RGB", (1920, 1080)), 1.0))
    monkeypatch.setattr(ad, "_clipboard_tools", lambda: (["fakeclip"], ["fakeclip"]))
    monkeypatch.setattr(ad, "find_chrome_executable", lambda: "/fake/chrome")
    monkeypatch.setattr(ad, "mac_permission", lambda framework, symbol: True)
    monkeypatch.setattr(ad, "windows_dpi_scale", lambda: 1.0)


def test_doctor_json_passes(run, quiet_doctor):
    code, out, err = run("--json", "doctor", "--backend", "pyautogui")
    data = json.loads(out)
    assert code == 0 and err == ""
    assert data["ok"] is True and data["failed"] == 0 and data["version"] == ad.__version__
    names = {check["name"] for check in data["checks"]}
    assert {"python", "pillow", "pyautogui", "backend", "screen-size", "screenshot", "clipboard", "chrome", "store"} <= names
    assert all(check["state"] in ("ok", "warn") for check in data["checks"])
    assert (ad.store_dir()).is_dir()


def test_doctor_text_rows_then_status_line(run, quiet_doctor):
    code, out, _ = run("doctor", "--backend", "pyautogui")
    lines = out.splitlines()
    assert code == 0
    assert any(line.startswith("ok   python") for line in lines)
    assert lines[-1].startswith("OK doctor version=") and "failed=0" in lines[-1]


def test_doctor_failure_exits_1_with_rows(run, quiet_doctor, monkeypatch):
    monkeypatch.setattr(ad, "_check_store", lambda: ("fail", "broken", "fix it"))
    code, out, _ = run("--json", "doctor", "--backend", "pyautogui")
    data = json.loads(out)
    assert code == 1 and data["ok"] is False and data["failed"] == 1
    store = next(check for check in data["checks"] if check["name"] == "store")
    assert store["state"] == "fail" and store["hint"] == "fix it"
