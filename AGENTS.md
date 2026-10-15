# Instructions for AI agents: controlling this computer with `ai-desktop`

You can see and control this computer's desktop with the `ai-desktop` command-line tool.
If `ai-desktop` is not on PATH, run `.venv/bin/ai-desktop` (Windows: `.venv\Scripts\ai-desktop`)
or `python3 ai_desktop.py` from this folder. Check it works first: `ai-desktop --version`.
If your harness connects to it as an MCP server instead, the tools have the same names
and rules as the commands below.

## The loop

1. `ai-desktop screenshot --grid` prints `path=...`. Open that image file and look at it.
2. Read the target's coordinates off the labelled grid. **Labels are screen coordinates**:
   pass them to `click` unchanged, even when they are negative (a monitor left of the
   primary one) and even in a zoomed or shrunk image.
3. Do ONE action: `click`, `move`, `type`, `key`, `hotkey`, `scroll`, `paste --text`, `copy`.
   Add `--seen latest` (all but `copy`): the action is refused, doing nothing, if the screen
   changed since your screenshot. Add `--shot` to get the next screenshot from the same
   call; its path is printed as `shot=`.
4. Look at that new screenshot and confirm the action worked before doing the next one.

- Opened a page or an app? `ai-desktop wait --stable --shot` waits until the screen stops
  changing, then takes the screenshot.
- Lost the path? `ai-desktop screenshots latest`.
- Target small or between grid lines? `ai-desktop screenshot --zoom X,Y` shows the area
  around that point enlarged; its labels are still screen coordinates. (`--grid --spacing 50`
  draws finer lines over the whole screen.)
- More than one monitor? The default image shows all of them; `--monitor 1` (an index
  from `ai-desktop monitors`) zooms into one.
- Want a fixed path? `ai-desktop screenshot --grid -o /tmp/screen.png`.
- Not sure what is open? `ai-desktop windows`, then `ai-desktop focus --title "part of title"`
  before acting in that window. Take a screenshot afterwards to confirm it is in front.
- Want to review what you did? `ai-desktop log`.

## Commands

```text
ai-desktop screenshot [--grid] [--spacing auto|N] [--monitor all|primary|N] [-o PATH]
ai-desktop screenshot --zoom X,Y [--radius 200] [--factor 3]   # enlarged close-up
ai-desktop click --x N --y N [--button left|right|middle] [--double] [--instant]
ai-desktop move --x N --y N [--instant]
ai-desktop type --text "ascii text" [--instant]
ai-desktop key --key enter
ai-desktop hotkey --keys ctrl+c
ai-desktop scroll [--direction up|down] [--amount 3] [--x N --y N] [--instant]
ai-desktop copy                      # press copy, then print what was copied
ai-desktop paste [--text "any text"] # put text on the clipboard, then press paste
#   click, move, type, key, hotkey, scroll and paste also take:
#   --seen latest   abort, doing nothing, if the screen changed since that screenshot
#   --shot          take a gridded screenshot after acting; its path is printed as shot=
ai-desktop wait --stable | --changed latest [--timeout 10] [--shot]
ai-desktop run --steps '[["click","--x","500","--y","300"], "key --key enter"]' [--shot]
ai-desktop clipboard get|set [--text "..."]
ai-desktop position
ai-desktop windows                   # open windows, front to back, with coordinates
ai-desktop focus --title "..." | --app "..." | --pid N | --id ID
ai-desktop monitors
ai-desktop launch-chrome [--url URL] [--profile "Profile 1"]
ai-desktop screenshots dir|latest|list|clean
ai-desktop log [--limit 20]
ai-desktop doctor
```

## Output contract

- Success: one line on stdout, `OK <command> key=value ...`.
- Failure: `ERROR <message>` on stderr. Exit codes: `1` error, `2` bad arguments,
  `3` the human aborted with the mouse-corner failsafe: **stop and ask them**.
- Add `--json` (anywhere on the line) or set `AI_DESKTOP_JSON=1` for one JSON object instead:
  `{"ok": true, "command": "click", ...}` or `{"ok": false, "error": "...", "code": 1}`.
- `copy` and `clipboard get` print the text after the first line (JSON: in `"text"`).
- `windows`, `monitors`, `screenshots list` and `log` print one item per line after the
  first line (JSON: a list).

## Typing and keys

- `type --text` handles printable ASCII only. For anything else (accents, emoji, long
  passages) use `paste --text "..."`.
- Typing is paced like a person: about 0.1–0.15 s per character and 0.25–0.5 s after each
  space, so a 50-character sentence takes roughly 8 s. Use `paste --text` for more than
  a sentence or two, or add `--instant` for the fixed fast cadence.
- `click`, `move` and `scroll --x/--y` glide along a random curved path that lands exactly
  on the coordinates you gave. Add `--instant` to any command to jump straight there.
- Key names: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `up`, `down`, `left`,
  `right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, `ctrl`, `shift`, `alt`, `cmd`
  (same as `win`). Combine with `+`: `hotkey --keys ctrl+shift+t`.
- On macOS the copy shortcut is `cmd+c`; on Windows and Linux it is `ctrl+c`. The `copy`
  and `paste` commands pick the right one for you.

## Chrome and windows

- `ai-desktop launch-chrome --url URL` opens the user's own signed-in Chrome, so their
  logins and cookies are available. The window may open behind other windows; on GNOME a
  "... is ready" notification appears at the top of the screen: click it. Then screenshot.
- On Linux Wayland (GNOME) `windows` and `focus` only see X11 apps; `partial=true` in the
  output tells you so. Use the notification or the dock instead.

## Rules

- Never type passwords, 2FA codes or payment details. Stop and ask the human.
- Ask before anything public or irreversible: posting, sending, buying, deleting, accepting
  terms, signing in to a new service.
- One action per screenshot. If something unexpected is on screen (a dialog, a login page,
  an error), stop and describe it instead of guessing.
- If an action fails with "screen changed", nothing happened: take a new screenshot and
  read it again before acting.
- Use `run` only for short sequences you are sure of, and still look at the screen
  afterwards.
- Do not click the exact corners of the primary screen; on Windows, macOS and Linux X11
  that triggers the failsafe and aborts the command. The tool's own path never touches a
  corner, so a failsafe error means the human moved the mouse: stop and ask them.

## Developing this repo

`pip install -e ".[dev,mcp]"`, then `pytest`. The tests run headless and never touch the
real desktop. `ai-desktop doctor` reports what this platform still needs. Nothing may
print to stdout except through `status()` / `fail()`: when the tool runs as an MCP
server, stdout is the protocol channel. See README.md.
