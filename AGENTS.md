# Instructions for AI agents: controlling this computer with `ai-desktop`

You can see and control this computer's desktop with the `ai-desktop` command-line tool.
If `ai-desktop` is not on PATH, run `.venv/bin/ai-desktop` (Windows: `.venv\Scripts\ai-desktop`)
or `python3 ai_desktop.py` from this folder. Check it works first: `ai-desktop --version`.

## The loop

1. `ai-desktop screenshot --grid` prints `path=...`. Open that image file and look at it.
2. Read the target's pixel coordinates off the labelled grid. Image pixels are click coordinates.
3. Do ONE action: `click`, `move`, `type`, `key`, `hotkey`, `scroll`, `paste --text`, `copy`.
4. Take a new screenshot and confirm the action worked before doing the next one.

- Lost the path? `ai-desktop screenshots latest`.
- Target small or between grid lines? `ai-desktop screenshot --grid --spacing 50`.
- Want a fixed path? `ai-desktop screenshot --grid -o /tmp/screen.png`.

## Commands

```text
ai-desktop screenshot [--grid] [--spacing 100] [-o PATH]
ai-desktop click --x N --y N [--button left|right|middle] [--double]
ai-desktop move --x N --y N
ai-desktop type --text "ascii text"
ai-desktop key --key enter
ai-desktop hotkey --keys ctrl+c
ai-desktop scroll [--direction up|down] [--amount 3] [--x N --y N]
ai-desktop copy                      # press copy, then print what was copied
ai-desktop paste [--text "any text"] # put text on the clipboard, then press paste
ai-desktop clipboard get|set [--text "..."]
ai-desktop position
ai-desktop launch-chrome [--url URL] [--profile "Profile 1"]
ai-desktop screenshots dir|latest|list|clean
ai-desktop doctor
```

## Output contract

- Success: one line on stdout, `OK <command> key=value ...`.
- Failure: `ERROR <message>` on stderr. Exit codes: `1` error, `2` bad arguments,
  `3` the human aborted with the mouse-corner failsafe: **stop and ask them**.
- Add `--json` (anywhere on the line) or set `AI_DESKTOP_JSON=1` for one JSON object instead:
  `{"ok": true, "command": "click", ...}` or `{"ok": false, "error": "...", "code": 1}`.
- `copy` and `clipboard get` print the text after the first line (JSON: in `"text"`).

## Typing and keys

- `type --text` handles printable ASCII only. For anything else (accents, emoji, long
  passages) use `paste --text "..."`.
- Key names: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `up`, `down`, `left`,
  `right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, `ctrl`, `shift`, `alt`, `cmd`
  (same as `win`). Combine with `+`: `hotkey --keys ctrl+shift+t`.
- On macOS the copy shortcut is `cmd+c`; on Windows and Linux it is `ctrl+c`. The `copy`
  and `paste` commands pick the right one for you.

## Chrome

`ai-desktop launch-chrome --url URL` opens the user's own signed-in Chrome, so their logins
and cookies are available. The window may open behind other windows; on GNOME a
"... is ready" notification appears, click it. Take a screenshot to check what happened.

## Rules

- Never type passwords, 2FA codes or payment details. Stop and ask the human.
- Ask before anything public or irreversible: posting, sending, buying, deleting, accepting
  terms, signing in to a new service.
- One action per screenshot. If something unexpected is on screen (a dialog, a login page,
  an error), stop and describe it instead of guessing.
- Do not click the exact corners of the screen; on Windows, macOS and Linux X11 that
  triggers the failsafe and aborts the command.

## Developing this repo

`pip install -e ".[dev]"`, then `pytest`. The tests run headless and never touch the real
desktop. `ai-desktop doctor` reports what this platform still needs. See README.md.
