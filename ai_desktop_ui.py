"""Find things on the screen by name, as text, without sending the agent a screenshot.

Two local sources, both answering with a line of text per thing found:

- the accessibility tree (AT-SPI on Linux, through ai_desktop_helper.py): buttons, links,
  fields and the like with their role, name and position; elements get references such
  as `e12`, kept in ~/.ai-desktop/ui-snapshot.json until the next snapshot;
- text recognition (Tesseract) of the screen: every phrase with its position.

On GNOME Wayland, apps that run natively on Wayland (Chrome, Firefox, GTK 4 apps) report
positions relative to their own window, and GNOME won't say where the window is. Then
the window's offset is found by matching element names against recognised text: when
at least three names sit at one consistent offset, every element of that window gets a
screen position. Without that, such elements are pressed through accessibility itself.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter

import ai_desktop as _ai_desktop

AD = _ai_desktop
SNAPSHOT_FILE = "ui-snapshot.json"
OCR_SCALE = 2  # Tesseract reads small UI text far better enlarged
OCR_MIN_CONF = 50
ALIGN_MIN = 3  # names that must agree on a window's offset
ALIGN_TOLERANCE = 6  # px
_OCR_CACHE: dict = {}  # image digest -> phrases (the service keeps it between commands)
_HELPER = None  # the service's long-lived helper


def bind(module) -> None:
    global AD
    AD = module


# --- text recognition ---------------------------------------------------------------------


def tesseract() -> str:
    exe = shutil.which("tesseract")
    if exe is None:
        raise AD.CliError("text recognition needs Tesseract: sudo apt install tesseract-ocr (brew install tesseract on macOS)")
    return exe


def ocr_words(img) -> list[dict]:
    """Words with confidence and position in `img` pixels."""
    big = img.resize((img.width * OCR_SCALE, img.height * OCR_SCALE), AD.Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    big.save(buf, "PNG", compress_level=1)
    try:
        result = subprocess.run([tesseract(), "stdin", "stdout", "--psm", "11", "tsv"], input=buf.getvalue(),
                                capture_output=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise AD.CliError("tesseract did not finish within 60 s") from exc
    if result.returncode != 0:
        raise AD.CliError(f"tesseract failed: {result.stderr.decode(errors='replace').strip()[-200:]}")
    words = []
    for row in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
            left, top, width, height = (int(v) // OCR_SCALE for v in cols[6:10])
        except ValueError:
            continue
        if conf >= OCR_MIN_CONF:
            words.append({"text": cols[11].strip(), "conf": conf, "x": left, "y": top, "w": max(1, width), "h": max(1, height)})
    return words


def group_phrases(words: list[dict]) -> list[dict]:
    """Words on one line with small gaps between them become one phrase."""
    words = sorted(words, key=lambda w: (w["y"] + w["h"] / 2, w["x"]))
    lines: list[list[dict]] = []
    for word in words:
        mid = word["y"] + word["h"] / 2
        for line in lines:
            last = line[-1]
            same_row = abs((last["y"] + last["h"] / 2) - mid) <= max(last["h"], word["h"]) * 0.5
            gap = word["x"] - (last["x"] + last["w"])
            if same_row and -2 <= gap <= max(last["h"], word["h"]) * 1.2:
                line.append(word)
                break
        else:
            lines.append([word])
    phrases = []
    for line in lines:
        left = min(w["x"] for w in line)
        top = min(w["y"] for w in line)
        right = max(w["x"] + w["w"] for w in line)
        bottom = max(w["y"] + w["h"] for w in line)
        phrases.append({"text": " ".join(w["text"] for w in line), "x": left, "y": top, "w": right - left, "h": bottom - top,
                        "conf": round(min(w["conf"] for w in line))})
    phrases.sort(key=lambda p: (p["y"], p["x"]))
    return phrases


def screen_text(cap, region=None) -> list[dict]:
    """Phrases on the screen (or in `region`), in screen coordinates, with ids t1, t2, ..."""
    img, ox, oy = cap.image, cap.x, cap.y
    if region is not None:
        x, y, w, h = region
        box = (max(0, x - ox), max(0, y - oy), min(img.width, x - ox + w), min(img.height, y - oy + h))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise AD.CliError(f"--region {x},{y},{w},{h} is outside the screen")
        img, ox, oy = img.crop(box), ox + box[0], oy + box[1]
    digest = hashlib.blake2b(img.tobytes(), digest_size=16).hexdigest() + f"{ox},{oy}"
    if digest not in _OCR_CACHE:
        phrases = group_phrases(ocr_words(img))
        for p in phrases:
            p["x"] += ox
            p["y"] += oy
        if len(_OCR_CACHE) > 8:
            _OCR_CACHE.clear()
        _OCR_CACHE[digest] = phrases
    return [{**p, "id": f"t{i}"} for i, p in enumerate(_OCR_CACHE[digest], 1)]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def find_text(phrases: list[dict], query: str) -> list[dict]:
    """Phrases equal to `query` (ignoring case and spacing), else those containing it."""
    q = norm(query)
    exact = [p for p in phrases if norm(p["text"]) == q]
    return exact or [p for p in phrases if q and q in norm(p["text"])]


# --- accessibility ------------------------------------------------------------------------


def a11y_supported() -> str | None:
    if not sys.platform.startswith("linux"):
        return "the accessibility tree is supported on Linux so far (Windows UI Automation and macOS AX are not done yet)"
    return None


def helper():
    """The service's helper is kept; a command-line call gets a fresh one."""
    global _HELPER
    live = AD.live_module()
    if AD.IN_SERVICE:
        if _HELPER is None or _HELPER.proc.poll() is not None:
            _HELPER = live.HelperProcess()
        return _HELPER, False
    return live.HelperProcess(), True


