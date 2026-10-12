# AI Desktop Controller

[![CI](https://github.com/MichaelDKulka/ai-desktop/actions/workflows/ci.yml/badge.svg)](https://github.com/MichaelDKulka/ai-desktop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A small command-line tool that lets **any AI agent that can run shell commands and look at images** see your desktop through a coordinate grid and control the mouse, keyboard and clipboard. That includes driving your real, already signed-in Chrome. One Python file, two dependencies, Windows / macOS / Linux (X11 and Wayland).

> **⚠️ Safety warning.** Anything that can run this tool has **full control of your desktop and of every account that is logged in on it**: email, banking, admin consoles, password managers. Run it only with an agent you trust, watch what it does, and keep sensitive sessions closed while it runs.
>
> - **Windows, macOS, Linux X11:** a *failsafe* is on. Slam the mouse into **any screen corner** and the current and every following command aborts with exit code 3, until you move the mouse away.
> - **Linux Wayland:** there is no corner failsafe, because Wayland doesn't let apps read the pointer position. Stop the agent process itself (Ctrl+C in its terminal).

## Install

**One-shot setup** (creates `.venv`, installs, offers the Linux extras, then runs `doctor`):

```bash
git clone https://github.com/MichaelDKulka/ai-desktop
cd ai-desktop
./setup.sh                                               # macOS, Linux
powershell -ExecutionPolicy Bypass -File setup.ps1       # Windows
```

**By hand:**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # or: pip install -r requirements.txt
ai-desktop doctor                  # tells you what your OS still needs
```

Or, without cloning: `pip install git+https://github.com/MichaelDKulka/ai-desktop` (and, once released, `pip install ai-desktop`).

Then finish the one-time steps for your platform under [Platform notes](#platform-notes). `ai-desktop doctor` lists exactly what is missing and how to fix it.

**Requirements:** Python 3.9+. The only Python dependencies are `pyautogui` and `pillow`; pip pulls in what they need per OS (pyobjc on macOS, python-xlib on Linux). Linux Wayland additionally needs `gnome-screenshot` (or `grim`/`spectacle`), `xclip` and write access to `/dev/uinput`, see below.

## Commands

```text
ai-desktop screenshot [--grid] [--spacing 100] [-o screenshot.png]
ai-desktop grid --input image.png [-o gridded.png] [--spacing 100]
ai-desktop click --x 500 --y 300 [--button left|right|middle] [--double]
ai-desktop move --x 500 --y 300
ai-desktop type --text "hello world" [--interval 0.02]
ai-desktop key --key enter
ai-desktop hotkey --keys ctrl+c
ai-desktop scroll [--direction up|down] [--amount 3] [--x N --y N]
ai-desktop copy
ai-desktop paste [--text "any text, even é or ✓"]
ai-desktop clipboard get|set [--text "..."]
ai-desktop position
ai-desktop launch-chrome [--url URL] [--profile "Profile 1"]
ai-desktop screenshots dir|latest|list [--limit 20]|clean [--days N | --all]
ai-desktop doctor
ai-desktop --version
```

Global flags, accepted before or after the command: `--json`, `--backend auto|pyautogui|uinput`.

**Output.** Every command prints one line an agent can parse, or an error on stderr:

```text
OK screenshot path=/home/me/.ai-desktop/screenshots/20261012-101905-123-grid100.png width=1920 height=1080 spacing=100 scale=2.0 backend=pyautogui pruned=0
OK click x=520 y=340 button=left clicks=1
ERROR (2000, 50) is off screen; valid range is x 0..1919, y 0..1079
```

With `--json` (or `AI_DESKTOP_JSON=1`) each command prints one JSON object instead, errors included:

```json
{"ok": true, "command": "click", "x": 520, "y": 340, "button": "left", "clicks": 1}
{"ok": false, "command": "click", "error": "(2000, 50) is off screen; ...", "code": 1}
```

Exit codes: `0` ok, `1` error, `2` bad arguments (plain argparse text, never JSON), `3` failsafe abort.

**Screenshot coordinates are click coordinates.** On HiDPI screens (a Retina Mac, or GNOME scaled to 200%) the capture is resized to the logical screen size, so a point you read off the gridded image is exactly what you pass to `click`. `scale=` reports the original ratio.

**Key names.**
- Named keys: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `up`/`down`/`left`/`right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, `ctrl`, `shift`, `alt`, and `cmd` (also `win`, `super`, `meta`).
- Any single character also works, e.g. `hotkey --keys ctrl+a`.
- Aliases such as `return`, `escape`, `control`, `option` and `pgdn` are accepted.
- Unknown keys are an error, never silently ignored.

## Screenshots

`screenshot` saves to the tool's own folder, **`~/.ai-desktop/screenshots/`** on every OS (Windows: `C:\Users\you\.ai-desktop\screenshots\`), as `YYYYMMDD-HHMMSS-mmm[-gridSPACING].png` in local time. The path is printed each time, and `screenshots latest` finds the newest one again.

**Retention.** Files are deleted automatically when they are **older than 30 days**, and the oldest go first whenever the folder exceeds **2 GB**. The check runs inside `screenshot`, at most once an hour, so there is no background service to install. Only files the tool named itself are ever removed; anything else you put in the folder, symlinks, and files written with `-o PATH` are never touched.

```bash
ai-desktop screenshots dir            # where, how many, how big, current limits
ai-desktop screenshots latest         # newest file
ai-desktop screenshots list --limit 5 # newest first, one path per line
ai-desktop screenshots clean          # apply the rules now
ai-desktop screenshots clean --days 7 # or stricter
ai-desktop screenshots clean --all    # empty the store
```

Why not the system temp folder: macOS purges `/tmp` files unused for three days and Linux usually clears it on reboot, so 30-day retention would be meaningless there.

**Settings** (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `AI_DESKTOP_HOME` | `~/.ai-desktop` | Base folder; screenshots go in `screenshots/` under it |
| `AI_DESKTOP_KEEP_DAYS` | `30` | Delete screenshots older than this; `off` disables |
| `AI_DESKTOP_KEEP_MB` | `2048` | Keep the folder under this size; `off` disables |
| `AI_DESKTOP_JSON` | unset | `1` makes every command print JSON, same as `--json` |
| `AI_DESKTOP_BACKEND` | `auto` | `pyautogui` or `uinput`, same as `--backend` |
| `AI_DESKTOP_SCREEN_SIZE` | unset | `1920x1080` override for the Wayland backend |
| `AI_DESKTOP_UINPUT_DELAY` | `0.5` | Seconds to wait for the compositor to see a virtual device (Wayland) |

## Copy and paste

- **`copy`** presses the copy shortcut and prints what was copied. The shortcut is cmd+c on macOS and ctrl+c elsewhere. The first line is `OK copy chars=N`, and everything after it is the copied text (with `--json`, it is the `"text"` field). If nothing was selected it fails with an error, so an agent never mistakes old clipboard contents for a fresh copy.
- **`paste --text "…"`** puts the text on the clipboard, then presses the paste shortcut. Use it for anything `type` can't enter, like accented letters, emoji or long passages. Without `--text`, it pastes whatever is already on the clipboard.
- **`clipboard get`** and **`clipboard set --text "…"`** read or write the clipboard without pressing any keys.
- **Terminals** copy with ctrl+shift+c. There, run `hotkey --keys ctrl+shift+c`, then `clipboard get`.

Clipboard tools: macOS `pbcopy`/`pbpaste` and Windows PowerShell are built in; Linux needs `xclip` (or `xsel`, or `wl-clipboard`). On GNOME Wayland, `xclip` also works for native Wayland apps.

## Agent loop

```bash
# 1. See the screen
ai-desktop screenshot --grid
#    -> OK screenshot path=/home/me/.ai-desktop/screenshots/20261012-101905-123-grid100.png ...
#    -> the agent opens that image and reads coordinates off the grid

# 2. Act
ai-desktop click --x 520 --y 340
ai-desktop type --text "search query"
ai-desktop key --key enter

# 3. Look again, and repeat until done
ai-desktop screenshot --grid
```

Tips for agents:
- Take a fresh screenshot after every action instead of assuming it worked.
- If a target sits between grid lines, re-run with `--spacing 50`, or crop the image and run `grid --input` on the crop. A crop has its own coordinates, so add the crop's offset back before clicking.

## Connecting an AI agent

Any AI agent can drive this tool if it can do two things:

1. **Run shell commands** on the machine with the desktop. That rules out chat-only assistants in a browser tab.
2. **Look at image files.** It has to open the screenshot to decide where to click. A text-only model can still type and press keys, but it is working blind.

Coding agents that run in your terminal or editor qualify: **Claude Code**, **OpenAI Codex CLI**, **Gemini CLI**, **xAI's Grok Build** (its file reader shows PNGs to the model), and similar tools.

### Setup

1. **Install the tool and run `ai-desktop doctor`** until it reports no failures.
2. **Give the agent the instructions in [`AGENTS.md`](AGENTS.md).** It explains the screenshot-then-act loop, the output format, and the safety rules (never type passwords, ask before posting or buying).
   - **Grok Build and Codex** read `AGENTS.md` automatically when started inside this folder.
   - **Claude Code** reads `CLAUDE.md`, which imports `AGENTS.md`.
   - **Gemini CLI** reads `GEMINI.md`: copy `AGENTS.md` to that name, or set `context.fileName` to `AGENTS.md`.
   - **Anything else:** paste the contents of `AGENTS.md` into its project instructions or system prompt.
   - Working from another folder? Point the agent at the file, or `cat AGENTS.md` into its prompt.
3. **Let the agent run the tool without a prompt for every command** (optional, and only if you will be watching the screen). Each agent has its own allow-list:
   - Claude Code: `/permissions`, or in `.claude/settings.json`:
     ```json
     { "permissions": { "allow": ["Bash(ai-desktop:*)", "Bash(.venv/bin/ai-desktop:*)", "Bash(python3 ai_desktop.py:*)"] } }
     ```
   - Grok Build: `grok --allow "Bash(ai-desktop:*)"`, or `--permission-mode auto` to approve everything; `--rules "$(cat AGENTS.md)"` appends the instructions to its system prompt.
   - Codex / Gemini CLI: see their approval-mode settings.

   With the rule in place nothing asks you before the agent clicks, types, or posts as you.

### Building your own agent

If you are writing an agent against a model API:
- **Give the model one tool**, say `run(command)`, that runs an `ai-desktop` command and returns its output (use `--json` for easy parsing).
- **Return the picture after a screenshot.** When the command was `screenshot`, read the file from `path` and send it back in the tool result as an image, so the model actually sees it.
- **Use `AGENTS.md` as the system prompt.**

That is the whole loop: the model reads a screenshot, picks a command, sees the result, and repeats.

### Is this the right tool?

- **Web-only tasks.** Browser automation that reads the page's structure (Playwright, browser-use, Claude in Chrome) is usually faster and more reliable than clicking pixels, because the agent can target elements by text or role instead of coordinates.
- **Isolated testing.** A sandboxed setup, such as a VM or a Docker desktop like Anthropic's computer-use reference implementation, keeps the agent away from your real accounts.
- **What this tool is for.** Driving *your real desktop and signed-in apps* from *any* agent that has a shell. No server to run, works on native Wayland, one Python file.

## Using your signed-in Chrome

```bash
ai-desktop launch-chrome --url https://mail.google.com
ai-desktop screenshot --grid      # confirm you're signed in
```

`launch-chrome` opens your normal Chrome with your existing cookies, logins and extensions. If Chrome is already running, it opens a new window in that same session.
- **Windows:** finds `chrome.exe` under Program Files, `%LOCALAPPDATA%` or the registry, and points it at `%LOCALAPPDATA%\Google\Chrome\User Data`.
- **macOS:** runs `open -a "Google Chrome"`.
- **Linux:** looks for `google-chrome`/`chromium` on PATH.

Chrome opens the profile you used last. To pick a different one, pass `--profile` with the folder name, e.g. `"Profile 1"`. Folder names are listed under `chrome://version` → *Profile Path*.

The new window may open **behind** the agent's own window; GNOME shows a "Google Chrome is ready" notification instead of raising it. Agents should take a screenshot after `launch-chrome` and click the notification or Chrome's dock icon.

Remember that the agent now acts as **you** on every site you're logged into.

## Platform notes

Start with `ai-desktop doctor` on every platform. It prints one `ok` / `warn` / `fail` line per requirement, with the fix next to each failure, and exits 1 while anything fails. Run it from the same terminal or IDE the agent will use.

### macOS
Under **System Settings → Privacy & Security**, grant your terminal or IDE app (Terminal, iTerm, VS Code, …) both of these, then quit and reopen it:
- **Screen Recording.** Without it, screenshots show only the wallpaper and menu bar, with no windows.
- **Accessibility.** Without it, clicks and keystrokes are silently ignored.

`doctor` reports both permissions (`screen-recording`, `accessibility`) for the app it was launched from.

Other notes:
- Use `cmd` in hotkeys (`hotkey --keys cmd+t`). `ctrl+c` on a Mac is Control-C, not copy.
- Scroll units are lines and feel small. Raise `--amount` if needed.

### Windows
- **Scaling.** 100% display scaling is recommended (`doctor` shows the current value). Other scales work on the primary monitor because screenshot and clicks share the same physical-pixel coordinates.
- **Admin windows.** Input can't reach windows running as administrator (Task Manager, installers, UAC prompts) unless the tool itself runs from an elevated terminal.
- **Scroll.** `--amount` counts mouse-wheel notches.
- Nothing else to install: the clipboard uses PowerShell, which is built in.

### Linux on X11
In an X11 session, such as **"Ubuntu on Xorg"** picked from the gear icon on the login screen, input goes through pyautogui exactly as on Windows and macOS, failsafe included. Screenshots use `gnome-screenshot` (or `grim`/`spectacle`) when installed and pyautogui otherwise. Install `xclip` for the clipboard.

### Linux on Wayland
pyautogui can't see or control native Wayland windows, so on Wayland the tool switches automatically:
- **Screenshots** use `gnome-screenshot` (GNOME), `grim` (Sway, Hyprland) or `spectacle` (KDE).
- **Input** goes through the kernel's `/dev/uinput`, using short-lived virtual mouse and keyboard devices. This needs no extra Python packages.

One-time setup, tested on Ubuntu 24.04 / GNOME 46. `./setup.sh` offers to run these for you:

```bash
sudo apt install gnome-screenshot xclip

# Let the logged-in user create virtual input devices (same rule Steam uses)
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-ai-desktop-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
getfacl /dev/uinput     # should list user:<you>:rw-  (if not, log out and in, or reboot)
```

Wayland caveats:
- **Security.** Any program running as you can now inject keyboard and mouse input. Delete the rule file to undo it.
- **US layout only.** `type` assumes the **US keyboard layout** is active, since it sends physical key positions. With another layout, symbols will come out wrong. `paste --text` doesn't depend on the layout.
- **Speed.** Each action takes about 0.6 s, because the compositor needs a moment to notice each new virtual device. If clicks are dropped, raise `AI_DESKTOP_UINPUT_DELAY` (default `0.5` seconds).
- **No pointer position.** `position` reports `x=unknown y=unknown`.
- **Screen size.** It is read from Xwayland. If that fails, set `AI_DESKTOP_SCREEN_SIZE=1920x1080`.
- **Snap-packaged terminals** (the VS Code snap, for example) inject their own library paths into child processes; the tool strips those before starting host programs, and `doctor` tells you when it has done so.

## Known limits
- No OCR, template matching, or window-specific screenshots.
- **Single screen.** Windows and macOS capture and address the primary screen only. On Linux, the screenshot and the coordinates cover the whole desktop.
- **ASCII only.** `type` handles printable ASCII, newline and tab, and rejects anything else (é, emoji, CJK) with an error. Use `paste --text` for those.
- **Scroll distance** per notch varies by app and platform.
- Windows and macOS are exercised by CI (install, unit tests, `doctor`), but live mouse and keyboard tests there depend on user reports.

## Development

```bash
pip install -e ".[dev]"
pytest                      # headless; never touches the real desktop
ai-desktop doctor
```

CI runs the tests and a CLI smoke test on Ubuntu, macOS and Windows with Python 3.9 and 3.12. The `doctor` and live-screenshot steps there are informational, because the runners have no desktop (Linux) or no permissions (macOS).

## Releasing

Pushing a tag like `v0.2.0` builds the package and publishes it to PyPI through [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored anywhere. One-time setup:

1. Create a PyPI account, then under *Your projects → Publishing* add a **pending publisher**: PyPI project `ai-desktop`, owner `MichaelDKulka`, repository `ai-desktop`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repo, *Settings → Environments*, create an environment named `pypi`.
3. Bump `version` in `pyproject.toml` and `__version__` in `ai_desktop.py`, update `CHANGELOG.md`, commit, then `git tag v0.2.0 && git push --tags`.

After the first release, `pip install ai-desktop` works everywhere.

## License

MIT, see [LICENSE](LICENSE).
