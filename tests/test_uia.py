"""Windows UI Automation backend, against a fake UIA layer (runs on every OS), plus one
smoke test against the real UIAutomationCore on Windows."""
import json
import subprocess
import sys
import time
import types

import pytest
from PIL import Image

import ai_desktop as ad
import ai_desktop_ui as ui
import ai_desktop_uia as uia


class Node:
    def __init__(self, type, name="", rect=(0, 0, 0, 0), children=(), **props):
        self.props = {"type": type, "name": name, "rect": list(rect) if rect and rect[2] > 0 and rect[3] > 0 else None,
                      "offscreen": False, "enabled": True, "focused": False, "password": False, "framework": "",
                      "class_name": "", "pid": 0, "hwnd": 0, "value": None, "readonly": None, "toggle": None,
                      "selected": None, "expand": None, "patterns": [], **props}
        self.kids = list(children)


class FakeUia:
    """The ComApi interface over a tree of Nodes, recording what was done."""

    Error = (RuntimeError,)

    def __init__(self, *windows, foreground=None, screen=(0, 0, 1920, 1080)):
        self.windows = {w.props["hwnd"]: w for w in windows}
        self.fg = foreground if foreground is not None else (windows[0].props["hwnd"] if windows else 0)
        self.screen = screen
        self.done = []
        self.fail = set()
        self.focusable = True
        self.children_calls = 0

    def foreground(self):
        return self.fg

    def window(self, hwnd):
        return self.windows.get(hwnd)

    def top_windows(self):
        return list(self.windows.values())

    def children(self, node):
        self.children_calls += 1
        return node.kids

    def props(self, node):
        return node.props

    def process_name(self, pid):
        return {1: "chrome", 2: "notepad"}.get(pid, "")

    def virtual_screen(self):
        return self.screen

    def _do(self, verb, node, *args):
        if verb in self.fail:
            raise RuntimeError(f"{verb} failed")
        self.done.append((verb, node.props["name"], *args))

    def invoke(self, node):
        self._do("invoke", node)

    def toggle(self, node):
        self._do("toggle", node)

    def select(self, node):
        self._do("select", node)

    def expand(self, node):
        self._do("expand", node)
        return "expand"

    def default_action(self, node):
        self._do("default-action", node)

    def focus(self, node):
        self._do("focus", node)
        if self.focusable:
            node.props["focused"] = True

    def has_focus(self, node):
        return node.props["focused"]

    def set_value(self, node, text):
        self._do("set-value", node, text)
        node.props["value"] = text

    def value(self, node):
        return node.props["value"]

    def text(self, node, limit):
        return node.props.get("doc_text")

    def range_value(self, node):
        return node.props.get("range_value")


def chrome_window(page=None):
    """A Chrome window with tabs and a page holding a small form."""
    if page is None:
        page = [
            Node("Text", "Email", (120, 200, 40, 16)),
            Node("Edit", "Email", (180, 196, 300, 28), patterns=["value", "text"], value="a@b.c", readonly=False),
            Node("Edit", "Password", (180, 236, 300, 28), patterns=["value"], value="hunter2", readonly=False, password=True),
            Node("CheckBox", "Remember me", (180, 276, 16, 16), patterns=["toggle"], toggle=1),
            Node("Button", "Sign in", (180, 310, 90, 32), patterns=["invoke"]),
            Node("Hyperlink", "Forgot password?", (290, 316, 120, 18), patterns=["invoke", "value"], value="https://x/forgot"),
            Node("Group", "", (100, 180, 600, 200)),  # an unnamed div: not listed
            Node("Button", "Below the fold", (180, 2000, 90, 32), patterns=["invoke"], offscreen=True,
                 children=[Node("Text", "hidden child", (180, 2000, 50, 10))]),
            Node("RadioButton", "Plan B", (180, 360, 1, 1), patterns=["select"]),  # visually hidden input
        ]
    doc = Node("Document", "Sign in - Example", (0, 80, 1366, 650), patterns=["value", "text"], value="https://x/",
               readonly=True, children=page, framework="Chrome")
    tabs = Node("Tab", "", (0, 0, 1366, 40), children=[Node("TabItem", "Sign in - Example", (10, 4, 200, 32), patterns=["select"], selected=True)])
    return Node("Pane", "Sign in - Example - Google Chrome", (0, 0, 1366, 738), children=[tabs, doc],
                hwnd=0x1A2B, pid=1, framework="Win32", class_name="Chrome_WidgetWin_1")