def ask(request: dict) -> dict:
    why = a11y_supported()
    if why:
        raise AD.CliError(why)
    proc, temporary = helper()
    try:
        head, _ = proc.ask(request)
    finally:
        if temporary:
            proc.close()
    if not head.get("ok") and head.get("error", "").startswith("ModuleNotFoundError"):
        raise AD.CliError("the accessibility helper needs the system's python3-gi and gir1.2-atspi-2.0")
    return head


def coordinates_are_relative(window: dict) -> bool:
    """Native Wayland apps report SCREEN extents equal to their WINDOW extents, relative to
    the window. XWayland apps (their SCREEN differs) and GNOME Shell itself are global."""
    if not AD.is_wayland() or window.get("toolkit") in ("clutter", "gnome-shell") or window.get("app") == "gnome-shell":
        return False
    screen, inner = window.get("screen"), window.get("window")
    return bool(screen and inner and screen[:2] == inner[:2] == [0, 0])


def align(elements: list[dict], phrases: list[dict]) -> tuple[int, int] | None:
    """The offset that puts the most element names onto recognised text of the same name."""
    names = Counter(norm(e["name"]) for e in elements if len(norm(e["name"])) >= 3)
    offsets = []
    for e in elements:
        key = norm(e["name"])
        if len(key) < 3 or names[key] != 1:
            continue
        matches = [p for p in phrases if norm(p["text"]) == key]
        if len(matches) != 1:
            continue
        p = matches[0]
        ex, ey, ew, eh = e["rect"]
        offsets.append((round(p["x"] + p["w"] / 2 - (ex + ew / 2)), round(p["y"] + p["h"] / 2 - (ey + eh / 2))))
    best, support = None, 0
    for dx, dy in offsets:
        n = sum(1 for ox, oy in offsets if abs(ox - dx) <= ALIGN_TOLERANCE and abs(oy - dy) <= ALIGN_TOLERANCE)
        if n > support:
            best, support = (dx, dy), n
    return best if support >= ALIGN_MIN else None


def snapshot(scope: str = "active", backend=None, cap=None) -> dict:
    """The accessibility snapshot with element references and, where possible, screen
    positions; saved for --ref and --mark in later commands."""
    tree = ask({"op": "a11y-tree", "scope": scope})
    if not tree.get("ok"):
        raise AD.CliError(f"accessibility query failed: {tree.get('error')}")
    windows, elements = tree["windows"], tree["elements"]
    placed = {}
    for index, window in enumerate(windows):
        if not coordinates_are_relative(window):
            placed[index] = ("screen", (0, 0))
            continue
        offset = None
        mine = [e for e in elements if e["window"] == index]
        if mine and shutil.which("tesseract"):
            try:
                cap = cap or AD.capture_screen(backend or AD.get_backend("auto"), "all")
                offset = align(mine, screen_text(cap))
            except AD.CliError:
                offset = None
        placed[index] = ("aligned", offset) if offset else ("window", (0, 0))
    out = []
    for i, e in enumerate(elements, 1):
        kind, (dx, dy) = placed[e["window"]]
        x, y, w, h = e["rect"]
        item = {**e, "id": f"e{i}", "where": kind}
        item["rect"] = [x + dx, y + dy, w, h] if kind != "window" else e["rect"]
        out.append(item)
    snap = {"taken": time.time(), "scope": scope, "windows": windows, "elements": out}
    try:
        with AD.private_file(AD.private_dir(AD.home_dir()) / SNAPSHOT_FILE) as fh:
            json.dump(snap, fh)
    except OSError:
        pass
    return snap


