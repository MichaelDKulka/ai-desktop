"""Finding things by name: text recognition, the accessibility tree, click targets."""
import json
import sys
import types

import pytest
from PIL import Image

import ai_desktop as ad
import ai_desktop_ui as ui


def word(text, x, y, w=40, h=14, conf=90):
    return {"text": text, "conf": conf, "x": x, "y": y, "w": w, "h": h}


def tsv(words):
    """Tesseract's TSV for words given in screen px (it sees the image OCR_SCALE times bigger)."""
    s = ui.OCR_SCALE
    rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
    for i, w in enumerate(words, 1):
        rows.append(f"5\t1\t{i}\t1\t1\t1\t{w['x'] * s}\t{w['y'] * s}\t{w['w'] * s}\t{w['h'] * s}\t{w['conf']}\t{w['text']}")
    return "\n".join(rows).encode()


@pytest.fixture
def screen_words(monkeypatch, fake_gui):
    """A fake screen whose text Tesseract 'reads' as the words set on .words."""
    state = types.SimpleNamespace(words=[])
    monkeypatch.setattr(ad, "capture_screen", lambda backend, monitor="all", cursor=False: ad.Capture(
        Image.new("RGB", (1920, 1080), (255, 255, 255)), 0, 0, 1.0, "all"))
    monkeypatch.setattr(ui.shutil, "which", lambda name: "/usr/bin/tesseract" if name == "tesseract" else None)
    ui._OCR_CACHE.clear()

    def run(argv, **kwargs):
        assert argv[0] == "/usr/bin/tesseract"
        ui._OCR_CACHE.clear()
        return types.SimpleNamespace(returncode=0, stdout=tsv(state.words), stderr=b"")

    monkeypatch.setattr(ui.subprocess, "run", run)
    return state


def test_words_on_a_line_become_phrases():
    words = [word("Save", 100, 50), word("changes?", 144, 51, w=70), word("Cancel", 400, 50), word("OK", 100, 200)]
    phrases = ui.group_phrases(words)
    assert [p["text"] for p in phrases] == ["Save changes?", "Cancel", "OK"]
    assert phrases[0]["x"] == 100 and phrases[0]["w"] == 114


def test_text_command_lists_phrases(run, screen_words):
    screen_words.words = [word("Compose", 86, 196, w=92, h=24), word("Inbox", 90, 260)]
    code, out, _ = run("text")
    assert code == 0 and out.splitlines()[0] == "OK text count=2"
    assert out.splitlines()[1] == 't1 "Compose" 86,196 92x24'
    code, out, _ = run("--json", "text", "--find", "inbox")
    assert json.loads(out)["items"][0]["text"] == "Inbox"


def test_click_text_clicks_the_middle_of_it(run, screen_words, fake_gui):
    screen_words.words = [word("Compose", 86, 196, w=92, h=24)]
    code, out, _ = run("click", "--text", "compose", "--instant")
    assert code == 0 and "x=132 y=208" in out and "found_by=text" in out
    assert fake_gui.calls[-1][2]["x"] == 132


def test_click_text_refuses_to_guess(run, screen_words, fake_gui):
    screen_words.words = [word("Delete", 100, 100), word("Delete", 100, 400)]
    code, _, err = run("click", "--text", "Delete")
    assert code == 1 and "2 places show 'Delete'" in err and not any(c[0] == "click" for c in fake_gui.calls)
    code, out, _ = run("click", "--text", "Delete", "--nth", "2", "--instant")
    assert code == 0 and "y=407" in out
    code, _, err = run("click", "--text", "Nowhere")
    assert code == 1 and "no text 'Nowhere'" in err


def test_click_needs_a_point_or_a_target(run, fake_gui):
    code, _, err = run("click")
    assert code == 2 and "give --x and --y, or a target" in err
    code, _, err = run("click", "--x", "1", "--y", "1", "--text", "x")
    assert code == 2 and "not both" in err