def by_name(tree, name):
    return next(e for e in tree["elements"] if e["name"] == name)


# --- the tree --------------------------------------------------------------------------------


def test_tree_has_the_linux_helpers_shape_and_roles():
    tree = uia.a11y_tree({"scope": "active"}, FakeUia(chrome_window()))
    assert tree["ok"] and tree["windows"] == [{"path": [0x1A2B], "app": "chrome", "toolkit": "Win32",
                                               "title": "Sign in - Example - Google Chrome", "screen": [0, 0, 1366, 738],
                                               "window": [0, 0, 1366, 738]}]
    button = by_name(tree, "Sign in")
    assert set(button) == {"path", "role", "name", "rect", "states", "actions", "window"}
    assert button["role"] == "push button" and button["rect"] == [180, 310, 90, 32] and button["actions"] == ["invoke"]
    assert button["states"] == ["enabled", "showing", "visible", "sensitive"]
    assert button["path"] == [0x1A2B, 1, 4]  # window handle, then child indexes
    roles = {e["name"]: e["role"] for e in tree["elements"]}
    assert roles["Sign in - Example"] in ("page tab", "document web")
    assert roles["Forgot password?"] == "link" and roles["Remember me"] == "check box" and roles["Password"] == "password text"
    email = [e for e in tree["elements"] if e["name"] == "Email"]
    assert sorted(e["role"] for e in email) == ["entry", "label"]
    entry = next(e for e in email if e["role"] == "entry")
    assert entry["text"] == "a@b.c" and "editable" in entry["states"]
    assert "text" not in by_name(tree, "Password")  # never a password
    assert "checked" in by_name(tree, "Remember me")["states"]
    assert [e["rect"][1] for e in tree["elements"]] == sorted(e["rect"][1] for e in tree["elements"])


def test_offscreen_subtrees_and_unnamed_containers_are_skipped():
    tree = uia.a11y_tree({"scope": "active"}, FakeUia(chrome_window()))
    names = [e["name"] for e in tree["elements"]]
    assert "Below the fold" not in names and "hidden child" not in names and "" not in names


def test_elements_without_usable_bounds_are_marked_none():
    tree = uia.a11y_tree({"scope": "active"}, FakeUia(chrome_window()))
    assert by_name(tree, "Plan B")["where"] == "none"  # 1x1: a real click would miss
    assert "where" not in by_name(tree, "Sign in")
    win = chrome_window([Node("Button", "Off the side", (1500, 10, 80, 30), patterns=["invoke"])])
    assert by_name(uia.a11y_tree({}, FakeUia(win)), "Off the side")["where"] == "none"  # outside its window


def test_node_depth_and_child_caps(monkeypatch):
    monkeypatch.setattr(uia, "MAX_NODES", 10)
    many = [Node("Button", f"b{i}", (10 + i, 100, 20, 20), patterns=["invoke"]) for i in range(50)]
    tree = uia.a11y_tree({}, FakeUia(chrome_window(many)))
    assert tree["truncated"] and tree["visited"] == 10
    monkeypatch.setattr(uia, "MAX_NODES", 4000)
    monkeypatch.setattr(uia, "MAX_CHILDREN", 5)
    tree = uia.a11y_tree({}, FakeUia(chrome_window(many)))
    assert [e["name"] for e in tree["elements"] if e["name"].startswith("b")] == ["b0", "b1", "b2", "b3", "b4"]
    deep = Node("Button", "deep", (10, 300, 20, 20), patterns=["invoke"])
    for _ in range(5):
        deep = Node("Group", "", (0, 0, 500, 500), children=[deep])
    monkeypatch.setattr(uia, "MAX_DEPTH", 4)
    assert "deep" not in [e["name"] for e in uia.a11y_tree({}, FakeUia(chrome_window([deep])))["elements"]]


def test_scope_all_lists_every_visible_top_level_window():
    note = Node("Window", "notes.txt - Notepad", (1400, 100, 500, 400), hwnd=0x77, pid=2, class_name="Notepad",
                children=[Node("Edit", "Text editor", (1400, 150, 500, 340), patterns=["value"], value="hi", readonly=False)])
    hidden = Node("Window", "tray", (0, 0, 0, 0), hwnd=0x88)
    tree = uia.a11y_tree({"scope": "all"}, FakeUia(chrome_window(), note, hidden))
    assert [w["app"] for w in tree["windows"]] == ["chrome", "notepad"]
    editor = by_name(tree, "Text editor")
    assert editor["window"] == 1 and editor["path"] == [0x77, 0] and editor["role"] == "entry"


