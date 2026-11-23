"""A local vision model as ai-desktop's eyes: find things that have no name and no text.

Talks to a model on this machine (Ollama by default; LM Studio and the llama.cpp server
through their OpenAI-compatible API) with the standard library only. The agent's own,
bigger model then gets a line of text ("found at 1210,88") instead of a screenshot.

Small models make mistakes, so `locate` looks twice: once at the whole screen, then at a
close-up of the candidate with a yes/no question; if the two disagree it refuses rather
than guesses. A target found once is remembered, and reused only while the pixels around
it are unchanged.

Settings: AI_DESKTOP_VISION_URL (default http://127.0.0.1:11434; a URL ending in /v1 is
taken as OpenAI-compatible), AI_DESKTOP_VISION_MODEL (default: the first vision model
Ollama lists), AI_DESKTOP_VISION_TIMEOUT (seconds, default 180: a CPU is slow).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request

import ai_desktop as _ai_desktop

AD = _ai_desktop
DEFAULT_URL = "http://127.0.0.1:11434"
VISION_NAME_HINTS = ("vl", "vision", "llava", "moondream", "minicpm-v", "gemma3", "bakllava", "pixtral", "ui-tars", "showui")
# Qwen-VL models answer in the pixels of the image as *they* resize it: sides that are
# multiples of 28, about this many pixels in all (measured with Ollama's qwen2.5vl: a
# 1024x576 and a 640x360 image both came back in 1204x672 coordinates). Sending the
# image at exactly that size makes the answer plain pixels of what was sent.
MODEL_PIXELS = 1204 * 672
MODEL_GRID = 28
CLOSEUP = (240, 140)  # screen px around the candidate for the second look
_MEMORY: dict = {}  # description -> (x, y, patch image) for reuse while unchanged


def bind(module) -> None:
    global AD
    AD = module


def settings() -> dict:
    url = os.environ.get("AI_DESKTOP_VISION_URL", DEFAULT_URL).rstrip("/")
    try:
        timeout = float(os.environ.get("AI_DESKTOP_VISION_TIMEOUT", "180"))
    except ValueError:
        timeout = 180.0
    return {"url": url, "openai": url.endswith("/v1"), "model": os.environ.get("AI_DESKTOP_VISION_MODEL", ""), "timeout": timeout}


def _http(url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AD.CliError(f"the vision model server answered {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AD.CliError(f"no local vision model server at {url.split('/api')[0].split('/v1')[0]} ({exc}); "
                          "start Ollama (ollama serve) or set AI_DESKTOP_VISION_URL") from exc
    except ValueError as exc:
        raise AD.CliError("the vision model server sent something that isn't JSON") from exc


def pick_model(cfg: dict) -> str:
    if cfg["model"]:
        return cfg["model"]
    if cfg["openai"]:
        names = [m.get("id", "") for m in _http(cfg["url"] + "/models", None, 10).get("data", [])]
    else:
        names = [m.get("name", "") for m in _http(cfg["url"] + "/api/tags", None, 10).get("models", [])]
    for name in names:
        if any(h in name.lower() for h in VISION_NAME_HINTS):
            return name
    raise AD.CliError("no vision model found (for example: ollama pull qwen2.5vl:3b), or set AI_DESKTOP_VISION_MODEL")


def model_size(width: int, height: int) -> tuple[int, int]:
    scale = (MODEL_PIXELS / (width * height)) ** 0.5
    return (max(MODEL_GRID, round(width * scale / MODEL_GRID) * MODEL_GRID),
            max(MODEL_GRID, round(height * scale / MODEL_GRID) * MODEL_GRID))


def _jpeg(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def ask(img, question: str) -> str:
    """One question about one image; the model's text answer."""
    cfg = settings()
    model = pick_model(cfg)
    if cfg["openai"]:
        payload = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _jpeg(img)}}]}]}
        answer = _http(cfg["url"] + "/chat/completions", payload, cfg["timeout"])
        return answer["choices"][0]["message"]["content"]
    payload = {"model": model, "stream": False, "options": {"temperature": 0},
               "messages": [{"role": "user", "content": question, "images": [_jpeg(img)]}]}
    return _http(cfg["url"] + "/api/chat", payload, cfg["timeout"])["message"]["content"]


