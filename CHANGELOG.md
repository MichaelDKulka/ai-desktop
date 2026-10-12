# Changelog

## 0.2.0 (unreleased)

- **Screenshot store.** `screenshot` now saves timestamped files under
  `~/.ai-desktop/screenshots/` (or `$AI_DESKTOP_HOME/screenshots`) instead of overwriting
  one file in the system temp folder. Files older than 30 days are deleted automatically,
  and the folder is kept under 2 GB. Only files the tool named itself are ever removed.
  Settings: `AI_DESKTOP_KEEP_DAYS`, `AI_DESKTOP_KEEP_MB`.
- **`screenshots` command:** `dir`, `latest`, `list [--limit N]`, `clean [--days N | --all]`.
- **`doctor` command** checks everything the current platform needs (Python, dependencies,
  screen capture, macOS Screen Recording and Accessibility permissions, Windows display
  scaling, Linux Wayland `/dev/uinput` access and screenshot tool, clipboard, Chrome)
  and prints a fix hint for each problem.
- **`--json`** (or `AI_DESKTOP_JSON=1`) makes every command print one JSON object, errors
  included. Global flags now work before or after the subcommand. **`--version`** added.
- **Linux X11** captures through `gnome-screenshot` (or `grim`/`spectacle`) when
  installed, falling back to Pillow's X11 capture (pyautogui's own screenshot refuses to
  run on Linux without gnome-screenshot).
- `setup.sh` / `setup.ps1` one-shot installers, `AGENTS.md` instructions for any AI agent,
  a headless test suite, CI on Windows/macOS/Linux, and a PyPI release workflow.

## 0.1.0 (2026-10-12)

- Gridded screenshots, mouse and keyboard control, clipboard (`copy`, `paste`,
  `clipboard`), signed-in Chrome launcher.
- Backends: pyautogui (Windows, macOS, Linux X11) and a stdlib-only uinput backend for
  Linux Wayland.
