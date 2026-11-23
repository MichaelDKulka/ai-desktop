# Changelog

## 0.6.0 (unreleased)

- **Background service.** The first action or screenshot starts a per-user background
  process (`ai_desktop_service.py`), and later commands hand their work to it over a Unix
  socket in a folder only you can open (Linux also checks the caller's uid). It keeps the
  virtual mouse and keyboard open, so on Wayland only the first action pays the 0.5 s
  device delay. Output, exit codes, the stop switch and the action log are as before
  (`via=cli/service`); Ctrl+C in the client stops the action at its next pause. It never
  makes a command fail: if it is missing, busy, from another session or running older
  code, the command runs by itself, and old code is replaced. `ai-desktop service
  status|start|stop`; `AI_DESKTOP_SERVICE=off`; it exits after `AI_DESKTOP_SERVICE_IDLE`
  seconds (default 1800) without a request. Linux and macOS only.
- Fonts for grid labels are loaded once per size instead of for every screenshot.

## 0.5.0 (unreleased)

- **Stop switch.** `ai-desktop stop [--reason TEXT]` halts ai-desktop on every platform,
  Wayland included: every action refuses to start, and a running one aborts at its next
  pause (glide step, typed character, drag step, scroll notch), all with exit code 3.
  Looking (screenshot, windows, log, doctor, ...) still works. `ai-desktop resume` lifts it
  and only works from a terminal, so an agent can't release the human's brake; `resume`
  can't be a `run` step. `doctor` shows the state.
- **The keyboard `--seen` check catches dialogs.** `key`, `hotkey`, `type` and `paste`
  only refused when more than 10% of the screen changed, so a 600x300 dialog on a
  1920x1080 screen (8.7%) passed and got the keystroke. Any connected changed area of at
  least 200x150 px now refuses too, with its `region` in the error; toasts still pass.
- **MCP: text that looks like JSON is passed as text.** The SDK JSON-decodes arguments that
  aren't typed plain `str`, so `paste(text="null")` pasted whatever was already on the
  clipboard and `paste(text="[1, 2]")` was rejected. Optional text parameters are now plain
  `str`, where an empty string means "not given".
- **The right monitor is primary on multi-monitor Linux.** In `xrandr --listmonitors` the
  primary is marked `*`; `+` (read as primary until now) is on every automatic monitor,
  so index 0 and `--monitor primary` were simply the top-left monitor.
- **A capital letter in a hotkey no longer adds Shift.** `hotkey --keys Ctrl+C` sent
  Ctrl+Shift+C (Chrome's inspector) and `Ctrl+W` closed the whole window. With ctrl, alt
  or cmd in the combination, letters are taken as lower case; write `shift` to add it.
- **pyautogui drags.** A drag aborted by the corner failsafe left the mouse button held
  (pyautogui's `mouseUp` checks the failsafe first and raised again); the release now
  bypasses it. The held move uses `dragTo`, which macOS needs to send drag events.
- **`scroll --x --y` on Windows** moves the pointer there first; pyautogui's Windows scroll
  ignores the coordinates, so the pane under the old pointer position scrolled.
- **Private store.** Screenshots of the whole desktop, the action log, the pointer memory
  and the stop file were readable by every local user (folders 775, files 664). Files are
  now created 0600 and the tool's folders 0700, including an existing `~/.ai-desktop`; a
  folder set with `AI_DESKTOP_HOME` keeps its own mode, and so does the folder of `-o`.
- **Values that begin with a dash.** `screenshot --zoom -1420,400` (a monitor left of the
  primary), `type --text -la`, `focus --title -draft` and the like were usage errors,
  because argparse read the value as an option. They work now, as in the MCP tools.
- **No tracebacks.** An unexpected error, or a capture or clipboard tool that hangs past
  its timeout, ended in a Python traceback with no `ERROR` line and no JSON. Now it is
  `ERROR internal error: ...` or `ERROR gnome-screenshot did not finish within 30 s ...`,
  exit 1; `AI_DESKTOP_DEBUG=1` adds the traceback on stderr.
- **One-line argument errors in `run` and MCP.** A bad step or tool argument returned
  argparse's whole usage block; it is now the reason plus `(see: ai-desktop click --help)`.
  A `run` step with an unbalanced quote no longer copies the step's text, which is often
  typed text, into the error and the action log. A `--file` that isn't UTF-8 is exit 2.
- **`--seen` checks closer to the action.** The check used to run before the virtual
  input device was created (0.5 s on Wayland) and before a glide of up to 1.5 s, so the
  screen could change after it. With `--seen` the device is opened first and the glide
  takes at most 0.4 s.
- **Straight drags on Wayland.** Each pointer position went out as two input frames (the
  new x with the old y, then the new y), so drags were staircases; x and y now arrive
  together.
- **`copy` never loses the clipboard.** It parks a marker on the clipboard to tell a fresh
  copy from stale text, but restored the user's text only when nothing was copied: a
  failing key press left `ai-desktop-copy-...` behind. Now it is restored whatever goes
  wrong, and the keyboard is opened before the clipboard is touched at all.
- **Clipboard tool per desktop.** On KDE and wlroots Wayland sessions `wl-copy` and
  `wl-paste` now come before `xclip`, whose clipboard those compositors share only while an
  X11 window has focus; GNOME keeps `xclip` first. wl-clipboard is asked for plain text, so
  an image on the clipboard no longer comes back as bytes.
- **The monitor layout is detected once per command** instead of up to eight times (each
  an `xrandr` run on Linux).
- **Screenshot tools that suit the desktop, one after another.** Only the first installed
  tool was ever run, whether or not it worked there (gnome-screenshot on KDE captures
  Xwayland's empty root; grim needs wlroots). Now the tools that suit the session are tried
  in turn (GNOME: gnome-screenshot; KDE: spectacle; Sway, Hyprland: grim, then spectacle),
  then on X11 Pillow's own capture, and the error lists every failure.
- **Wayland started from a text console is Wayland.** A compositor started without a
  display manager inherits `XDG_SESSION_TYPE=tty`, so `auto` picked pyautogui, whose input
  never reaches native Wayland windows. `WAYLAND_DISPLAY` now decides unless the session is
  `x11`.
- **A screenshot and its `.raw.png` copy are pruned together**, judged by the newer of the
  two, so `--seen` never finds half a pair.
- **`launch-chrome` checks what it is given.** A URL without a scheme (`example.com`) gets
  `https://` (`http://` for localhost); desktop launchers opened it as a missing local
  file. A `--url` or `--profile` that looks like an option is refused. An unknown
  `--profile` is refused with the list of profiles, where Chrome would silently create a
  new, signed-out one; a display name (`Work`) is mapped to its folder (`Profile 2`). On
  macOS `--profile` works while Chrome is already running. The desktop entry is tried
  before the binary (a Flatpak Chrome has none on PATH), and a launcher that fails falls
  back to starting Chrome directly instead of reporting OK.
- **MCP: cancelling a call stops it.** Tools ran on a worker thread the SDK can't stop, so a
  cancelled or timed-out `type` kept typing and calls queued behind it still ran. Tools
  are now async with a cancel token per call: the action stops at its next pause (exit
  130, "cancelled by the client"), and a call cancelled while waiting its turn never starts.
- **MCP: failures carry their payload.** A failed call's error is the whole JSON payload,
  not just the message, so the finished steps of `run`, the `region` of a `--seen`
  refusal and the like reach the model. `wait(stable=false)` without `changed` was
  silently a `--stable` wait; it is now an error.
- **MCP: inline images have a size budget.** Screenshots over about 900 KB as PNG go inline
  as JPEG (smaller still if needed), since some clients reject results over 1 MB.
- **`run` checks every step before the first acts.** Only the argument syntax was checked
  up front, so non-ASCII text in a later `type` step, an unknown key, an off-screen point
  or a missing `--seen` file failed after earlier steps had already clicked. These checks
  now run for all steps first.
- **`wait --stable` waits for real quiet.** One matching pair of captures half a second
  apart counted as stable, so a spinner, or a page that hadn't started loading, passed.
  The screen must now be unchanged for `--quiet` seconds (default 1). New
  `wait --changed SCREENSHOT --settle` waits for a change and then for quiet; the MCP
  `wait` tool has `settle` and `quiet`.
- **Output can't break.** Text output is UTF-8 whatever the console's encoding, so a
  window title or copied text with `•`, `✓` or CJK no longer crashes on a Windows pipe
  (cp1252) after the action ran. A `key=value` value with a quote, `=` or a newline is
  quoted with those escaped, so it stays on its line; backslashes in Windows paths are
  kept as they are.
- **Windows: PowerShell by its full path.** Windows looks in the current folder before
  System32 for a bare `powershell`, so a `powershell.exe` in the agent's working folder
  ran on every clipboard call.
- **macOS: `focus` checks the window is in front.** It reported success when the app
  accepted the request, even with another of its windows in front.
- **X11 window list: dialogs are listed, sizes are what you see.** Windows marked
  skip-taskbar were dropped, and GNOME marks every dialog with a parent that way, so a
  Save or Preferences dialog could be neither listed nor focused. Windows are now filtered
  by their declared type (docks, desktops, menus, tooltips and notifications are left
  out), and each has a `type` (`normal`, `dialog`, `utility`). GTK windows that draw their
  own decorations no longer include their invisible shadow margin in x, y, width, height.
- **`grid` output keeps its screen coordinates.** `grid` saved no metadata, so re-gridding
  its output started the labels at (0, 0): image pixels, 1920 px off for a monitor left of
  the primary. It now records origin, scale and spacing, and a link to the original
  un-gridded copy, which a later `grid --input` draws on.
- **The corner failsafe watches all four corners** of the primary screen, as documented;
  pyautogui alone only watched the top-left one.
- **pyautogui no longer needs tkinter.** On Linux without `python3-tk`, pyautogui's
  `mouseinfo` import called `sys.exit()`, so every input command ended with a note instead
  of an `ERROR` line. `mouseinfo` is replaced by an empty module.
- **Human-like input.** `click`, `move` and `scroll --x/--y` travel along a random curved
  path (at most 1.5 s) that ends exactly on the target, stays on a monitor and never
  crosses a failsafe corner; `type` sends one key at a time with random 0.1–0.15 s pauses
  and 0.25–0.5 s after each word (about 8 s for a 50-character sentence). `--instant` or
  `AI_DESKTOP_HUMAN=0` restores instant input; `--seed`/`AI_DESKTOP_SEED` makes it
  repeatable. On Wayland the last pointer position is kept in `pointer.json`.
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
- **`drag` follows the other pointer commands.** It glides to its start point along the
  human-like path (`--instant` jumps), takes `--seen` (the areas around both points must be
  unchanged) and `--shot`, and on the uinput backend clamps to the axis range and
  remembers where the pointer ended.
- **`run` command strings are split the same way on every OS.** On Windows a quote inside a
  word (`it's`) used to be an ordinary character and `--text="a b"` became two arguments;
  now only the backslash escape is off there, so paths keep their backslashes.
- **CI and releases.** CI covers Python 3.10 and 3.14 on all three systems and 3.9 on
  Ubuntu 24.04 and macOS, lints, and fails below 80% test coverage; the live checks
  (screenshots on Windows and macOS, a real move and screenshot under Xvfb on Linux) now
  fail the build instead of being informational. A release runs the whole CI first, then
  checks the metadata and installs the built wheel before publishing. The test suite has a
  tripwire: opening `/dev/uinput`, running a desktop program or grabbing the real screen
  fails the test.
- **Packaging.** The version is written only in `__version__` (`pyproject.toml` reads it, and
  the release workflow checks the tag against it); `main` carries `.dev0` between releases.
  The sdist now ships `tests/conftest.py`: without it the store tests ran against the real
  `~/.ai-desktop`.

## 0.4.0 (2026-10-13)

- **`drag`.** `drag --x1 --y1 --x2 --y2 [--duration 0.5] [--button left|right|middle]`
  presses at the start point, moves to the end over `--duration` seconds and releases
  (the button is released even if the move fails). Both points are bounds/failsafe
  checked before anything moves. Works on pyautogui and the Wayland uinput backend, is
  logged like every command, and is an MCP tool.
- **Monitor-relative coordinates.** `click`, `move`, `drag` and `scroll` accept
  `--monitor N|primary`; x/y are then relative to that monitor's top-left and must lie
  inside it. Global coordinates stay the default. The MCP `click`, `move`, `drag` and
  `scroll` tools take an optional `monitor`.
- **Demo.** `docs/demo.gif` in the README, recorded by `docs/record_demo.py`.

## 0.3.0 (2026-10-13)

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
