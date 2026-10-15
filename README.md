# AI Desktop Controller

[![CI](https://github.com/MichaelDKulka/ai-desktop/actions/workflows/ci.yml/badge.svg)](https://github.com/MichaelDKulka/ai-desktop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A small command-line tool (and MCP server) that lets **any AI agent that can run shell commands and look at images** see your desktop through a coordinate grid and control the mouse, keyboard, clipboard and windows. That includes driving your real, already signed-in Chrome. One Python file for the core, two dependencies, Windows / macOS / Linux (X11 and Wayland), multi-monitor, with an action log of everything the agent did.

> **⚠️ Safety warning.** Anything that can run this tool has **full control of your desktop and of every account that is logged in on it**: email, banking, admin consoles, password managers. Run it only with an agent you trust, watch what it does, and keep sensitive sessions closed while it runs.
>
> - **Windows, macOS, Linux X11:** a *failsafe* is on. Slam the mouse into **any corner of the primary screen** and the current and every following command aborts with exit code 3, until you move the mouse away.
> - **Linux Wayland:** there is no corner failsafe, because Wayland doesn't let apps read the pointer position. Stop the agent process itself (Ctrl+C in its terminal).

## Install

**One-shot setup** (creates `.venv`, installs, offers the Linux extras, then runs `doctor`):

```bash
git clone https://github.com/MichaelDKulka/ai-desktop
cd ai-desktop
./setup.sh                                               # macOS, Linux   (add --mcp for the MCP server)
powershell -ExecutionPolicy Bypass -File setup.ps1       # Windows        (add -Mcp for the MCP server)
```

**By hand:**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # or: pip install -e ".[mcp]" for the MCP server (Python 3.10+)
ai-desktop doctor                  # tells you what your OS still needs
```

Or, without cloning: `pip install git+https://github.com/MichaelDKulka/ai-desktop` (and, once released, `pip install ai-desktop`).

Then finish the one-time steps for your platform under [Platform notes](#platform-notes). `ai-desktop doctor` lists exactly what is missing and how to fix it.

**Requirements:** Python 3.9+ (3.10+ for the MCP server). The only Python dependencies are `pyautogui` and `pillow`; pip pulls in what they need per OS (pyobjc on macOS, python-xlib on Linux). Linux Wayland additionally needs `gnome-screenshot` (or `grim`/`spectacle`), `xclip` and write access to `/dev/uinput`, see below.

## Commands

```text
ai-desktop screenshot [--grid] [--spacing auto|N] [--monitor all|primary|N] [--cursor auto|marker|none]
                      [--zoom X,Y [--radius 200] [--factor 3]] [--max-width 1920] [-o screenshot.png]
ai-desktop grid --input image.png [-o gridded.png] [--spacing auto|N] [--max-width 1920]
ai-desktop click --x 500 --y 300 [--button left|right|middle] [--double] [--instant] [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop move --x 500 --y 300 [--instant] [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop type --text "hello world" [--interval 0.02] [--instant] [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop key --key enter [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop hotkey --keys ctrl+c [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop scroll [--direction up|down] [--amount 3] [--x N --y N] [--instant] [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop copy
ai-desktop paste [--text "any text, even é or ✓"] [--seen PATH|latest] [--shot [MONITOR]]
ai-desktop wait (--stable | --changed PATH|latest) [--timeout 10] [--interval 0.5] [--monitor all] [--shot [MONITOR]]
ai-desktop run (--steps JSON | --file PATH|-) [--shot [MONITOR]]
ai-desktop clipboard get|set [--text "..."]
ai-desktop position
ai-desktop windows
ai-desktop focus --title "part of title" | --app NAME | --pid N | --id ID
ai-desktop monitors
ai-desktop launch-chrome [--url URL] [--profile "Profile 1"]
ai-desktop screenshots dir|latest|list [--limit 20]|clean [--days N | --all]
ai-desktop log [--limit 20]
ai-desktop doctor
ai-desktop mcp [--max-width 1920]
ai-desktop --version
```

Global flags, accepted before or after the command: `--json`, `--backend auto|pyautogui|uinput`, `--instant`, `--seed N`.

**Output.** Every command prints one line an agent can parse, or an error on stderr:

```text
OK screenshot path=/home/me/.ai-desktop/screenshots/20261012-101905-123-grid200.png width=1920 height=631 x=0 y=0 monitor=all spacing=200 scale=1.0 backend=pyautogui pruned=0 image_scale=0.5843 cursor=native pointer=live pointer_x=1210 pointer_y=388
OK click x=520 y=340 button=left clicks=1
ERROR (5000, 50) is on no monitor; monitors: 0 eDP-1 at 0,0 size 1366x768
```

`windows`, `monitors`, `screenshots list` and `log` print one item per line after the first line. With `--json` (or `AI_DESKTOP_JSON=1`) each command prints one JSON object instead, errors included, and those lists become JSON arrays:

```json
{"ok": true, "command": "click", "x": 520, "y": 340, "button": "left", "clicks": 1}
{"ok": false, "command": "click", "error": "(5000, 50) is on no monitor; ...", "code": 1}
```

Exit codes: `0` ok, `1` error, `2` bad arguments (plain argparse text, never JSON; a bad `run` step is reported like any other error), `3` failsafe abort.

**Human-like pacing.** By default `click`, `move` and `scroll --x/--y` glide to the target along a random curved path (up to 60 steps, about 0.15–1.5 s in total, slow-fast-slow) that lands **exactly** on the requested coordinates, stays on a monitor and never crosses a failsafe corner; the target is validated before anything moves. `type` sends one character at a time with a random 0.10–0.15 s pause after each character and 0.25–0.5 s after each space, newline or tab, so a 50-character, 8-word sentence takes about 8 s. `--instant` (or `AI_DESKTOP_HUMAN=0`) jumps straight to the target and types at the fixed `--interval`; `--seed N` (or `AI_DESKTOP_SEED`) makes the path and the pauses repeatable. `paste --text` is still the fast way to enter long text.

```bash
ai-desktop click --x 520 --y 340                 # curved path, about a second
ai-desktop --instant click --x 520 --y 340       # jump straight there
ai-desktop --seed 7 type --text "hello world"    # same pauses every run
```

**Screenshot coordinates are click coordinates.** The grid labels are global screen coordinates, so a point you read off the image is exactly what you pass to `click`, on any monitor. That holds in a `--zoom` close-up and in an image shrunk to `--max-width` too, because the labels, not the pixel positions, carry the coordinates; `image_scale=` reports image px per screen px, so image pixel (px, py) is screen point (x + px / image_scale, y + py / image_scale). Labels are sized so their digits stay legible after a vision model shrinks the image to about 1568 px on its long edge. On HiDPI screens (a Retina Mac, or GNOME scaled to 200%) the capture is resized to the logical size first; `scale=` reports the original ratio.

**Key names.**
- Named keys: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `up`/`down`/`left`/`right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, `ctrl`, `shift`, `alt`, and `cmd` (also `win`, `super`, `meta`).
- Any single character also works, e.g. `hotkey --keys ctrl+a`.
- Aliases such as `return`, `escape`, `control`, `option` and `pgdn` are accepted.
- Unknown keys are an error, never silently ignored.

## Screenshots

**The grid.** `--grid` draws lines with a magenta core over a dark outline, so they stay visible on light, pink, dark and busy content, with short unlabelled ticks halfway between them. X labels sit at the top and bottom, y labels at the left and right, and some crossings carry an interior `x,y` label (roughly every 400 px of the saved image). Labels are thinned out by their global value, so the same coordinates get labelled whatever the capture's origin. `--spacing` defaults to `auto`: 100 on ordinary screens, 200 or more on big screens and multi-monitor images, finer in a zoom. An explicit number (10 or more) still works.

**Zoom.** `--zoom X,Y` crops the area `--radius` px (default 200) around a screen point, enlarges it `--factor` times (default 3, from the physical pixels on HiDPI screens) and draws a fine grid whose labels are still global screen coordinates. It implies `--grid`, and the area stays on the point's monitor. Use it when a target is small or sits between grid lines.

```bash
ai-desktop screenshot --zoom 1311,712
# OK screenshot path=/home/me/.ai-desktop/screenshots/20261012-101921-004-zoom-grid20.png width=1200 height=1200 x=1111 y=512 ... spacing=20 ... image_scale=3.0 ... zoom=1311,712 radius=200 factor=3
```

**Width.** Images wider than `--max-width` (default 1920, or `AI_DESKTOP_MAX_WIDTH`; `0` = never) are shrunk, with lines and labels drawn bigger first so they stay legible. `image_scale=` in the OK line says by how much; since the labels carry the coordinates, a shrunk image still clicks right. `grid --max-width` does the same for an existing image.

**Pointer.** `--cursor auto` (the default) has the capture tool draw the real pointer (`gnome-screenshot -p`, `grim -c`, `spectacle -p`, macOS `screencapture -C`). Where it can't (Windows, Pillow's X11 fallback), a white crosshair is drawn instead, labelled `ptr` at the live position or `ptr?` at the position ai-desktop last set (Wayland, from `pointer.json`). `--cursor marker` always draws the crosshair, `--cursor none` neither. The OK line says what happened: `cursor=native|marker|none pointer=live|remembered|unknown pointer_x=... pointer_y=...`.

**The store.** `screenshot` saves to the tool's own folder, **`~/.ai-desktop/screenshots/`** on every OS (Windows: `C:\Users\you\.ai-desktop\screenshots\`), as `YYYYMMDD-HHMMSS-mmm[-N][-mM][-zoom][-gridS].png` in local time (`-N` when two land in the same millisecond, `-mM` for one monitor, `-gridS` with the spacing). The path is printed each time, and `screenshots latest` finds the newest one again.

Next to every screenshot goes **`<name>.raw.png`**: the un-gridded capture, with an `ai-desktop` PNG text chunk recording its origin, size, monitor and pointer. It is the reference for `--seen` and `wait --changed`, is hidden from `screenshots latest` and `list`, and is pruned with its screenshot. `-o PATH` files get their `.raw.png` next to them too; `-o` may not end in `.raw.png`. `grid --input` on a screenshot from the tool re-grids its `.raw.png` with the global origin, so the labels stay screen coordinates.

**Retention.** Files are deleted automatically when they are **older than 30 days**, and the oldest go first whenever the folder exceeds **2 GB**. The check runs inside the tool, at most once an hour, so there is no background service. Only files the tool named itself are ever removed; anything else you put in the folder, symlinks, and files written with `-o PATH` (and their `.raw.png`) are never touched. The action log files (below) follow the same age rule.

```bash
ai-desktop screenshots dir            # where, how many, how big, current limits
ai-desktop screenshots latest         # newest file
ai-desktop screenshots list --limit 5 # newest first, one path per line
ai-desktop screenshots clean          # apply the rules now
ai-desktop screenshots clean --days 7 # or stricter
ai-desktop screenshots clean --all    # empty the store (and the logs)
```

Why not the system temp folder: macOS purges `/tmp` files unused for three days and Linux usually clears it on reboot, so 30-day retention would be meaningless there.

## Monitors

```bash
ai-desktop monitors
# OK monitors count=2 primary=0 virtual_x=-1920 virtual_y=0 virtual_width=3286 virtual_height=1080 source=xrandr
# 0 eDP-1 0,0 1366x768 primary scale=1
# 1 HDMI-1 -1920,0 1920x1080 scale=1
```

- Monitor `0` is always the primary; the others follow top-to-bottom, left-to-right. Coordinates are global: a monitor to the left of the primary has negative `x`.
- `screenshot` captures **all monitors in one image** by default (black where no monitor is). `--monitor 1` captures one monitor, `--monitor primary` the primary. Either way the grid labels are global, so clicks need no arithmetic.
- A multi-monitor image is usually wider than `--max-width`, so it is shrunk and gets automatic spacing of 200 or more, which keeps its labels legible. For precise clicks look closer with `--monitor N` or `--zoom X,Y`.
- `click`, `move` and `scroll` accept any point on any monitor. `position` reports `monitor=` for the pointer.
- The layout comes from `xrandr` (Linux, also under Xwayland), `EnumDisplayMonitors` (Windows) or Quartz (macOS). If it can't be read, the tool assumes one screen and `doctor` warns.
- Known limit: Windows with *different* scaling per monitor isn't handled; keep the same scaling everywhere (100% recommended).

## Windows and focus

```bash
ai-desktop windows
# OK windows count=2 partial=False
# 0x3a00004 pid=4821 66,32 1300x736 active google-chrome "Inbox - Gmail - Google Chrome"
# 0x2200003 pid=3990 0,0 1366x741 minimized code "README.md - screen - Visual Studio Code"
ai-desktop focus --title gmail        # part of the title (or app name), case-insensitive
ai-desktop focus --app code           # part of the app / class / exe name
ai-desktop focus --pid 4821
ai-desktop focus --id 0x2200003       # exact id from the list above
```

Windows are listed front-to-back with their global geometry. When several windows match, an exact title match wins, otherwise the front-most one; `matches=` in the output says how many there were. `focus` restores a minimized window first and fails (exit 1) if the window did not come to the front.

- **Windows:** via the Win32 API; cloaked windows (UWP shells, other virtual desktops) and tool windows are skipped.
- **macOS:** via Quartz. Other apps' window titles are only visible with **Screen Recording** permission (without it, use `--app`). `focus` activates the app and raises the window with a System Events script, which asks once for **Automation** consent. Minimized windows and windows on other Spaces are not listed.
- **Linux X11:** via libX11 and the EWMH window-manager protocol, including under Xwayland.
- **Linux Wayland (GNOME):** only X11/XWayland apps are visible; GNOME has no public API to list or focus native Wayland windows. The output then carries `partial=True` and a note. Agents can still click a window, the dock, or the "is ready" notification. For full window control, log in to the "Ubuntu on Xorg" session.

## Action log

Every command appends one JSON line to **`~/.ai-desktop/logs/actions-YYYYMMDD.jsonl`** (one file per local day): timestamp, who ran it (`cli` or `mcp`, or `cli/run` and `mcp/run` for the steps of `run`), command, arguments, backend, result fields, exit code and duration. The `screenshot` entries carry the file path, so a session can be replayed from the log and the screenshots next to it.

```bash
ai-desktop log --limit 5
# OK log path=/home/me/.ai-desktop/logs count=5
# 13:05:09 cli click ok 1180ms x=500 y=300 button=left clicks=1
# 13:05:01 cli screenshot ok 1188ms path=/home/me/.ai-desktop/screenshots/20261012-130501-022-grid100.png width=1366 ...
# 13:04:58 mcp type ERROR(1) 3ms cannot type these characters (ASCII only): 'é'
ai-desktop --json log                 # the raw entries
```

**Privacy.** By default the text given to `type`, `paste --text` and `clipboard set`, and the text returned by `copy` and `clipboard get`, is **not** stored: the entry keeps only the character count (`text_chars`, `chars`). The `run` entry keeps only its steps' command names, whatever the setting. Set `AI_DESKTOP_LOG_TEXT=1` to keep the full text. Set `AI_DESKTOP_LOG=0` to turn the log off. Log files older than `AI_DESKTOP_KEEP_DAYS` are deleted with the screenshots, and `screenshots clean --all` removes them too. Logging never fails or slows a command: if the file can't be written, the command still runs.

**Settings** (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `AI_DESKTOP_HOME` | `~/.ai-desktop` | Base folder; screenshots go in `screenshots/`, logs in `logs/` under it |
| `AI_DESKTOP_KEEP_DAYS` | `30` | Delete screenshots and log files older than this; `off` disables |
| `AI_DESKTOP_KEEP_MB` | `2048` | Keep the screenshot folder under this size; `off` disables |
| `AI_DESKTOP_LOG` | on | `0` turns the action log off |
| `AI_DESKTOP_LOG_TEXT` | off | `1` stores typed, pasted and copied text in the log |
| `AI_DESKTOP_JSON` | unset | `1` makes every command print JSON, same as `--json` |
| `AI_DESKTOP_BACKEND` | `auto` | `pyautogui` or `uinput`, same as `--backend` |
| `AI_DESKTOP_HUMAN` | on | `0`, `false`, `off` or `no` disables the human-like path and typing pauses, same as `--instant` |
| `AI_DESKTOP_SEED` | unset | Integer seed that makes the random path and pauses repeatable, same as `--seed` |
| `AI_DESKTOP_MAX_WIDTH` | `1920` | Shrink screenshots wider than this many px, same as `--max-width`; `0` never shrinks |
| `AI_DESKTOP_SHOT_DELAY` | `0.4` | Seconds between an action and its `--shot` screenshot |
| `AI_DESKTOP_SCREEN_SIZE` | unset | `1920x1080` override for the Wayland backend |
| `AI_DESKTOP_UINPUT_DELAY` | `0.5` | Seconds to wait for the compositor to see a virtual device (Wayland); inside `run` and the MCP server it is paid once per device |

## Copy and paste

- **`copy`** presses the copy shortcut and prints what was copied. The shortcut is cmd+c on macOS and ctrl+c elsewhere. The first line is `OK copy chars=N`, and everything after it is the copied text (with `--json`, it is the `"text"` field). If nothing was selected it fails with an error, so an agent never mistakes old clipboard contents for a fresh copy.
- **`paste --text "…"`** puts the text on the clipboard, then presses the paste shortcut. Use it for anything `type` can't enter, like accented letters, emoji or long passages. `type` is paced like a person (about 0.12 s per character plus 0.25–0.5 s per word), so prefer `paste --text` or `--instant` for more than a sentence or two. Without `--text`, it pastes whatever is already on the clipboard.
- **`clipboard get`** and **`clipboard set --text "…"`** read or write the clipboard without pressing any keys.
- **Terminals** copy with ctrl+shift+c. There, run `hotkey --keys ctrl+shift+c`, then `clipboard get`.

Clipboard tools: macOS `pbcopy`/`pbpaste` and Windows PowerShell are built in; Linux needs `xclip` (or `xsel`, or `wl-clipboard`). On GNOME Wayland, `xclip` also works for native Wayland apps.

## Seen, shot, wait and run

**`--seen SCREENSHOT|latest`** on `click`, `move`, `scroll`, `type`, `key`, `hotkey` and `paste` checks, just before acting, that the screen still looks like the screenshot the agent read. If it doesn't, the command fails with exit 1 and nothing is moved or typed. `click`, `move` and `scroll --x/--y` compare the 129x129 px area around the target (limit: 5% of its pixels changed); the other commands compare a coarse fingerprint of the whole screen (limit 10%: a toast passes, a dialog doesn't). `latest` is the newest screenshot in the store; for a file written with `-o`, pass its path. The check costs one capture (about 1 s on GNOME), and a pointer the capture tool drew into the reference is ignored.

```bash
ai-desktop click --x 520 --y 340 --seen latest
# OK click x=520 y=340 button=left clicks=1 seen=/home/me/.ai-desktop/screenshots/20261012-101905-123-grid100.png seen_diff=0.012
# ERROR screen changed near (520, 340) since /home/me/.ai-desktop/screenshots/20261012-101905-123-grid100.png: 31% of the 129x129 px area around it differs (limit 5%); take a new screenshot before acting
```

Other refusals name their cause: the file doesn't exist, it has no `.raw.png` next to it (older files cannot be checked), the target lies outside it, or the screen layout changed. In every case: take a new screenshot.

**`--shot [MONITOR]`** on the same commands takes a gridded screenshot after acting (after a 0.4 s settle, `AI_DESKTOP_SHOT_DELAY`) and adds `shot=<path>` to the OK line; with `--json` there is also a `screenshot` object with that screenshot's fields. If that capture fails, the action still reports OK, with `shot=error shot_error="..."`, so an agent never repeats an action that already happened.

```bash
ai-desktop click --x 520 --y 340 --seen latest --shot
# OK click x=520 y=340 button=left clicks=1 seen=... seen_diff=0.0 shot=/home/me/.ai-desktop/screenshots/20261012-101911-480-grid100.png
```

**`wait`** watches the screen instead of guessing a sleep. `--stable` returns once two captures in a row match, which is the thing to do after opening a page or an app; `--changed SCREENSHOT|latest` returns once the screen differs from that screenshot by more than 2%. `--interval` (default 0.5 s) is the pause between captures, and `--monitor` picks the screen `--stable` watches. After `--timeout` seconds (default 10, at most 300) it fails with exit 1: `screen still changing after 10 s ...` or `screen unchanged after 10 s ...`. With `--shot`, the last capture becomes the screenshot, at no extra cost.

```bash
ai-desktop launch-chrome --url https://example.com
ai-desktop wait --stable --timeout 20 --shot
# OK wait mode=stable polls=3 elapsed=2.9 diff=0.0 shot=/home/me/.ai-desktop/screenshots/20261012-101930-212-grid100.png
```

**`run`** does several commands in one process. `--steps` takes a JSON list whose items are argument lists (`["click","--x","500","--y","300"]`) or command strings (`"key --key enter"`); `--file PATH` reads the list from a file, `--file -` from stdin. Every step's arguments are checked before any step runs (a bad one exits 2), and `run` and `mcp` can't be steps. It stops at the first failure with `ERROR step 3 (click) failed: <reason>` and that step's exit code (3 for the failsafe). `--backend` and `--instant` given to `run` apply to every step, and `--shot` takes a screenshot after the last one. Each step is logged as its own entry with `via` set to `cli/run` (or `mcp/run`). On Wayland the virtual input devices stay open across steps, so only the first action of each kind pays the ~0.5 s device delay: measured live, three moves took 1.0 s as one `run` and 2.45 s as three commands. Use it only for short sequences you are sure of, and still look at the screen afterwards.

```bash
ai-desktop run --shot --steps '[["click","--x","500","--y","300"], "type --text hello", "key --key enter"]'
# OK run steps=3 done=3 shot=/home/me/.ai-desktop/screenshots/20261012-101945-907-grid100.png
```

## Agent loop

```bash
# 1. See the screen
ai-desktop focus --title "Chrome"      # or launch-chrome, then: ai-desktop wait --stable
ai-desktop screenshot --grid
#    -> OK screenshot path=/home/me/.ai-desktop/screenshots/20261012-101905-123-grid100.png ...
#    -> the agent opens that image and reads coordinates off the grid
#    -> target small? ai-desktop screenshot --zoom 520,340 (its labels are screen coordinates too)

# 2. Act, one action per look. --seen latest refuses to act if the screen changed since the
#    newest screenshot; --shot takes the next one in the same call.
ai-desktop click --x 520 --y 340 --seen latest --shot  # glides there along a curved path
ai-desktop type --text "search query" --seen latest    # paced: ~0.12 s per character plus 0.25-0.5 s per word
ai-desktop key --key enter
ai-desktop wait --stable --shot                        # the page loads; shot= is the settled screen
#    (for more than a sentence or two, use paste --text or add --instant)

# 3. Look at the shot= image, and repeat until done
```

Tips for agents:
- Look at a fresh screenshot after every action instead of assuming it worked; `--shot` takes it in the same call.
- If an action fails with "screen changed", nothing happened: take a new screenshot and read it again before acting.
- If a target is small or sits between grid lines, `screenshot --zoom X,Y` shows it enlarged; `--spacing 50` or `--monitor N` also help.
- After opening a page or an app, use `wait --stable` instead of a fixed sleep.
- Use `windows` to find out what is open and `focus` before typing into a window. Take the screenshot after `focus`, since it changes the screen.

## Connecting an AI agent

Any AI agent can drive this tool if it can do two things:

1. **Run shell commands** on the machine with the desktop (or connect to the [MCP server](#mcp-server)). That rules out chat-only assistants in a browser tab.
2. **Look at image files** (with MCP, the screenshot arrives inline). A text-only model can still type and press keys, but it is working blind.

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

If you are writing an agent against a model API, the easiest route is the MCP server below. Without MCP:
- **Give the model one tool**, say `run(command)`, that runs an `ai-desktop` command and returns its output (use `--json` for easy parsing).
- **Return the picture after a screenshot.** After `screenshot`, read the file from `path` (after an action with `--shot`, from `shot`) and send it back in the tool result as an image, so the model actually sees it.
- **Use `AGENTS.md` as the system prompt.**

### Is this the right tool?

- **Web-only tasks.** Browser automation that reads the page's structure (Playwright, browser-use, Claude in Chrome) is usually faster and more reliable than clicking pixels, because the agent can target elements by text or role instead of coordinates.
- **Isolated testing.** A sandboxed setup, such as a VM or a Docker desktop like Anthropic's computer-use reference implementation, keeps the agent away from your real accounts.
- **What this tool is for.** Driving *your real desktop and signed-in apps* from *any* agent that has a shell or speaks MCP. No server to run, works on native Wayland, one Python file.

## MCP server

`ai-desktop mcp` runs the same commands as an [MCP](https://modelcontextprotocol.io) server over stdio. Every command is a tool with the same name (`screenshot`, `click`, `move`, `type`, `key`, `hotkey`, `scroll`, `wait`, `run`, `copy`, `paste`, `clipboard_get`, `clipboard_set`, `position`, `windows`, `focus`, `monitors`, `launch_chrome`, `screenshots_latest`, `log`, `doctor`), and the **`screenshot` tool returns the saved PNG inline**, so the model sees it without opening a file; it takes `grid`, `spacing`, `monitor`, `zoom`, `cursor` and `max_width`. Errors come back as tool errors with the same messages as the CLI, and every call lands in the action log as `via=mcp` (`mcp/run` for the steps of `run`).

- `click`, `move`, `scroll` and `type` accept `instant=true` to skip the human-like path and typing pauses.
- `click`, `move`, `scroll`, `type`, `key`, `hotkey` and `paste` accept `seen` (`"latest"` or a screenshot path) and `shot=true`; the new screenshot comes back inline in the same result.
- `wait` takes `stable` (the default), `changed`, `timeout` (at most 30 s here) and `shot`; `run` takes `steps` as a list of argument lists.
- The server keeps the uinput devices open for its whole lifetime, so on Wayland only the first action of each kind waits for the compositor.

```bash
pip install -e ".[mcp]"          # needs Python 3.10+; the core stays on 3.9
ai-desktop mcp --max-width 1920  # what the clients below run for you
```

`--max-width` (default: `AI_DESKTOP_MAX_WIDTH`, else 1920) applies to every screenshot the server takes, the `screenshot` tool's and the ones actions return with `shot=true`; the tool's own `max_width` overrides it per call. Wider captures are shrunk, with the labels enlarged first so they stay legible, and the saved file is the image the model sees. The JSON part of the result reports `width`, `height` and `image_scale`. `0` disables the shrinking.

Client setup, using the absolute path of the installed command (clients do not inherit your virtualenv):

- **Claude Code:** `claude mcp add ai-desktop -- /abs/path/.venv/bin/ai-desktop mcp`
- **Claude Desktop:** in `claude_desktop_config.json`:
  ```json
  { "mcpServers": { "ai-desktop": { "command": "/abs/path/.venv/bin/ai-desktop", "args": ["mcp"] } } }
  ```
- **Grok Build:** `grok mcp add ai-desktop -- /abs/path/.venv/bin/ai-desktop mcp`
- **Codex:** `codex mcp add ai-desktop -- /abs/path/.venv/bin/ai-desktop mcp`
- **Gemini CLI:** `gemini mcp add ai-desktop /abs/path/.venv/bin/ai-desktop mcp`

Environment variables such as `AI_DESKTOP_HOME` or `AI_DESKTOP_LOG_TEXT` go in the client's `env` block for the server. The server's instructions carry the safety rules; `AGENTS.md` still makes a good system prompt.

## Using your signed-in Chrome

```bash
ai-desktop launch-chrome --url https://mail.google.com
ai-desktop wait --stable --shot   # let it load, then confirm you're signed in
```

`launch-chrome` opens your normal Chrome with your existing cookies, logins and extensions. If Chrome is already running, it opens a new window in that same session.
- **Windows:** finds `chrome.exe` under Program Files, `%LOCALAPPDATA%` or the registry, and points it at `%LOCALAPPDATA%\Google\Chrome\User Data`.
- **macOS:** runs `open -a "Google Chrome"`.
- **Linux:** launches through the desktop entry (`gtk-launch` or `gio launch`) when there is one, else the `google-chrome`/`chromium` binary on PATH.

Chrome opens the profile you used last. To pick a different one, pass `--profile` with the folder name, e.g. `"Profile 1"`. Folder names are listed under `chrome://version` → *Profile Path*.

On GNOME the new window opens **behind** the agent's own window and a "Google Chrome is ready" notification appears at the top of the screen instead; this happens with every launch method. Agents should take a screenshot after `launch-chrome` and click the notification or Chrome's dock icon (`focus` can't reach native Wayland windows, see above).

Remember that the agent now acts as **you** on every site you're logged into.

## Platform notes

Start with `ai-desktop doctor` on every platform. It prints one `ok` / `warn` / `fail` line per requirement, with the fix next to each failure, and exits 1 while anything fails. Run it from the same terminal or IDE the agent will use.

### macOS
Under **System Settings → Privacy & Security**, grant your terminal or IDE app (Terminal, iTerm, VS Code, …) both of these, then quit and reopen it:
- **Screen Recording.** Without it, screenshots show only the wallpaper and menu bar, with no windows, and `windows` can't see other apps' titles.
- **Accessibility.** Without it, clicks and keystrokes are silently ignored and `focus` can't raise windows.

The first `focus` also asks for **Automation** consent (controlling System Events). `doctor` reports the first two permissions (`screen-recording`, `accessibility`) for the app it was launched from.

Other notes:
- Use `cmd` in hotkeys (`hotkey --keys cmd+t`). `ctrl+c` on a Mac is Control-C, not copy.
- Scroll units are lines and feel small. Raise `--amount` if needed.

### Windows
- **Scaling.** 100% display scaling is recommended (`doctor` shows the current value). Other scales work as long as every monitor uses the same one.
- **Admin windows.** Input can't reach windows running as administrator (Task Manager, installers, UAC prompts) unless the tool itself runs from an elevated terminal.
- **Scroll.** `--amount` counts mouse-wheel notches.
- Nothing else to install: the clipboard uses PowerShell, which is built in.

### Linux on X11
In an X11 session, such as **"Ubuntu on Xorg"** picked from the gear icon on the login screen, input goes through pyautogui exactly as on Windows and macOS, failsafe included, and `windows`/`focus` see every window. Screenshots use `gnome-screenshot` (or `grim`/`spectacle`) when installed and Pillow's own X11 capture otherwise. Install `xclip` for the clipboard.

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
- **Speed.** Each action costs about 0.6 s of virtual-device setup, because the compositor needs a moment to notice each new virtual device, plus the human-paced path or typing time (see [Human-like pacing](#commands)). Inside `run` and the MCP server the devices stay open, so only the first action of each kind pays the setup. If clicks are dropped, raise `AI_DESKTOP_UINPUT_DELAY` (default `0.5` seconds).
- **No pointer position.** `position` reports `x=unknown y=unknown`. The tool keeps the last position it set in `pointer.json` under the ai-desktop home folder and starts the next path from there; if that is unknown or off-screen, the path starts from a random nearby point on the target's monitor. Screenshots still show the real pointer, drawn by `gnome-screenshot -p` (or `grim -c`, `spectacle -p`); where a tool can't draw it, or with `--cursor marker`, a `ptr?` crosshair marks the remembered position.
- **Windows.** `windows` and `focus` only see X11/XWayland apps (see [Windows and focus](#windows-and-focus)).
- **Screen size and layout** come from Xwayland (`libX11`, `xrandr`). If that fails, set `AI_DESKTOP_SCREEN_SIZE=1920x1080`.
- **Snap-packaged terminals** (the VS Code snap, for example) inject their own library paths into child processes; the tool strips those before starting host programs, and `doctor` tells you when it has done so.

## Known limits
- No OCR or element finding: the agent reads coordinates off the grid.
- **Mixed DPI.** Monitors with different scaling factors (Windows) are not handled; HiDPI with the same factor everywhere is.
- **Wayland windows.** Native Wayland windows can't be listed or focused on GNOME; macOS doesn't list minimized windows or other Spaces.
- **ASCII only.** `type` handles printable ASCII, newline and tab, and rejects anything else (é, emoji, CJK) with an error. Use `paste --text` for those. Newline and tab count as word breaks for the typing pause, like a space.
- **Scroll distance** per notch varies by app and platform.
- **Moving content.** The `--seen` pixel check can't tell an animation, a video or a ticking clock near the target from a real change; aim at a tight target or leave out `--seen` there. For the same reason `wait --stable` may time out while a video plays.
- **Disk use.** The `.raw.png` copies roughly double the space screenshots take (`AI_DESKTOP_KEEP_MB` still caps the total).
- **Zoom on macOS.** With `--monitor all` (the default), macOS displays may differ in scale, so `--zoom` enlarges the logical image; zoom with `--monitor N` to use a Retina display's physical pixels.
- Windows and macOS are exercised by CI (install, unit tests, `monitors`, `windows`, `doctor`, live screenshots), but live mouse, keyboard and `focus` behaviour there depend on user reports.

## Development

```bash
pip install -e ".[dev,mcp]"
pytest                      # headless; never touches the real desktop
ai-desktop doctor
```

CI runs the tests and a CLI smoke test on Ubuntu, macOS and Windows with Python 3.9 and 3.12 (the MCP extra on 3.12 only), plus `monitors` and `windows` live on the macOS and Windows runners. The `doctor` and live-screenshot steps there are informational, because the runners have no desktop (Linux) or no permissions (macOS).

Rule for contributors: nothing may print to stdout except through `status()` / `fail()`, because under `ai-desktop mcp` stdout is the protocol channel. Every subprocess the tool starts captures or discards its output.

## Releasing

Pushing a tag like `v0.3.0` builds the package and publishes it to PyPI through [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored anywhere. One-time setup:

1. Create a PyPI account, then under *Your projects → Publishing* add a **pending publisher**: PyPI project `ai-desktop`, owner `MichaelDKulka`, repository `ai-desktop`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repo, *Settings → Environments*, create an environment named `pypi` (already done).
3. Bump `version` in `pyproject.toml` and `__version__` in `ai_desktop.py`, update `CHANGELOG.md`, commit, then `git tag v0.3.0 && git push --tags`.

After the first release, `pip install ai-desktop` works everywhere.

## License

MIT, see [LICENSE](LICENSE).