def test_an_empty_chromium_page_is_retried_then_explained(sleeps):
    fake = FakeUia(chrome_window([]))
    tree = uia.a11y_tree({}, fake)
    assert sleeps.count(uia.CHROMIUM_RETRY_WAIT) == uia.CHROMIUM_RETRIES
    assert "launch-chrome --a11y" in tree["note"]
    # Chrome builds the page tree after the first request: the retry finds it.
    win = chrome_window([])
    fake = FakeUia(win)
    real_children = fake.children

    def grows(node):
        if node.props["type"] == "Document" and fake.children_calls > 3 and not node.kids:
            node.kids.append(Node("Button", "Late", (10, 300, 50, 20), patterns=["invoke"]))
        return real_children(node)

    fake.children = grows
    tree = uia.a11y_tree({}, fake)
    assert "note" not in tree and by_name(tree, "Late")


def test_a_native_app_is_not_retried(sleeps):
    win = Node("Window", "Calculator", (0, 0, 300, 400), hwnd=5, class_name="ApplicationFrameWindow",
               children=[Node("Button", "Seven", (10, 200, 50, 50), patterns=["invoke"])])
    tree = uia.a11y_tree({}, FakeUia(win))
    assert not sleeps and "note" not in tree and by_name(tree, "Seven")["role"] == "push button"


# --- acting --------------------------------------------------------------------------------


def request(tree, name, do, **extra):
    e = by_name(tree, name)
    return {"op": "a11y-do", "path": e["path"], "role": e["role"], "name": e["name"], "do": do, **extra}


def test_press_uses_invoke_toggle_then_select():
    fake = FakeUia(chrome_window())
    tree = uia.a11y_tree({}, fake)
    assert uia.a11y_do(request(tree, "Sign in", "press"), fake) == {"ok": True, "did": "invoke"}
    assert uia.a11y_do(request(tree, "Remember me", "press"), fake)["did"] == "toggle"
    assert uia.a11y_do(request(tree, "Plan B", "press"), fake)["did"] == "select"
    assert [d[0] for d in fake.done] == ["invoke", "toggle", "select"]


def test_press_falls_back_to_focus_and_says_when_nothing_works():
    label = Node("Text", "Just text", (10, 300, 80, 16))
    fake = FakeUia(chrome_window([label]))
    tree = uia.a11y_tree({}, fake)
    assert uia.a11y_do(request(tree, "Just text", "press"), fake)["did"] == "focus"
    fake.focusable = False
    label.props["focused"] = False
    answer = uia.a11y_do(request(tree, "Just text", "press"), fake)
    assert answer["ok"] is False and answer["error"] == "no action"


def test_a_changed_element_is_stale():
    win = chrome_window()
    fake = FakeUia(win)
    tree = uia.a11y_tree({}, fake)
    doc = win.kids[1]
    doc.kids[4] = Node("Button", "Delete account", (180, 310, 90, 32), patterns=["invoke"])
    answer = uia.a11y_do(request(tree, "Sign in", "press"), fake)
    assert answer == {"ok": False, "error": "stale", "detail": "now push button 'Delete account'"} and not fake.done
    del doc.kids[3:]
    assert uia.a11y_do(request(tree, "Sign in", "press"), fake)["detail"] == "the element is gone"
    fake.windows.clear()  # the window closed
    assert uia.a11y_do(request(tree, "Email", "read"), fake)["error"] == "stale"


def test_set_text_uses_the_value_pattern_else_asks_for_the_paste_fallback():
    fake = FakeUia(chrome_window())
    tree = uia.a11y_tree({}, fake)
    entry = next(e for e in tree["elements"] if e["role"] == "entry")
    req = {"op": "a11y-do", "path": entry["path"], "role": "entry", "name": "Email", "do": "set-text", "text": "me@x.org"}
    assert uia.a11y_do(req, fake) == {"ok": True, "did": "set-text"} and fake.done[-1] == ("set-value", "Email", "me@x.org")
    fake.fail.add("set-value")
    assert uia.a11y_do(req, fake) == {"ok": False, "error": "not editable", "fallback": "paste"}
    answer = uia.a11y_do(request(tree, "Sign in", "set-text", text="x"), fake)  # no ValuePattern at all
    assert answer["fallback"] == "paste"