# --- accessibility ------------------------------------------------------------------------

TREE = {
    "ok": True,
    "windows": [
        {"path": [3, 0], "app": "gedit", "toolkit": "GTK", "title": "notes", "screen": [100, 50, 800, 600], "window": [0, 0, 800, 600]},
        {"path": [5, 0], "app": "Google Chrome", "toolkit": "Chromium", "title": "Mail", "screen": [0, 0, 1300, 736], "window": [0, 0, 1300, 736]},
    ],
    "elements": [
        {"path": [3, 0, 1], "role": "push button", "name": "Save", "rect": [700, 60, 60, 30], "states": ["showing"], "actions": ["click"], "window": 0},
        {"path": [5, 0, 2], "role": "push button", "name": "Compose", "rect": [20, 150, 92, 24], "states": ["showing"], "actions": ["press"], "window": 1},
        {"path": [5, 0, 3], "role": "link", "name": "Inbox", "rect": [24, 210, 40, 14], "states": ["showing"], "actions": ["jump"], "window": 1},
        {"path": [5, 0, 4], "role": "link", "name": "Starred", "rect": [24, 240, 50, 14], "states": ["showing"], "actions": ["jump"], "window": 1},
        {"path": [5, 0, 5], "role": "entry", "name": "Search mail", "rect": [300, 20, 400, 30], "states": ["showing", "editable"], "actions": [], "window": 1},
    ],
}


@pytest.fixture
def tree(monkeypatch, screen_words):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("AI_DESKTOP_BACKEND", "pyautogui")  # clicks go to fake_gui
    asked = []

    def ask(request):
        asked.append(request)
        if request["op"] == "a11y-tree":
            return json.loads(json.dumps(TREE))
        return {"ok": True, "did": "press"}

    monkeypatch.setattr(ui, "ask", ask)
    # Chrome's window is really at (66, 32): its names show up there in the text.
    screen_words.words = [word("Compose", 86, 194, w=92, h=24), word("Inbox", 90, 248), word("Starred", 90, 278, w=50)]
    return asked


def test_window_relative_positions_are_placed_by_matching_text(tree):
    assert ui.coordinates_are_relative(TREE["windows"][1]) and not ui.coordinates_are_relative(TREE["windows"][0])
    snap = ui.snapshot()
    by_name = {e["name"]: e for e in snap["elements"]}
    assert by_name["Save"]["where"] == "screen" and by_name["Save"]["rect"] == [700, 60, 60, 30]
    assert by_name["Compose"]["where"] == "aligned" and by_name["Compose"]["rect"][:2] == [86, 194]
    assert by_name["Search mail"]["rect"][:2] == [366, 64]  # the same offset for everything in that window


def test_too_few_matches_leave_positions_window_relative(tree, screen_words):
    screen_words.words = [word("Compose", 86, 194, w=92, h=24)]
    snap = ui.snapshot()
    assert {e["name"]: e["where"] for e in snap["elements"]}["Inbox"] == "window"


def test_ui_command_lists_elements_with_refs(run, tree):
    code, out, _ = run("ui")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("OK ui count=5 windows=2")
    assert any(line.startswith('e2 push button "Compose" 86,194 92x24 ~') for line in lines)
    code, out, _ = run("ui", "--role", "link", "--find", "star")
    assert out.splitlines()[-1].startswith('e4 link "Starred"')


def test_click_ref_uses_the_screen_position_when_known(run, tree, fake_gui):
    run("ui")
    code, out, err = run("click", "--ref", "e2", "--instant")
    assert code == 0 and "x=132 y=206" in out and "found_by=accessibility" in out, err
    assert fake_gui.calls[-1][0] == "click"


def test_click_presses_through_accessibility_without_a_position(run, tree, fake_gui, screen_words):
    screen_words.words = []
    run("ui")
    code, out, _ = run("click", "--ref", "e3")
    assert code == 0 and "via=accessibility did=press" in out
    assert tree[-1] == {"op": "a11y-do", "path": [5, 0, 3], "role": "link", "name": "Inbox", "do": "press"}
    assert not any(c[0] == "click" for c in fake_gui.calls)