def parse_point(text: str, width: int, height: int) -> tuple[int, int] | None:
    """{"x": .., "y": ..} (or a bare "x, y", or a 0-1000 normalised pair) from an answer."""
    match = re.search(r"\{[^{}]*\}", text)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = {}
        if data.get("found") is False:
            return None
        if "x" in data and "y" in data:
            try:
                x, y = float(data["x"]), float(data["y"])
            except (TypeError, ValueError):
                return None
            return _in_image(x, y, width, height)
        box = data.get("bbox_2d") or data.get("bbox") or data.get("box")
        if isinstance(box, list) and len(box) == 4:
            return _in_image((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, width, height)
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
    if len(numbers) >= 2:
        return _in_image(float(numbers[0]), float(numbers[1]), width, height)
    return None


def _in_image(x: float, y: float, width: int, height: int) -> tuple[int, int] | None:
    if 0 <= x <= 1 and 0 <= y <= 1 and (width > 2 or height > 2):  # fractions
        x, y = x * width, y * height
    elif (x > width or y > height) and x <= 1000 and y <= 1000:  # Qwen's 0-1000 grid
        x, y = x * width / 1000, y * height / 1000
    if 0 <= x < width and 0 <= y < height:
        return round(x), round(y)
    return None


def yes(text: str) -> bool:
    return bool(re.match(r"\W*(yes|yeah|correct|true)\b", text.strip().lower()))


def _closeup(cap, x: int, y: int):
    w, h = CLOSEUP
    left, top = max(0, x - cap.x - w // 2), max(0, y - cap.y - h // 2)
    box = (left, top, min(cap.image.width, left + w), min(cap.image.height, top + h))
    patch = cap.image.crop(box)
    big = patch.resize((patch.width * 2, patch.height * 2), AD.Image.Resampling.LANCZOS)
    # A crosshair marks the spot the question is about.
    from PIL import ImageDraw

    draw = ImageDraw.Draw(big)
    cx, cy = (x - cap.x - box[0]) * 2, (y - cap.y - box[1]) * 2
    for color, width in (((0, 0, 0), 5), ((255, 0, 0), 2)):
        draw.line([(cx - 18, cy), (cx + 18, cy)], fill=color, width=width)
        draw.line([(cx, cy - 18), (cx, cy + 18)], fill=color, width=width)
    return patch, big


def locate(cap, description: str) -> dict:
    """Find `description` on the screen capture `cap`: {"x", "y", ...} in screen coordinates,
    or {"found": False, "reason": ...}."""
    started = time.monotonic()
    remembered = _MEMORY.get(description.casefold())
    if remembered is not None:
        x, y, old_patch = remembered
        patch, _ = _closeup(cap, x, y)
        if patch.size == old_patch.size and AD.patch_diff(old_patch, patch, (patch.width // 2, patch.height // 2), radius=max(patch.size)) <= 0.05:
            return {"x": x, "y": y, "found_by": "vision", "remembered": True, "ms": 0}
        _MEMORY.pop(description.casefold(), None)
    img = cap.image
    size = model_size(*img.size)
    sent = img.resize(size, AD.Image.Resampling.LANCZOS)
    # The model's own grounding prompt: answers are faster and far more reliable than
    # when asked for a custom JSON shape.
    answer = ask(sent, f"Locate {description}, output its bbox coordinates using JSON format.")
    point = parse_point(answer, sent.width, sent.height)
    if point is None:
        return {"found": False, "reason": "the model did not find it", "ms": round((time.monotonic() - started) * 1000)}
    x = cap.x + round(point[0] * img.width / sent.width)
    y = cap.y + round(point[1] * img.height / sent.height)
    patch, big = _closeup(cap, x, y)
    if os.environ.get("AI_DESKTOP_VISION_VERIFY", "").strip().lower() in ("0", "off", "no", "false"):
        _MEMORY[description.casefold()] = (x, y, patch)
        return {"x": x, "y": y, "found_by": "vision", "verified": False, "ms": round((time.monotonic() - started) * 1000)}
    check = ask(big, f'Is the red cross on {description}? Answer yes or no.')
    ms = round((time.monotonic() - started) * 1000)
    if not yes(check):
        return {"found": False, "reason": f"the close-up check disagreed (the model first said {x},{y})", "candidate_x": x,
                "candidate_y": y, "ms": ms}
    _MEMORY[description.casefold()] = (x, y, patch)
    return {"x": x, "y": y, "found_by": "vision", "verified": True, "ms": ms}


def locate_target(backend, description: str) -> dict | None:
    """For click --target: a point, or None when the model can't vouch for one."""
    found = locate(AD.capture_screen(backend, "all"), description)
    if not found.get("x") and found.get("found") is False:
        return None
    return {k: v for k, v in found.items() if k in ("x", "y", "found_by", "verified", "remembered", "ms")}


def describe(img, focus: str = "") -> str:
    question = ("Describe what this part of a screen shows in one or two short sentences: the window or dialog, "
                "its main text, and any buttons." if not focus else focus)
    return " ".join(ask(img, question).split())[:600]


def status() -> dict:
    """For doctor: is a model reachable, which one, and roughly how much memory it needs."""
    cfg = settings()
    model = pick_model(cfg)
    info = {"url": cfg["url"], "model": model}
    if not cfg["openai"]:
        for m in _http(cfg["url"] + "/api/tags", None, 10).get("models", []):
            if m.get("name") == model:
                info["size_gb"] = round(m.get("size", 0) / 1e9, 1)
    return info