def load_snapshot() -> dict:
    try:
        return json.loads((AD.home_dir() / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AD.CliError("no accessibility snapshot yet; run: ai-desktop ui") from exc


def element_line(e: dict) -> str:
    x, y, w, h = e["rect"]
    where = {"screen": "", "aligned": " ~", "window": " (window-relative)"}[e["where"]]
    flags = [s for s in e.get("states", []) if s in ("focused", "checked", "selected", "expanded")]
    extra = (" " + ",".join(flags)) if flags else ""
    text = f" text={json.dumps(e['text'][:60], ensure_ascii=False)}" if e.get("text") else ""
    return f'{e["id"]} {e["role"]} {json.dumps(e["name"][:80], ensure_ascii=False)} {x},{y} {w}x{h}{where}{extra}{text}'


def by_ref(ref: str) -> dict:
    snap = load_snapshot()
    ref = ref if ref.startswith("e") else f"e{ref}"
    for e in snap["elements"]:
        if e["id"] == ref:
            return e
    raise AD.CliError(f"no element {ref} in the last snapshot; run: ai-desktop ui")


def by_name(name: str, role: str | None = None, scope: str = "active", backend=None) -> dict:
    snap = snapshot(scope, backend)
    q = norm(name)
    pool = [e for e in snap["elements"] if role is None or e["role"] == role]
    exact = [e for e in pool if norm(e["name"]) == q]
    found = exact or [e for e in pool if q and q in norm(e["name"])]
    if not found:
        raise AD.CliError(f"no element named {name!r}" + (f" with role {role!r}" if role else "") + " in the active window")
    if len(found) > 1 and not exact:
        listing = "; ".join(element_line(e) for e in found[:5])
        raise AD.CliError(f"{len(found)} elements match {name!r}; be more precise or use --ref: {listing}")
    if len(exact) > 1:
        listing = "; ".join(element_line(e) for e in exact[:5])
        raise AD.CliError(f"{len(exact)} elements are named {name!r}; use --role or --ref: {listing}")
    return found[0]


def act(element: dict, what: str, **extra) -> dict:
    """Do something to an element through accessibility, after checking it is the same."""
    answer = ask({"op": "a11y-do", "path": element["path"], "role": element["role"], "name": element["name"], "do": what, **extra})
    if not answer.get("ok"):
        if answer.get("error") == "stale":
            raise AD.CliError(f"stale reference {element['id']}: the screen changed ({answer.get('detail')}); "
                              "take a new snapshot (ai-desktop ui)")
        raise AD.CliError(f"{what} on {element['id']} failed: {answer.get('error')} {answer.get('detail', '')}".strip())
    return answer


def center(rect) -> tuple[int, int]:
    x, y, w, h = rect
    return x + w // 2, y + h // 2


def draw_marks(img, ox: int, oy: int, elements: list[dict]):
    """Numbered boxes on the elements whose screen position is known (Set-of-Mark)."""
    from PIL import ImageDraw

    base = img.convert("RGBA")
    overlay = AD.Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = AD.load_font(14)
    for e in elements:
        if e["where"] == "window":
            continue
        x, y, w, h = e["rect"]
        x, y = x - ox, y - oy
        if x + w < 0 or y + h < 0 or x > base.width or y > base.height:
            continue
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=(255, 140, 0, 230), width=2)
        AD._draw_label(draw, e["id"][1:], font, max(0, x), max(0, y - 18))
    return AD.Image.alpha_composite(base, overlay).convert("RGB")


def enable_accessibility(on: bool) -> dict:
    """Turn GNOME's accessibility switch on or off (apps such as Chrome and VS Code expose
    their content only when it is on; already running apps may need a restart)."""
    value = "true" if on else "false"
    done = {}
    if shutil.which("gsettings"):
        r = subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", value],
                           capture_output=True, text=True, timeout=10, env=AD.host_env())
        done["gsettings"] = r.returncode == 0
    if shutil.which("busctl"):
        r = subprocess.run(["busctl", "--user", "set-property", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status",
                            "IsEnabled", "b", value], capture_output=True, text=True, timeout=10, env=AD.host_env())
        done["a11y_bus"] = r.returncode == 0
    if not done:
        raise AD.CliError("neither gsettings nor busctl is installed")
    return done