def test_a_stale_reference_is_refused(run, tree, monkeypatch, screen_words):
    screen_words.words = []
    run("ui")
    monkeypatch.setattr(ui, "ask", lambda request: {"ok": False, "error": "stale", "detail": "now link 'Spam'"})
    code, _, err = run("click", "--ref", "e3")
    assert code == 1 and "stale reference e3" in err and "ai-desktop ui" in err


def test_click_name_and_its_ambiguity(run, tree, fake_gui):
    code, out, _ = run("click", "--name", "save", "--instant")
    assert code == 0 and "x=730 y=75" in out
    code, _, err = run("click", "--name", "nothing")
    assert code == 1 and "no element named 'nothing'" in err


def test_set_text_goes_through_accessibility_and_stays_out_of_the_log(run, tree):
    run("ui")
    code, out, _ = run("set-text", "--ref", "e5", "--value", "invoice from Bob")
    assert code == 0 and "chars=16" in out
    assert tree[-1]["do"] == "set-text" and tree[-1]["text"] == "invoice from Bob"
    log = ad.log_files()[0][0].read_text(encoding="utf-8")
    assert "invoice from Bob" not in log and '"value_chars": 16' in log


def test_target_tries_accessibility_then_text_then_vision(run, tree, screen_words, monkeypatch, fake_gui):
    code, out, err = run("click", "--target", "Save", "--instant")
    assert code == 0 and "found_by=accessibility" in out, err
    screen_words.words = [word("Unsubscribe", 900, 700, w=90)]
    ui._OCR_CACHE.clear()  # the fake screen's pixels don't change with its words
    code, out, err = run("click", "--target", "Unsubscribe", "--instant")
    assert code == 0 and "found_by=text" in out, err
    monkeypatch.setattr(ad.vision_module(), "locate_target", lambda backend, what: {"x": 1500, "y": 90, "found_by": "vision", "verified": True})
    code, out, _ = run("click", "--target", "the gear icon", "--instant")
    assert code == 0 and "x=1500 y=90 " in out and "found_by=vision verified=True" in out
    monkeypatch.setattr(ad.vision_module(), "locate_target", lambda backend, what: None)
    code, _, err = run("click", "--target", "a purple unicorn")
    assert code == 1 and "accessibility:" in err and "text: not on the screen" in err and "vision: not found" in err


def test_screenshot_marks_numbers_the_elements(run, tree):
    code, out, _ = run("screenshot", "--marks", "--cursor", "none")
    assert code == 0 and "marks=" in out
    code, out, _ = run("click", "--mark", "2", "--instant")
    assert code == 0 and "ref=e2" in out


def test_launch_chrome_a11y_adds_the_flag(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ad, "find_chrome_executable", lambda: "/usr/bin/google-chrome")
    spawned = []
    monkeypatch.setattr(ad, "spawn_detached", lambda argv: spawned.append(argv))
    assert run("launch-chrome", "--a11y")[0] == 0
    assert spawned == [["/usr/bin/google-chrome", "--force-renderer-accessibility"]]


def test_accessibility_says_where_it_is_not_supported(monkeypatch, run):
    monkeypatch.setattr(sys, "platform", "darwin")
    code, _, err = run("ui")
    assert code == 1 and "Linux and Windows so far" in err and "macOS" in err


def test_no_tesseract_is_a_clear_error(run, monkeypatch, fake_gui):
    monkeypatch.setattr(ad, "capture_screen", lambda backend, monitor="all", cursor=False: ad.Capture(Image.new("RGB", (10, 10)), 0, 0, 1.0, "all"))
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    code, _, err = run("text")
    assert code == 1 and "sudo apt install tesseract-ocr" in err