def test_read_returns_value_text_or_name_but_never_a_password():
    fake = FakeUia(chrome_window())
    tree = uia.a11y_tree({}, fake)
    entry = next(e for e in tree["elements"] if e["role"] == "entry")
    info = uia.a11y_do({"op": "a11y-do", "path": entry["path"], "role": "entry", "name": "Email", "do": "read"}, fake)
    assert info["text"] == "a@b.c" and info["name"] == "Email" and "editable" in info["states"]
    assert "text" not in uia.a11y_do(request(tree, "Password", "read"), fake)
    assert uia.a11y_do(request(tree, "Forgot password?", "read"), fake).get("text") is None  # not the URL
    label = next(e for e in tree["elements"] if e["role"] == "label")
    info = uia.a11y_do({"op": "a11y-do", "path": label["path"], "role": "label", "name": "Email", "do": "read"}, fake)
    assert info["text"] == "Email"


def test_a_com_error_becomes_an_error_answer():
    fake = FakeUia(chrome_window())
    tree = uia.a11y_tree({}, fake)
    fake.fail.add("invoke")
    answer = uia.a11y_do(request(tree, "Sign in", "press"), fake)
    assert answer["ok"] is False and answer["error"] == "RuntimeError"


def test_property_values_are_cleaned():
    assert uia._rect((10.0, 20.4, 30.6, 40.0)) == [10, 20, 31, 40]
    assert uia._rect((0.0, 0.0, 0.0, 0.0)) is None and uia._rect((float("inf"), 0, 1, 1)) is None
    assert uia._rect(None) is None and uia._plain(object()) is None and uia._plain("x") == "x"


# --- caching and the CLI on Windows ------------------------------------------------------------


