"""Record docs/demo.gif: ai-desktop driving a real window on one monitor.

Run on Windows from the repo root: python docs/record_demo.py [monitor-index]
The script opens its own scratch editor window over a plain, always-on-top backdrop,
so none of your other windows (or a session-restoring Notepad) can end up in the GIF.
All input to the editor comes from the ai-desktop CLI.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageGrab

MONITOR = sys.argv[1] if len(sys.argv) > 1 else "1"
EXE = str(Path(sys.executable).with_name("ai-desktop.exe"))
OUT = Path(__file__).with_name("demo.gif")
FPS = 6
WIDTH = 960

captions: list[tuple[float, str]] = []
frames: list[tuple[float, Image.Image]] = []
stop = threading.Event()


def ai(*args: str) -> str:
    captions.append((time.monotonic(), "$ ai-desktop " + " ".join(args)))
    out = subprocess.run([EXE, *args], capture_output=True, text=True, check=True).stdout
    time.sleep(0.6)
    return out


def monitor_rect() -> tuple[int, int, int, int]:
    out = subprocess.run([EXE, "monitors"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts[0] == MONITOR:
            x, y = map(int, parts[2].split(","))
            w, h = map(int, parts[3].split("x"))
            return x, y, w, h
    raise SystemExit(f"no monitor {MONITOR}")


def grab(bbox):
    while not stop.is_set():
        t = time.monotonic()
        frames.append((t, ImageGrab.grab(bbox=bbox, all_screens=True)))
        time.sleep(max(0, 1 / FPS - (time.monotonic() - t)))


STAGE = r'''
import sys, tkinter as tk
x, y, w, h = map(int, sys.argv[1:5])
root = tk.Tk(); root.overrideredirect(True); root.geometry(f"{w}x{h}+{x}+{y}")
root.configure(bg="#1e2430"); root.attributes("-topmost", True)
tk.Label(root, text="ai-desktop demo", fg="#5b6b85", bg="#1e2430", font=("Segoe UI", 28)).place(relx=0.5, rely=0.9, anchor="center")
pad = tk.Toplevel(root); pad.title("Scratch pad - ai-desktop demo")
pad.geometry(f"1000x560+{x + 160}+{y + 140}"); pad.attributes("-topmost", True)
text = tk.Text(pad, font=("Consolas", 18), wrap="word", undo=True, padx=12, pady=10)
text.pack(fill="both", expand=True)
pad.protocol("WM_DELETE_WINDOW", root.destroy)
root.after(300, lambda: (pad.lift(), pad.focus_force(), text.focus_set()))
root.mainloop()
'''

BG = (0x1E, 0x24, 0x30)


def main() -> None:
    mx, my, mw, mh = monitor_rect()
    bbox = (mx, my, mx + mw, my + mh)
    stage = subprocess.Popen([sys.executable, "-c", STAGE, str(mx), str(my), str(mw), str(mh)])
    time.sleep(2.0)
    ai("focus", "--title", "Scratch pad - ai-desktop demo")
    time.sleep(0.5)
    probe = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
    for px in ((5, 5), (mw - 5, 5), (5, mh - 5), (mw - 5, mh - 5), (60, 600), (mw - 60, 100)):
        if max(abs(a - b) for a, b in zip(probe.getpixel(px), BG)) > 8:
            stage.terminate()
            raise SystemExit(f"backdrop not covering the monitor at {px}; aborting before recording")

    grabber = threading.Thread(target=grab, args=(bbox,), daemon=True)
    grabber.start()
    time.sleep(1.0)
    ai("click", "--monitor", MONITOR, "--x", "600", "--y", "420")
    ai("type", "--text", "Hello from ai-desktop", "--interval", "0.06")
    ai("key", "--key", "enter")
    ai("type", "--text", "Every keystroke, click and drag here came from the CLI.", "--interval", "0.03")
    ai("move", "--monitor", MONITOR, "--x", "700", "--y", "300")
    # Drag the window by its title bar, in coordinates relative to this monitor.
    ai("drag", "--monitor", MONITOR, "--x1", "560", "--y1", "155", "--x2", "1100", "--y2", "420", "--duration", "1.5")
    ai("drag", "--monitor", MONITOR, "--x1", "1100", "--y1", "420", "--x2", "620", "--y2", "235", "--duration", "1.2")
    ai("key", "--key", "enter")
    ai("type", "--text", "pip install ai-desktop", "--interval", "0.05")
    time.sleep(1.5)
    stop.set()
    grabber.join()
    stage.terminate()
    stage.wait(timeout=5)
    build_gif()

def build_gif() -> None:
    try:
        font = ImageFont.truetype("consola.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    out: list[Image.Image] = []
    for t, img in frames:
        img = img.convert("RGB")
        img = img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.Resampling.LANCZOS)
        caption = next((c for ct, c in reversed(captions) if ct <= t), "")
        if caption:
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, img.height - 40, img.width, img.height), fill=(12, 14, 20))
            draw.text((14, img.height - 32), caption, fill=(120, 230, 140), font=font)
        out.append(img.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
    out[0].save(OUT, save_all=True, append_images=out[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    print(f"{OUT} {len(out)} frames {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()

