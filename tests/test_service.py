"""The background service: a real Unix socket and a real server thread, fake desktop."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

import pytest

import ai_desktop as ad

svc = pytest.importorskip("ai_desktop_service")
pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="Unix sockets")


@pytest.fixture
def runtime(monkeypatch):
    # A short folder: a Unix socket path must stay under about 100 bytes on macOS.
    folder = tempfile.mkdtemp(prefix="aid-", dir="/tmp")
    os.chmod(folder, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", folder)
    monkeypatch.setenv("AI_DESKTOP_SERVICE", "on")
    monkeypatch.setattr(svc, "spawn", lambda: spawned.append(1) or True)
    spawned.clear()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


spawned: list = []


@pytest.fixture
def service(runtime, fake_gui):
    """A service on a thread; stopped afterwards."""
    path = svc._private_runtime_dir() / "service.sock"
    server = svc.Service(path, idle=60)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    yield server
    svc.stop()
    thread.join(5)
    assert not thread.is_alive()


def test_an_action_runs_in_the_service(run, service, fake_gui):
    code, out, err = run("click", "--x", "500", "--y", "300", "--instant")
    assert code == 0 and out == "OK click x=500 y=300 button=left clicks=1\n" and err == ""
    assert fake_gui.calls[-1][0] == "click"
    assert service.requests == 1
    info = svc.status()
    assert info["requests"] == 1 and info["pid"] == os.getpid()


def test_errors_and_json_come_back_unchanged(run, service):
    code, out, err = run("type", "--text", "héllo")
    assert code == 1 and out == "" and err.startswith("ERROR cannot type these characters")
    code, out, _ = run("--json", "click", "--x", "5000", "--y", "1")
    assert code == 1 and json.loads(out)["ok"] is False
    assert service.requests == 2


def test_commands_that_gain_nothing_run_here(run, service):
    assert run("position")[0] == 0
    assert run("resume")[0] == 0  # must see this process's own terminal
    assert service.requests == 0


def test_without_a_service_the_command_runs_here_and_one_is_started(run, runtime, fake_gui):
    code, out, _ = run("click", "--x", "500", "--y", "300", "--instant")
    assert code == 0 and out.startswith("OK click") and spawned == [1]


def test_off_means_off(run, runtime, fake_gui, monkeypatch):
    monkeypatch.setenv("AI_DESKTOP_SERVICE", "off")
    assert run("click", "--x", "500", "--y", "300", "--instant")[0] == 0
    assert spawned == []


def test_a_service_running_older_code_is_not_used(run, service, monkeypatch):
    monkeypatch.setattr(svc, "build_id", lambda: "a-newer-build")
    stops = []
    real_stop = svc.stop
    monkeypatch.setattr(svc, "stop", lambda: stops.append(1) or real_stop())
    code, out, _ = run("click", "--x", "500", "--y", "300", "--instant")
    assert code == 0 and out.startswith("OK click") and service.requests == 0
    assert stops == [1]  # told to exit, so the next command starts a fresh one


def test_another_session_is_not_used(run, service, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")  # an ssh login or another seat
    assert run("click", "--x", "500", "--y", "300", "--instant")[0] == 0
    assert service.requests == 0


def test_a_folder_others_can_enter_is_refused(run, service, runtime):
    os.chmod(runtime + "/ai-desktop", 0o755)
    try:
        assert run("click", "--x", "500", "--y", "300", "--instant")[0] == 0
        assert service.requests == 0
    finally:
        os.chmod(runtime + "/ai-desktop", 0o700)


def test_client_settings_and_folder_are_used(run, service, tmp_path, monkeypatch):
    steps = tmp_path / "steps.json"
    steps.write_text('[["key", "--key", "enter"]]', encoding="utf-8")
    other_home = tmp_path / "other-home"
    monkeypatch.setenv("AI_DESKTOP_HOME", str(other_home))
    monkeypatch.chdir(tmp_path)
    code, out, _ = run("run", "--file", "steps.json")  # relative to the client's folder
    assert code == 0 and "steps=1 done=1" in out and service.requests == 1
    assert (other_home / "logs").is_dir()  # the client's AI_DESKTOP_HOME, not the service's
    assert os.environ.get("AI_DESKTOP_SERVICE") == "on"  # the service restored its own settings


def test_the_stop_switch_applies_in_the_service(run, service, fake_gui):
    assert run("stop")[0] == 0
    code, _, err = run("click", "--x", "500", "--y", "300", "--instant")
    assert code == 3 and "ai-desktop stop" in err and service.requests == 1
    assert not any(c[0] == "click" for c in fake_gui.calls)


def test_a_client_that_goes_away_cancels_its_action(service, fake_gui, monkeypatch):
    monkeypatch.setattr(ad, "_sleep", lambda s: time.sleep(0.05))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(svc.socket_path()))
    buffer = bytearray()
    svc._send(sock, {"op": "hello", "build": svc.build_id(), "session": svc.session_key()})
    assert svc._recv(sock, buffer)["ok"]
    svc._send(sock, {"op": "run", "argv": ["type", "--text", "a long sentence to type"], "cwd": "/", "settings": dict(os.environ)})
    time.sleep(0.3)
    sock.close()  # Ctrl+C in the client
    time.sleep(0.5)
    typed = [c for c in fake_gui.calls if c[0] == "write"]
    assert 0 < len(typed) < len("a long sentence to type")


def test_service_command_reports_status(run, service):
    code, out, _ = run("service", "status")
    assert code == 0 and "running=True" in out and "current=True" in out
    code, out, _ = run("service", "stop")
    assert code == 0 and "stopped=True" in out


def test_service_command_without_a_service(run, runtime):
    code, out, _ = run("service", "status")
    assert code == 0 and "running=False" in out
