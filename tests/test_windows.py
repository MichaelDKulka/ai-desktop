import json
import sys
import types

import pytest

import ai_desktop as ad


def win(id, title, app="app", pid=1, x=0, y=0, w=100, h=100, active=False, minimized=False):
    return ad.Win(id, pid, app, title, x, y, w, h, active, minimized)


WINDOWS = [
    win("0x1", "Inbox - Mail", "thunderbird", 10, active=True),
    win("0x2", "Chrome - Google", "google-chrome", 20),
    win("0x3", "chrome", "google-chrome", 20, minimized=True),
]


def test_select_window_rules():
    w, n = ad.select_window(WINDOWS, title="chrome")
    assert (w.id, n) == ("0x3", 2)  # an exact title match beats stacking order
    w, n = ad.select_window(WINDOWS, title="google")
    assert (w.id, n) == ("0x2", 2)  # otherwise the front-most match wins
    w, n = ad.select_window(WINDOWS, app="thunder")
    assert (w.id, n) == ("0x1", 1)
    w, n = ad.select_window(WINDOWS, pid=20)
    assert (w.id, n) == ("0x2", 2)
    w, _ = ad.select_window(WINDOWS, id="0X3")
    assert w.id == "0x3"
    with pytest.raises(ad.CliError, match="no window matches title='nope'"):
        ad.select_window(WINDOWS, title="nope")


def test_windows_command(run, monkeypatch):
    monkeypatch.setattr(ad, "list_windows", lambda: (WINDOWS, False))
    code, out, _ = run("windows")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "OK windows count=3 partial=False"
    assert lines[1] == '0x1 pid=10 0,0 100x100 active thunderbird "Inbox - Mail"'
    assert lines[3] == '0x3 pid=20 0,0 100x100 minimized google-chrome "chrome"'
    _, out, _ = run("--json", "windows")
    data = json.loads(out)
    assert data["windows"][1]["title"] == "Chrome - Google" and "note" not in data


def test_windows_partial_list_carries_the_wayland_note(run, monkeypatch):
    monkeypatch.setattr(ad, "list_windows", lambda: ([], True))
    _, out, _ = run("--json", "windows")
    data = json.loads(out)
    assert data["partial"] is True and "Wayland" in data["note"]


def test_focus_command(run, monkeypatch):
    monkeypatch.setattr(ad, "list_windows", lambda: (WINDOWS, False))
    seen = {}
    monkeypatch.setattr(ad, "activate_window", lambda w: seen.update(id=w.id) or {"activated": True})
    code, out, _ = run("focus", "--title", "inbox")
    assert code == 0 and seen["id"] == "0x1"
    assert out.strip() == 'OK focus id=0x1 pid=10 app=thunderbird title="Inbox - Mail" matches=1 activated=True'
    code, _, err = run("focus")
    assert code == 1 and "give --title" in err
    monkeypatch.setattr(ad, "activate_window", lambda w: {"activated": False})
    code, _, err = run("focus", "--pid", "20")
    assert code == 1 and "did not come to the front" in err


class FakeX11:
    """Stand-in for the libX11 wrapper, serving canned EWMH properties."""

    root = 1
    atoms = {"_NET_WM_STATE_HIDDEN": 101, "_NET_WM_STATE_SKIP_TASKBAR": 102}
    props = {
        (1, "_NET_CLIENT_LIST_STACKING"): [0x10, 0x20, 0x30],  # bottom-to-top
        (1, "_NET_ACTIVE_WINDOW"): [0x30],
        (0x30, "_NET_WM_NAME"): "Front — Editor",
        (0x30, "WM_CLASS"): ["gnome-text-editor", "org.gnome.TextEditor"],
        (0x30, "_NET_WM_PID"): [555],
        (0x30, "_NET_FRAME_EXTENTS"): [0, 0, 37, 0],
        (0x20, "WM_NAME"): "Legacy",
        (0x20, "_NET_WM_STATE"): [101],
        (0x20, "WM_CLASS"): ["xterm"],
        (0x10, "_NET_WM_NAME"): "Dock",
        (0x10, "_NET_WM_STATE"): [102],
    }
    geometries = {0x30: (66, 69, 800, 600, True), 0x20: (0, 0, 100, 100, False), 0x10: (0, 0, 10, 10, True)}

    def __init__(self):
        # Each connection gets its own copy: activation changes the active window, and a
        # shared dict made test_x11_window_listing depend on the order tests ran in.
        self.props = dict(type(self).props)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def atom(self, name):
        return self.atoms.get(name, 999)

    def get_longs(self, window, name):
        value = self.props.get((window, name), [])
        return value if isinstance(value, list) and all(isinstance(v, int) for v in value) else []

    def get_text(self, window, name):
        value = self.props.get((window, name), "")
        return value if isinstance(value, str) else ""

    def get_strings(self, window, name):
        value = self.props.get((window, name), [])
        return value if value and isinstance(value[0], str) else []

    def geometry(self, window):
        return self.geometries[window]

    def send_client_message(self, window, name, data):
        self.sent.append((window, name, data))
        self.props[(1, "_NET_ACTIVE_WINDOW")] = [window]


