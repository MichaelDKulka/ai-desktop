import sys

import pytest

import ai_desktop as ad


def test_auto_picks_pyautogui_without_wayland(fake_gui):
    assert ad.get_backend("auto").name == "pyautogui"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uinput is Linux only")
def test_auto_picks_uinput_on_wayland(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert ad.get_backend("auto").name == "uinput"


def test_unknown_backend():
    with pytest.raises(ad.CliError, match="unknown backend"):
        ad.get_backend("bogus")


def test_bounds_and_failsafe_corners(fake_gui):
    backend = ad.get_backend("pyautogui")
    for x, y in ((0, 0), (1919, 0), (0, 1079), (1919, 1079)):
        with pytest.raises(ad.CliError, match="failsafe corner"):
            backend.check_point(x, y)
    with pytest.raises(ad.CliError, match="off screen"):
        backend.check_point(1920, 5)
    with pytest.raises(ad.CliError, match="off screen"):
        backend.check_point(-1, 5)
    backend.check_point(1, 1)
    backend.check_point(1918, 1079)


def test_scroll_units_per_platform(fake_gui, monkeypatch):
    backend = ad.get_backend("pyautogui")
    monkeypatch.setattr(sys, "platform", "win32")
    backend.scroll(3)
    assert fake_gui.calls[-1] == ("scroll", (360,), {"x": None, "y": None})  # 3 notches x WHEEL_DELTA
    monkeypatch.setattr(sys, "platform", "darwin")
    backend.scroll(-2, 5, 6)
    assert fake_gui.calls[-1] == ("scroll", (-2,), {"x": 5, "y": 6})


def test_meta_key_maps_per_platform(fake_gui, monkeypatch):
    backend = ad.get_backend("pyautogui")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert backend.resolve_key("cmd") == "command"
    monkeypatch.setattr(sys, "platform", "win32")
    assert backend.resolve_key("cmd") == "win"
    assert backend.resolve_key("Return") == "enter"
    assert backend.resolve_key("a") == "a"
    with pytest.raises(ad.CliError, match="unknown key"):
        backend.resolve_key("bogus")


def test_click_and_hotkey_cli(run, fake_gui):
    code, out, _ = run("click", "--x", "500", "--y", "300", "--double")
    assert code == 0 and out == "OK click x=500 y=300 button=left clicks=2\n"
    assert fake_gui.calls[-1] == (
        "click", (), {"x": 500, "y": 300, "button": "left", "clicks": 2, "interval": 0.1},
    )
    code, _, _ = run("hotkey", "--keys", "ctrl+shift+t")
    assert code == 0 and fake_gui.calls[-1][1] == ("ctrl", "shift", "t")
    code, _, err = run("hotkey", "--keys", "ctrl+")
    assert code == 1 and "bad hotkey" in err


def test_corner_click_is_rejected_before_acting(run, fake_gui):
    code, _, err = run("click", "--x", "0", "--y", "0")
    assert code == 1 and "failsafe corner" in err
    assert not any(call[0] == "click" for call in fake_gui.calls)


def test_type_rejects_non_ascii(run, fake_gui):
    code, _, err = run("type", "--text", "héllo")
    assert code == 1 and "ASCII" in err
    assert not any(call[0] == "write" for call in fake_gui.calls)
    code, _, _ = run("type", "--text", "Hi!")
    assert code == 0 and fake_gui.calls[-1] == ("write", ("Hi!",), {"interval": 0.02})


def test_failsafe_during_an_action(run, fake_gui):
    def boom(*args, **kwargs):
        raise fake_gui.FailSafeException("corner")

    fake_gui.write = boom
    code, _, err = run("type", "--text", "slow")
    assert code == 3 and "failsafe" in err


def test_position(run, fake_gui):
    _, out, _ = run("position")
    assert out == "OK position x=10 y=20 width=1920 height=1080 backend=pyautogui\n"


def test_missing_pyautogui_is_a_clean_error(run, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    code, _, err = run("--backend", "pyautogui", "position")
    assert code == 1 and "cannot load pyautogui" in err
