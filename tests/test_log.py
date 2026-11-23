import json
import os
import time

import ai_desktop as ad


def entries(limit=100):
    return ad.read_log_entries(limit)


def test_command_writes_one_entry(run, fake_gui):
    code, _, _ = run("click", "--x", "500", "--y", "300")
    assert code == 0
    logs = ad.log_files()
    assert len(logs) == 1 and ad.LOG_NAME_RE.match(logs[0][0].name)
    [entry] = entries()
    assert entry["command"] == "click" and entry["ok"] is True and entry["code"] == 0
    assert entry["via"] == "cli" and entry["backend"] == "auto"
    assert entry["args"] == {"x": 500, "y": 300, "button": "left", "double": False, "monitor": None}
    assert entry["result"] == {"x": 500, "y": 300, "button": "left", "clicks": 1}
    assert isinstance(entry["ms"], int) and entry["ts"][:4].isdigit()


def test_typed_text_is_redacted(run, fake_gui):
    run("type", "--text", "hunter2")
    [entry] = entries()
    assert "text" not in entry["args"] and entry["args"]["text_chars"] == 7
    raw = ad.log_files()[0][0].read_text(encoding="utf-8")
    assert "hunter2" not in raw


def test_text_is_kept_when_opted_in(run, fake_gui, monkeypatch):
    monkeypatch.setenv("AI_DESKTOP_LOG_TEXT", "1")
    run("type", "--text", "hello")
    [entry] = entries()
    assert entry["args"]["text"] == "hello"


def test_copied_text_is_dropped(run, monkeypatch):
    monkeypatch.setattr(ad, "cmd_copy", lambda args: ad.print_text("copy", "secret stuff"))
    code, out, _ = run("copy")
    assert code == 0 and "secret stuff" in out
    [entry] = entries()
    assert entry["result"] == {"chars": 12}
    assert "secret" not in json.dumps(entry)


def test_failed_command_is_logged(run, fake_gui):
    code, _, _ = run("click", "--x", "0", "--y", "0")
    assert code == 1
    [entry] = entries()
    assert entry["ok"] is False and entry["code"] == 1
    assert "failsafe corner" in entry["result"]["error"]


def test_log_can_be_disabled(run, fake_gui, monkeypatch):
    monkeypatch.setenv("AI_DESKTOP_LOG", "0")
    run("position")
    assert ad.log_files() == []


def test_log_command_itself_is_not_logged(run):
    code, out, _ = run("log")
    assert code == 0 and out.startswith("OK log ") and "count=0" in out
    assert ad.log_files() == []


def test_log_command_lists_newest_first(run, fake_gui):
    run("position")
    run("move", "--x", "5", "--y", "6")
    code, out, _ = run("log", "--limit", "1")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("OK log ") and "count=1" in lines[0]
    assert " cli move ok " in lines[1] and "x=5 y=6" in lines[1]
    _, out, _ = run("--json", "log")
    data = json.loads(out)
    assert [e["command"] for e in data["entries"]] == ["move", "position"]


def test_error_entry_format():
    entry = {
        "ts": "2026-10-12T13:05:09.123", "via": "cli", "command": "type",
        "ok": False, "code": 1, "ms": 3, "result": {"error": "cannot type"},
    }
    assert ad.format_log_entry(entry) == "13:05:09 cli type ERROR(1) 3ms cannot type"


def test_prune_logs_by_age():
    folder = ad.log_dir()
    folder.mkdir(parents=True)

    def make(name, age_days):
        path = folder / name
        path.write_text("{}\n")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    old = make("actions-20260101.jsonl", 40)
    new = make("actions-20261011.jsonl", 1)
    stray = make("notes.jsonl", 40)
    assert ad.prune_logs(30) == [old]
    assert not old.exists() and new.exists() and stray.exists()
    assert ad.prune_logs(None) == []


def test_clean_all_removes_logs_too(run, fake_gui):
    run("position")
    assert ad.log_files()
    _, out, _ = run("screenshots", "clean", "--all")
    assert "logs_deleted=1" in out
    # Only the clean command's own entry is left.
    assert [e["command"] for e in entries()] == ["screenshots"]


def test_logging_failure_does_not_break_the_command(run, fake_gui, monkeypatch):
    def boom():
        raise RuntimeError("disk")

    monkeypatch.setattr(ad, "log_dir", boom)
    code, out, _ = run("position")
    assert code == 0 and out.startswith("OK position")


def test_execute_quiet_returns_payload_without_printing(fake_gui, capsys):
    code, payload = ad.execute(["position"], quiet=True)
    assert code == 0 and payload["ok"] is True and payload["x"] == 10
    code, payload = ad.execute(["click", "--x", "0", "--y", "0"], quiet=True)
    assert code == 1 and payload["ok"] is False and "failsafe corner" in payload["error"]
    code, payload = ad.execute(["bogus"], quiet=True)
    assert code == 2 and payload["ok"] is False and payload["error"].startswith("bad arguments")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert ad.QUIET is False and ad.LAST_PAYLOAD["code"] == 2
    [entry] = [e for e in entries() if e["command"] == "position"]
    assert entry["via"] == "cli"