def test_x11_window_listing(monkeypatch):
    monkeypatch.setattr(ad, "_X11", FakeX11)
    found = ad._windows_x11()
    assert [w.id for w in found] == ["0x30", "0x20"]  # front first; the dock is skipped
    front = found[0]
    assert front.title == "Front — Editor" and front.app == "org.gnome.TextEditor"
    assert front.pid == 555 and front.active and not front.minimized
    assert (front.x, front.y, front.width, front.height) == (66, 32, 800, 637)  # frame extents added
    assert found[1].minimized and found[1].app == "xterm" and not found[1].active


def test_x11_activation_sends_net_active_window(monkeypatch):
    connections = []
    monkeypatch.setattr(ad, "_X11", lambda: connections.append(FakeX11()) or connections[-1])
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    result = ad._activate_x11(win("0x20", "Legacy"))
    assert result == {"activated": True}
    assert connections[-1].sent[-1] == (0x20, "_NET_ACTIVE_WINDOW", [2, 0, 0, 0, 0])


def test_macos_window_filter(monkeypatch):
    infos = [
        {"kCGWindowLayer": 25, "kCGWindowName": "Menubar", "kCGWindowOwnerName": "Window Server",
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1000, "Height": 24}},
        {"kCGWindowLayer": 0, "kCGWindowAlpha": 1, "kCGWindowNumber": 42, "kCGWindowOwnerPID": 7,
         "kCGWindowOwnerName": "Safari", "kCGWindowName": "Apple",
         "kCGWindowBounds": {"X": 10, "Y": 20, "Width": 800, "Height": 600}},
        {"kCGWindowLayer": 0, "kCGWindowAlpha": 1, "kCGWindowNumber": 43, "kCGWindowOwnerPID": 8,
         "kCGWindowOwnerName": "Finder", "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 30, "Height": 30}},
        {"kCGWindowLayer": 0, "kCGWindowAlpha": 1, "kCGWindowNumber": 44, "kCGWindowOwnerPID": 7,
         "kCGWindowOwnerName": "Safari", "kCGWindowName": "",
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 500, "Height": 400}},
    ]
    quartz = types.SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=16, kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda options, wid: infos,
    )

    class App:
        def processIdentifier(self):
            return 7

    workspace = types.SimpleNamespace(frontmostApplication=lambda: App())
    appkit = types.SimpleNamespace(NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    found = ad._windows_macos()
    assert [w.id for w in found] == ["42", "44"]  # menubar layer and the tiny window are dropped
    assert found[0].active and not found[1].active
    assert found[0].app == "Safari" and found[0].title == "Apple" and found[1].title == ""
    assert (found[0].x, found[0].y, found[0].width, found[0].height) == (10, 20, 800, 600)


def test_launch_chrome_prefers_the_desktop_entry(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad, "find_chrome_executable", lambda: "/usr/bin/google-chrome")
    entry = ("google-chrome", "/usr/share/applications/google-chrome.desktop")
    monkeypatch.setattr(ad, "find_desktop_entry", lambda ids: entry)
    monkeypatch.setattr(ad.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("gtk-launch", "gio") else None)
    spawned, launched = [], []
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: spawned.append(argv))
    monkeypatch.setattr(ad, "run_launcher", lambda argv: launched.append(argv))
    code, out, _ = run("launch-chrome", "--url", "https://example.com")
    assert code == 0 and "launcher=gtk-launch" in out
    assert launched == [["gtk-launch", "google-chrome", "https://example.com"]] and spawned == []
    monkeypatch.setattr(ad.shutil, "which", lambda n: "/usr/bin/gio" if n == "gio" else None)
    run("launch-chrome")
    assert launched[-1] == ["gio", "launch", entry[1]]
    run("launch-chrome", "--profile", "Profile 1")  # no Chrome folder to check against here
    assert spawned[-1] == ["/usr/bin/google-chrome", "--profile-directory=Profile 1"]
    monkeypatch.setattr(ad, "find_desktop_entry", lambda ids: None)
    _, out, _ = run("launch-chrome")
    assert spawned[-1] == ["/usr/bin/google-chrome"] and "launcher=exec" in out


