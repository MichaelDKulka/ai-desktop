import json

import pytest

import ai_desktop as ad


def raiser(exc):
    def _raise(args):
        raise exc

    return _raise


def test_status_text_quotes_and_drops_lists(capsys):
    ad.status("x", a=1, b="two words", c="", d=[1, 2], e={"k": 1})
    assert capsys.readouterr().out == 'OK x a=1 b="two words" c=""\n'


def test_status_json_keeps_types(capsys, monkeypatch):
    monkeypatch.setattr(ad, "JSON_OUTPUT", True)
    ad.status("x", a=1, f=1.5, d=[1, 2], p=ad.Path("x"))
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "command": "x", "a": 1, "f": 1.5, "d": [1, 2], "p": "x",
    }


def test_print_text_text_mode(capsys):
    ad.print_text("copy", "héllo\nworld")
    assert capsys.readouterr().out == "OK copy chars=11\nhéllo\nworld\n"


def test_print_text_json_mode(capsys, monkeypatch):
    monkeypatch.setattr(ad, "JSON_OUTPUT", True)
    ad.print_text("copy", "a b")
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "command": "copy", "chars": 3, "text": "a b",
    }


def test_fail_text_mode(capsys):
    assert ad.fail("boom", 1) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "ERROR boom\n"


def test_fail_json_mode(capsys, monkeypatch):
    monkeypatch.setattr(ad, "JSON_OUTPUT", True)
    monkeypatch.setattr(ad, "CURRENT_COMMAND", "click")
    assert ad.fail("boom", 3, extra="x") == 3
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False, "command": "click", "error": "boom", "code": 3, "extra": "x",
    }


def test_cli_error_exits_1(run, monkeypatch):
    monkeypatch.setattr(ad, "cmd_position", raiser(ad.CliError("bad thing")))
    code, out, err = run("position")
    assert code == 1 and out == "" and err == "ERROR bad thing\n"


def test_cli_error_fields_appear_in_json(run, monkeypatch):
    monkeypatch.setattr(ad, "cmd_position", raiser(ad.CliError("bad", checks=[1])))
    code, out, err = run("--json", "position")
    assert code == 1 and err == ""
    data = json.loads(out)
    assert data["ok"] is False and data["command"] == "position" and data["checks"] == [1]


def test_keyboard_interrupt_exits_130(run, monkeypatch):
    monkeypatch.setattr(ad, "cmd_position", raiser(KeyboardInterrupt()))
    code, _, err = run("position")
    assert code == 130 and err == "ERROR interrupted\n"


def test_failsafe_exits_3(run, monkeypatch, fake_gui):
    monkeypatch.setattr(ad, "cmd_position", raiser(fake_gui.FailSafeException("corner")))
    code, out, _ = run("position", "--json")
    data = json.loads(out)
    assert code == 3 and data["code"] == 3 and "failsafe" in data["error"]


def test_unexpected_exception_propagates(run, monkeypatch):
    monkeypatch.setattr(ad, "cmd_position", raiser(ValueError("x")))
    with pytest.raises(ValueError):
        run("position")


def test_json_env_var(run, monkeypatch, sample_png):
    monkeypatch.setenv("AI_DESKTOP_JSON", "1")
    _, out, _ = run("grid", "--input", str(sample_png))
    assert json.loads(out)["ok"] is True


def test_version_flag(run):
    with pytest.raises(SystemExit) as info:
        run("--version")
    assert info.value.code == 0


def test_backend_flag_before_or_after_subcommand(run, monkeypatch):
    seen = {}
    monkeypatch.setattr(ad, "cmd_position", lambda args: seen.update(backend=args.backend))
    run("position", "--backend", "uinput")
    assert seen["backend"] == "uinput"
    run("--backend", "pyautogui", "position")
    assert seen["backend"] == "pyautogui"
    run("position")
    assert seen["backend"] == "auto"
    monkeypatch.setenv("AI_DESKTOP_BACKEND", "uinput")
    run("position")
    assert seen["backend"] == "uinput"