@pytest.fixture
def windows(monkeypatch, fake_gui):
    """Run the CLI as on Windows, with the fake UIA layer behind ai_desktop_ui."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(uia, "unavailable", lambda: None)
    monkeypatch.setenv("AI_DESKTOP_BACKEND", "pyautogui")
    fake = FakeUia(chrome_window())
    monkeypatch.setattr(uia, "api", lambda: fake)
    screen = types.SimpleNamespace(image=Image.new("RGB", (1920, 1080), (255, 255, 255)))
    monkeypatch.setattr(ad, "capture_screen", lambda backend, monitor="all", cursor=False: ad.Capture(screen.image, 0, 0, 1.0, "all"))
    clipboard = []
    monkeypatch.setattr(ad, "set_clipboard", clipboard.append)
    fake.screen_img, fake.clipboard = screen, clipboard
    return fake


def test_ui_on_windows_prints_the_same_lines_as_linux(run, windows):
    code, out, err = run("ui")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("OK ui count=") and "windows=1" in lines[0], err
    assert lines[1] == "# chrome: Sign in - Example - Google Chrome"
    assert any(line.endswith('push button "Sign in" 180,310 90x32') for line in lines)
    assert any('entry "Email" 180,196 300x28 text="a@b.c"' in line for line in lines)
    assert any('radio button "Plan B" 180,360 1x1 (no position)' in line for line in lines)
    code, out, _ = run("--json", "ui", "--role", "push button")
    assert {e["name"] for e in json.loads(out)["elements"]} == {"Sign in"}


def test_the_tree_is_cached_until_the_screen_changes(run, windows):
    run("ui")
    first = windows.children_calls
    run("ui")
    assert windows.children_calls == first  # same pixels, same window: no walk
    windows.screen_img.image = Image.new("RGB", (1920, 1080), (0, 0, 0))
    run("ui")
    assert windows.children_calls > first


def test_acting_forgets_the_cached_tree(run, windows):
    run("ui")
    run("click", "--ref", next(e["id"] for e in ui.load_snapshot()["elements"] if e["name"] == "Plan B"))
    before = windows.children_calls
    run("ui")
    assert windows.children_calls > before


def test_click_name_on_windows_is_a_real_mouse_click(run, windows, fake_gui):
    code, out, err = run("click", "--name", "Sign in", "--instant")
    assert code == 0 and "x=225 y=326" in out and "found_by=accessibility" in out, err
    assert fake_gui.calls[-1][0] == "click" and not windows.done


def test_click_without_usable_bounds_goes_through_uia(run, windows, fake_gui):
    code, out, err = run("click", "--name", "Plan B")
    assert code == 0 and "via=accessibility did=select" in out, err
    assert windows.done == [("select", "Plan B")] and not any(c[0] == "click" for c in fake_gui.calls)


def test_a_stale_reference_is_refused_on_windows(run, windows):
    run("ui")
    ref = next(e["id"] for e in ui.load_snapshot()["elements"] if e["name"] == "Plan B")
    windows.windows[0x1A2B].kids[1].kids[8] = Node("Button", "Pay now", (180, 360, 1, 1), patterns=["invoke"])
    code, _, err = run("click", "--ref", ref)
    assert code == 1 and f"stale reference {ref}" in err and not windows.done


def test_target_uses_accessibility_first_on_windows(run, windows):
    code, out, err = run("click", "--target", "Forgot password?", "--instant")
    assert code == 0 and "found_by=accessibility" in out and "x=350 y=325" in out, err


def test_set_text_on_windows_uses_the_value_pattern(run, windows, fake_gui):
    code, out, err = run("set-text", "--name", "Email", "--role", "entry", "--value", "me@example.org")
    assert code == 0 and "chars=14" in out and "did=" not in out, err
    assert windows.done[-1] == ("set-value", "Email", "me@example.org") and not windows.clipboard


def test_set_text_falls_back_to_focus_select_all_and_paste(run, windows, fake_gui):
    windows.fail.add("set-value")
    code, out, err = run("set-text", "--name", "Email", "--role", "entry", "--value", "secret note")
    assert code == 0 and "did=paste" in out, err
    assert windows.done[-1] == ("focus", "Email") and windows.clipboard == ["secret note"]
    hotkeys = [c[1] for c in fake_gui.calls if c[0] == "hotkey"]
    assert hotkeys == [("ctrl", "a"), ("ctrl", "v")], fake_gui.calls  # select all, then paste
    log = ad.log_files()[0][0].read_text(encoding="utf-8")
    assert "secret note" not in log


def test_set_text_fallback_clicks_when_focus_fails(run, windows, fake_gui):
    windows.fail.add("set-value")
    windows.focusable = False
    code, out, err = run("set-text", "--name", "Email", "--role", "entry", "--value", "x")
    assert code == 0 and "did=paste" in out, err
    assert any(c[0] == "click" for c in fake_gui.calls)


def test_read_on_windows(run, windows):
    code, out, err = run("read", "--name", "Email", "--role", "entry")
    assert code == 0 and out.splitlines()[1] == "a@b.c", err


def test_ui_note_when_chrome_shows_no_page(run, windows, monkeypatch):
    monkeypatch.setattr(uia, "api", lambda: FakeUia(chrome_window([])))
    code, out, _ = run("ui")
    assert code == 0 and 'note="this Chromium window' in out


def test_ui_enable_explains_windows(run, windows):
    code, _, err = run("ui", "enable")
    assert code == 1 and "always on in Windows" in err


def test_missing_comtypes_is_a_clear_error(run, monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "comtypes" else real(name, *a))
    monkeypatch.setattr(sys, "platform", "win32")
    code, _, err = run("ui")
    assert code == 1 and 'pip install "ai-desktop[windows]"' in err


def test_doctor_reports_ui_automation(monkeypatch, windows):
    check = ad.run_check("ui-automation", ad._check_ui_automation, soft=True)
    assert check.state == "ok" and "comtypes" in check.detail and "1 top-level" in check.detail
    monkeypatch.setattr(uia, "unavailable", lambda: "no comtypes")
    check = ad.run_check("ui-automation", ad._check_ui_automation, soft=True)
    assert check.state == "warn" and "ai-desktop[windows]" in check.hint


# --- the real thing ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UI Automation")
def test_smoke_real_ui_automation(tmp_path):
    pytest.importorskip("comtypes")
    script = tmp_path / "win.py"
    script.write_text(
        "import tkinter as tk\n"
        "r = tk.Tk(); r.title('ai-desktop uia smoke'); r.geometry('300x120+100+100')\n"
        "tk.Button(r, text='Smoke OK').pack(); r.after(20000, r.destroy); r.mainloop()\n")
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        found = None
        for _ in range(40):
            tree = uia.a11y_tree({"scope": "all"})
            assert tree["ok"]
            found = next((e for e in tree["elements"] if e["name"] == "Smoke OK"), None)
            if found:
                break
            time.sleep(0.25)
        assert found, [w["title"] for w in tree["windows"]]
        assert found["role"] == "push button" and found["rect"][2] > 0
        info = uia.a11y_do({"op": "a11y-do", "path": found["path"], "role": found["role"], "name": found["name"], "do": "read"})
        assert info["ok"] and info["name"] == "Smoke OK"
    finally:
        proc.kill()