def test_a_failing_launcher_falls_back_to_chrome_itself(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad, "find_chrome_executable", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(ad, "find_desktop_entry", lambda ids: ("google-chrome", "/x.desktop"))
    monkeypatch.setattr(ad.shutil, "which", lambda n: "/usr/bin/gtk-launch" if n == "gtk-launch" else None)
    monkeypatch.setattr(ad, "run_launcher", lambda argv: "no such desktop file")
    spawned = []
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: spawned.append(argv))
    code, out, _ = run("launch-chrome", "--url", "example.com")
    assert code == 0 and spawned == [["/usr/bin/google-chrome", "https://example.com"]]
    assert "launcher=exec" in out and 'launcher_error="gtk-launch: no such desktop file"' in out


def test_flatpak_chrome_needs_no_binary_on_path(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad, "find_chrome_executable", lambda: (_ for _ in ()).throw(ad.CliError("not found")))
    monkeypatch.setattr(ad, "find_desktop_entry", lambda ids: ("com.google.Chrome", "/x.desktop"))
    monkeypatch.setattr(ad.shutil, "which", lambda n: "/usr/bin/gtk-launch" if n == "gtk-launch" else None)
    monkeypatch.setattr(ad, "run_launcher", lambda argv: None)
    code, out, _ = run("launch-chrome")
    assert code == 0 and "desktop entry com.google.Chrome" in out


@pytest.mark.parametrize("given,expected", [
    ("example.com", "https://example.com"),
    ("www.site.com/path?q=1", "https://www.site.com/path?q=1"),
    ("localhost:3000/app", "http://localhost:3000/app"),
    ("127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("https://mail.google.com", "https://mail.google.com"),
    ("about:blank", "about:blank"),
    ("chrome://settings", "chrome://settings"),
    ("file:///tmp/x.html", "file:///tmp/x.html"),
])
def test_normalize_url(given, expected):
    assert ad.normalize_url(given) == expected


def test_a_url_or_profile_that_looks_like_an_option_is_refused(run, monkeypatch):
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: pytest.fail("launched"))
    code, _, err = run("launch-chrome", "--url", "--disable-web-security")
    assert code == 1 and "--url must be a web address" in err
    code, _, err = run("launch-chrome", "--profile", "../other")
    assert code == 1 and "--profile must be a profile folder" in err


def test_unknown_profile_is_refused_and_display_names_are_mapped(run, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    user_data = tmp_path / "config" / "google-chrome"
    for folder in ("Default", "Profile 2"):
        (user_data / folder).mkdir(parents=True)
    (user_data / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"name": "Personal"}, "Profile 2": {"name": "Work"}}}}), encoding="utf-8")
    monkeypatch.setattr(ad, "find_chrome_executable", lambda: "/usr/bin/google-chrome")
    spawned = []
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: spawned.append(argv))
    code, _, err = run("launch-chrome", "--profile", "Profile 9")
    assert code == 1 and "no Chrome profile 'Profile 9'" in err and "'Profile 2' (Work)" in err
    assert spawned == []  # Chrome would have created an empty, signed-out profile
    code, out, _ = run("launch-chrome", "--profile", "work")
    assert code == 0 and spawned[-1] == ["/usr/bin/google-chrome", "--profile-directory=Profile 2"]


