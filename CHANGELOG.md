# Changelog

## 0.3.0 (unreleased)

- **Window management.** `windows` lists open windows front-to-back with their global
  coordinates; `focus --title|--app|--pid|--id` brings one to the front (Win32, macOS via
  Quartz/AppKit, Linux X11 via libX11 EWMH). Under GNOME Wayland only X11/XWayland
  windows are visible: GNOME offers no public API for native Wayland windows, and the
  output says so.
- **Action log.** Every command appends one JSON line to
  `~/.ai-desktop/logs/actions-YYYYMMDD.jsonl` (command, arguments, result, exit code,
  duration, `cli`/`mcp`). Typed, pasted and copied text is reduced to a character count
  unless `AI_DESKTOP_LOG_TEXT=1`; `AI_DESKTOP_LOG=0` disables the log. `log [--limit N]`
  shows recent entries; log files follow the screenshot retention rules.
- **Multi-monitor.** `monitors` lists displays with global coordinates. `screenshot`
  captures all monitors by default, `--monitor N|primary` one of them; grid labels are
  global screen coordinates (negative on a monitor left of the primary) and bounds checks
  accept any point on any monitor. `position` reports the monitor under the pointer.
- **MCP server.** `ai-desktop mcp` (install with `pip install 'ai-desktop[mcp]'`, Python
  3.10+) exposes every command as a tool over stdio; the `screenshot` tool returns the PNG
  inline, downscaled to `--max-width` (default 1920) with enlarged labels when needed.
- `launch-chrome` on Linux goes through the desktop entry (`gtk-launch`/`gio launch`);
  GNOME still opens the window behind the terminal and shows an "is ready" notification.
- `main()` is a thin wrapper over `execute(argv, quiet=False)`, which returns the result
  payload; `status()` and `fail()` keep the last payload in `LAST_PAYLOAD`.

## 0.2.0 (2026-10-12)

- **Screenshot store.** `screenshot` saves timestamped files under
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
