"""The local vision model: answer parsing, the two looks, memory, and the HTTP calls."""
import json

import pytest
from PIL import Image, ImageDraw

import ai_desktop as ad
import ai_desktop_vision as vision


@pytest.mark.parametrize("answer,expected", [
    ('{"x": 120, "y": 80}', (120, 80)),
    ('Sure! ```json\n{"x": 120.4, "y": 79.6}\n```', (120, 80)),
    ('{"bbox_2d": [100, 60, 140, 100]}', (120, 80)),
    ("The icon is at 120, 80.", (120, 80)),
    ('{"x": 0.5, "y": 0.25}', (500, 100)),  # fractions of the image
    ('{"found": false}', None),
    ('{"x": 5000, "y": 80}', None),  # off the image
    ("I can't see it.", None),
])
def test_parse_point(answer, expected):
    assert vision.parse_point(answer, 1000 if expected == (500, 100) else 400, 400) == expected


def test_qwen_grid_coordinates_are_scaled():
    # Some models answer on a 0-1000 grid whatever the image size.
    assert vision.parse_point('{"x": 900, "y": 500}', 640, 360) == (576, 180)


def screen_with_gear():
    img = Image.new("RGB", (1920, 1080), (250, 250, 250))
    ImageDraw.Draw(img).ellipse([1490, 80, 1510, 100], fill=(30, 30, 30))
    return ad.Capture(img, 0, 0, 1.0, "all")


def test_locate_looks_twice_and_remembers(monkeypatch):
    questions = []

    def ask(img, question):
        questions.append((img.size, question))
        if "Find:" in question:
            # the whole screen goes at 1024 px wide: the gear at 1500,90 is at 800,48 there
            return '{"x": 800, "y": 48}'
        return "Yes."

    monkeypatch.setattr(vision, "ask", ask)
    vision._MEMORY.clear()
    cap = screen_with_gear()
    found = vision.locate(cap, "the gear icon")
    assert (found["x"], found["y"], found["verified"]) == (1500, 90, True)
    assert questions[0][0] == (1024, 576) and "red cross" in questions[1][1]
    again = vision.locate(cap, "The gear icon")
    assert again["remembered"] is True and len(questions) == 2  # no third question


def test_locate_refuses_when_the_close_up_disagrees(monkeypatch):
    monkeypatch.setattr(vision, "ask", lambda img, q: '{"x": 100, "y": 100}' if "Find:" in q else "No, it is a menu.")
    vision._MEMORY.clear()
    found = vision.locate(screen_with_gear(), "the gear icon")
    assert found["found"] is False and "disagreed" in found["reason"]


def test_memory_is_dropped_when_the_pixels_change(monkeypatch):
    calls = []
    monkeypatch.setattr(vision, "ask", lambda img, q: calls.append(q) or ('{"x": 800, "y": 48}' if "Find:" in q else "yes"))
    vision._MEMORY.clear()
    vision.locate(screen_with_gear(), "gear")
    moved = Image.new("RGB", (1920, 1080), (250, 250, 250))
    ImageDraw.Draw(moved).rectangle([1440, 40, 1560, 140], fill=(200, 0, 0))
    vision.locate(ad.Capture(moved, 0, 0, 1.0, "all"), "gear")
    assert len(calls) == 4  # asked again


def test_ollama_and_openai_requests(monkeypatch):
    sent = []

    def http(url, payload, timeout):
        sent.append((url, payload))
        if url.endswith("/api/tags"):
            return {"models": [{"name": "llama3:8b"}, {"name": "qwen2.5vl:3b", "size": 3_200_000_000}]}
        if url.endswith("/models"):
            return {"data": [{"id": "qwen2-vl-7b"}]}
        if url.endswith("/api/chat"):
            return {"message": {"content": "a dialog"}}
        return {"choices": [{"message": {"content": "a dialog"}}]}

    monkeypatch.setattr(vision, "_http", http)
    img = Image.new("RGB", (10, 10))
    assert vision.ask(img, "what?") == "a dialog"
    assert sent[-1][0].endswith("/api/chat") and sent[-1][1]["model"] == "qwen2.5vl:3b"
    assert sent[-1][1]["messages"][0]["images"][0]  # base64 JPEG
    monkeypatch.setenv("AI_DESKTOP_VISION_URL", "http://127.0.0.1:1234/v1")
    assert vision.ask(img, "what?") == "a dialog"
    content = sent[-1][1]["messages"][0]["content"]
    assert sent[-1][0] == "http://127.0.0.1:1234/v1/chat/completions" and content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert vision.status()["model"] == "qwen2-vl-7b"


def test_no_vision_model_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(vision, "_http", lambda url, payload, timeout: {"models": [{"name": "llama3:8b"}]})
    with pytest.raises(ad.CliError, match="ollama pull qwen2.5vl:3b"):
        vision.pick_model(vision.settings())


def test_no_server_is_a_clear_error(monkeypatch):
    import urllib.error

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(vision.urllib.request, "urlopen", refuse)
    with pytest.raises(ad.CliError, match="no local vision model server"):
        vision.pick_model(vision.settings())


def test_locate_and_describe_commands(run, fake_gui, monkeypatch):
    monkeypatch.setattr(ad, "capture_screen", lambda backend, monitor="all", cursor=False: screen_with_gear())
    monkeypatch.setattr(vision, "ask", lambda img, q: '{"x": 800, "y": 48}' if "Find:" in q else ("yes" if "red cross" in q else "A settings page."))
    vision._MEMORY.clear()
    code, out, _ = run("locate", "the gear icon")
    assert code == 0 and out.startswith("OK locate x=1500 y=90 found_by=vision verified=True")
    code, out, _ = run("describe")
    assert code == 0 and out.splitlines()[1] == "A settings page."
    monkeypatch.setattr(vision, "ask", lambda img, q: '{"found": false}')
    vision._MEMORY.clear()
    code, _, err = run("locate", "a unicorn")
    assert code == 1 and "could not find 'a unicorn'" in err


def test_observe_describe_adds_words(run, fake_gui, monkeypatch):
    import ai_desktop_live as live

    monkeypatch.setattr(live, "observe", lambda view, **kw: {"changed": True, "seq": 2, "since": 1, "changed_x": 10, "changed_y": 10,
                                                             "changed_width": 50, "changed_height": 40, "areas": 1,
                                                             "_cap": screen_with_gear()})
    monkeypatch.setattr(vision, "ask", lambda img, q: "A small dialog asking to save.")
    code, out, _ = run("--json", "observe", "--describe", "--no-shot")
    data = json.loads(out)
    assert code == 0 and data["description"] == "A small dialog asking to save."