def test_macos_profile_goes_to_the_binary(run, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    app = tmp_path / "Google Chrome.app"
    app.mkdir()
    monkeypatch.setattr(ad, "MAC_CHROME_APPS", (str(app),))
    monkeypatch.setattr(ad, "chrome_user_data_dir", lambda: None)
    spawned = []
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: spawned.append(argv))
    code, out, _ = run("launch-chrome", "--profile", "Profile 1", "--url", "https://x.org")
    assert code == 0 and "launcher=binary" in out
    assert spawned == [[str(app / "Contents" / "MacOS" / "Google Chrome"), "--profile-directory=Profile 1", "https://x.org"]]


def test_find_desktop_entry(tmp_path, monkeypatch):
    apps = tmp_path / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / "com.google.Chrome.desktop").write_text("[Desktop Entry]\n")
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))
    monkeypatch.setattr(ad.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert ad.find_desktop_entry(ad.CHROME_DESKTOP_IDS) == (
        "com.google.Chrome", str(apps / "com.google.Chrome.desktop")
    )
    assert ad.find_desktop_entry(("nothing",)) is None


def test_macos_focus_checks_the_window_came_to_the_front(monkeypatch):
    app = types.SimpleNamespace(activateWithOptions_=lambda opts: True)
    appkit = types.SimpleNamespace(
        NSRunningApplication=types.SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: app),
        NSApplicationActivateIgnoringOtherApps=2,
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(ad.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(ad.time, "sleep", lambda s: None)
    target = ad.Win("7", 42, "Chrome", "Inbox", 0, 0, 800, 600, False, False)
    other = ad.Win("8", 42, "Chrome", "Docs", 0, 0, 800, 600, True, False)
    monkeypatch.setattr(ad, "_windows_macos", lambda: [other, target])
    assert ad._activate_macos(target)["activated"] is False  # the app came forward, another window of it
    monkeypatch.setattr(ad, "_windows_macos", lambda: [target._replace(active=True), other])
    assert ad._activate_macos(target)["activated"] is True


class TypedX11(FakeX11):
    atoms = {
        "_NET_WM_STATE_HIDDEN": 101, "_NET_WM_STATE_SKIP_TASKBAR": 102,
        "_NET_WM_WINDOW_TYPE_NORMAL": 201, "_NET_WM_WINDOW_TYPE_DIALOG": 202,
        "_NET_WM_WINDOW_TYPE_DOCK": 203, "_NET_WM_WINDOW_TYPE_DESKTOP": 204,
        "_NET_WM_WINDOW_TYPE_NOTIFICATION": 205,
    }
    props = {
        (1, "_NET_CLIENT_LIST_STACKING"): [0x10, 0x20, 0x30, 0x40, 0x50],
        (1, "_NET_ACTIVE_WINDOW"): [0x40],
        (0x10, "_NET_WM_NAME"): "Desktop icons", (0x10, "_NET_WM_WINDOW_TYPE"): [204],
        (0x20, "_NET_WM_NAME"): "Files", (0x20, "_NET_WM_WINDOW_TYPE"): [201],
        (0x20, "_GTK_FRAME_EXTENTS"): [23, 23, 15, 31],  # client-side shadow: invisible margin
        (0x30, "_NET_WM_NAME"): "Panel", (0x30, "_NET_WM_WINDOW_TYPE"): [203],
        # GNOME marks a dialog with a parent skip-taskbar; it must still be listed.
        (0x40, "_NET_WM_NAME"): "Save changes?", (0x40, "_NET_WM_WINDOW_TYPE"): [202],
        (0x40, "_NET_WM_STATE"): [102],
        (0x50, "_NET_WM_NAME"): "New mail", (0x50, "_NET_WM_WINDOW_TYPE"): [205],
    }
    geometries = {w: (100, 100, 446, 346, True) for w in (0x10, 0x20, 0x30, 0x40, 0x50)}


def test_x11_lists_dialogs_and_skips_docks_by_window_type(monkeypatch):
    monkeypatch.setattr(ad, "_X11", TypedX11)
    found = ad._windows_x11()
    assert [(w.title, w.type) for w in found] == [("Save changes?", "dialog"), ("Files", "normal")]
    assert found[0].active
    files = found[1]
    assert (files.x, files.y, files.width, files.height) == (123, 115, 400, 300)  # shadow removed
