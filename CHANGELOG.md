# Changelog

## 0.3.0 (unreleased)

- **Human-like input.** `click`, `move` and `scroll --x/--y` travel along a random curved
  path (at most 1.5 s) that ends exactly on the target, stays on a monitor and never
  crosses a failsafe corner; `type` sends one key at a time with random 0.1–0.15 s pauses
  and 0.25–0.5 s after each word (about 8 s for a 50-character sentence). `--instant` or
  `AI_DESKTOP_HUMAN=0` restores instant input; `--seed`/`AI_DESKTOP_SEED` makes it
  repeatable. On Wayland the last pointer position is kept in `pointer.json`.
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
  3.10+) exposes every command as a tool over stdio; the `screenshot` tool returns the saved
  PNG inline, shrunk to `--max-width` (default 1920) with enlarged labels when needed.
- **Grid overhaul.** Lines are a magenta core over a dark outline, visible on light, pink,
  dark and busy content, with unlabelled ticks halfway between them. X labels sit at the
  top and bottom, y labels at the left and right, and interior `x,y` landmarks mark some
  crossings. Labels are thinned by their global value and sized to stay legible after a
  vision model shrinks the image to about 1568 px, also on big screens and multi-monitor
  images. `--spacing` defaults to `auto`: 100 on ordinary screens, 200 or more on big ones
  and multi-monitor images, finer in a zoom.
- **Zoom.** `screenshot --zoom X,Y [--radius 200] [--factor 3]` shows the area around a
  point enlarged (from physical pixels on HiDPI) with a fine grid whose labels are still
  global screen coordinates; it implies `--grid` and stays on the point's monitor.
- **`--max-width`** on `screenshot` and `grid` (default 1920, or `AI_DESKTOP_MAX_WIDTH`;
  `0` = never) shrinks wider images in the core, with lines and labels drawn bigger first.
  The OK line reports `image_scale` (image px per screen px).
- **Pointer in screenshots.** `--cursor auto|marker|none` (default `auto`): the capture
  tool draws the real pointer (`gnome-screenshot -p`, `grim -c`, `spectacle -p`, macOS
  `screencapture -C`); where it can't, a `ptr` crosshair is drawn (`ptr?` at the position
  remembered on Wayland). New OK fields `cursor`, `pointer`, `pointer_x`, `pointer_y`.
- **Un-gridded copies.** Every screenshot also saves `<name>.raw.png` with an `ai-desktop`
  PNG text chunk (origin, size, monitor, pointer). It is the reference for `--seen` and
  `wait --changed`, hidden from `screenshots latest`/`list` and pruned with its
  screenshot; `-o` files keep theirs next to them, never pruned. Store names are now
  `YYYYmmdd-HHMMSS-mmm[-N][-mM][-zoom][-gridS][.raw].png`, and `-o` may not end in
  `.raw.png`. `grid --input` re-grids a tool screenshot from its `.raw.png` with the global
  origin.
- **Stale-screen guard.** `--seen SCREENSHOT|latest` on `click`, `move`, `scroll`, `type`,
  `key`, `hotkey` and `paste` re-captures before acting and aborts (exit 1, nothing done)
  if the screen changed since that screenshot: the 129x129 px area around the target
  (limit 5%) for pointer commands, a coarse whole-screen fingerprint (limit 10%) for the
  rest. The OK line gains `seen` and `seen_diff`.
- **`--shot [MONITOR]`** on the same commands takes a gridded screenshot after acting (0.4 s
  later, `AI_DESKTOP_SHOT_DELAY`) and adds `shot=<path>`; a failed capture shows as
  `shot=error` and never fails the action.
- **`wait`** `--stable` (two captures in a row match) or `--changed SCREENSHOT|latest`
  (more than 2% differs), with `--timeout` (default 10 s, at most 300), `--interval`,
  `--monitor` and `--shot` (the last capture becomes the screenshot).
- **`run`** executes a JSON list of steps (argument lists or command strings) in one
  process, from `--steps` or `--file PATH|-`: every step is checked first, it stops at the
  first failure with that step's exit code, and each step is logged as its own entry
  (`via` `cli/run` or `mcp/run`).
- **Wayland input devices are reused** across the steps of `run` and for the MCP server's
  lifetime, so only the first action of each kind waits `AI_DESKTOP_UINPUT_DELAY`: three
  moves took 1.0 s as one `run` and 2.45 s as three commands.
- **MCP tools.** `screenshot` returns the saved file itself (its `max_width` shrinks that
  file) and gains `zoom` and `cursor`; its payload reports `width`, `height` and
  `image_scale` in place of `inline_width`, `inline_height` and `inline_scale`. `click`,
  `move`, `scroll`, `type`, `key`, `hotkey` and `paste` gain `seen` and `shot` (the new
  screenshot comes back inline); new tools `wait` (timeout at most 30 s) and `run`.
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
