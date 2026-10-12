# AI Desktop Controller

A small CLI that lets an AI agent, or any harness that can run shell commands and read images, see the desktop through a coordinate grid and control the mouse and keyboard. That includes driving your real, already signed-in Chrome.

> **⚠️ Safety warning.** Anything that can run this tool has **full control of your desktop and of every account that is logged in on it**: email, banking, admin consoles, password managers. Run it only with an agent you trust, watch what it does, and keep sensitive sessions closed while it runs.
>
> - **Windows, macOS, Linux X11:** a *failsafe* is on. Slam the mouse into **any screen corner** and the current and every following command aborts with exit code 3, until you move the mouse away.
> - **Linux Wayland:** there is no corner failsafe, because Wayland doesn't let apps read the pointer position. Stop the agent process itself (Ctrl+C in its terminal).

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt     # pyautogui + pillow
python ai_desktop.py --help
```

Optionally run `pip install -e .` to get an `ai-desktop` command. The examples below work the same with `ai-desktop` or `python ai_desktop.py`.

Then do the one-time setup for your platform (see [Platform notes](#platform-notes)).

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
```

**Output.** Every command prints one line an agent can parse, or an error on stderr:

```text
OK screenshot path=/tmp/ai_desktop_screen.png width=1920 height=1080 spacing=100 scale=2.0 backend=pyautogui
OK click x=520 y=340 button=left clicks=1
ERROR (2000, 50) is off screen; valid range is x 0..1919, y 0..1079
```

Exit codes: `0` ok, `1` error, `2` bad arguments, `3` failsafe abort.

**Screenshot coordinates are click coordinates.** On HiDPI screens (a Retina Mac, or GNOME scaled to 200%) the capture is resized to the logical screen size, so a point you read off the gridded image is exactly what you pass to `click`. `scale=` reports the original ratio.

**Screenshot path.** Without `-o`, `screenshot` writes to `ai_desktop_screen.png` in the system temp folder and overwrites it each time.

**Key names.**
- Named keys: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `up`/`down`/`left`/`right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, `ctrl`, `shift`, `alt`, and `cmd` (also `win`, `super`, `meta`).
- Any single character also works, e.g. `hotkey --keys ctrl+a`.
- Aliases such as `return`, `escape`, `control`, `option` and `pgdn` are accepted.
- Unknown keys are an error, never silently ignored.

## Copy and paste

- **`copy`** presses the copy shortcut and prints what was copied. The shortcut is cmd+c on macOS and ctrl+c elsewhere. The first line is `OK copy chars=N`, and everything after it is the copied text. If nothing was selected it fails with an error, so an agent never mistakes old clipboard contents for a fresh copy.
- **`paste --text "…"`** puts the text on the clipboard, then presses the paste shortcut. Use it for anything `type` can't enter, like accented letters, emoji or long passages. Without `--text`, it pastes whatever is already on the clipboard.
- **`clipboard get`** and **`clipboard set --text "…"`** read or write the clipboard without pressing any keys.
- **Terminals** copy with ctrl+shift+c. There, run `hotkey --keys ctrl+shift+c`, then `clipboard get`.

**Clipboard tools.**
- macOS: `pbcopy`/`pbpaste`, built in.
- Windows: PowerShell's `Get-Clipboard`/`Set-Clipboard`, built in.
- Linux: needs `xclip` (or `xsel`, or `wl-clipboard`). On GNOME Wayland, `xclip` also works for native Wayland apps.

## Agent loop

```bash
# 1. See the screen
python ai_desktop.py screenshot --grid --spacing 100 -o /tmp/screen.png
#    -> agent opens /tmp/screen.png, reads coordinates off the grid

# 2. Act
python ai_desktop.py click --x 520 --y 340
python ai_desktop.py type --text "search query"
python ai_desktop.py key --key enter

# 3. Look again, and repeat until done
python ai_desktop.py screenshot --grid -o /tmp/screen.png
```

Tips for agents:
- Take a fresh screenshot after every action instead of assuming it worked.
- If a target sits between grid lines, re-run with `--spacing 50`, or crop the image and run `grid --input` on the crop. A crop has its own coordinates, so add the crop's offset back before clicking.

## Connecting an AI agent

Any AI agent can drive this tool if it can do two things:

1. **Run shell commands** on the machine with the desktop. That rules out chat-only assistants in a browser tab.
2. **Look at image files.** It has to see `screenshot.png` to decide where to click. A text-only model can still type and press keys, but it's working blind.

Coding agents that run in your terminal or editor (Claude Code, OpenAI Codex CLI, Gemini CLI and similar) qualify, as long as they can read local images. Check your agent's docs for image support.

### Setup

1. **Install the tool and do the platform setup.** See [Install](#install) and [Platform notes](#platform-notes).
2. **Let the agent run it without asking every time** (optional). Agents usually ask before each shell command. In Claude Code, add allow rules with `/permissions`, or in `.claude/settings.json`:
   ```json
   { "permissions": { "allow": [
       "Bash(.venv/bin/ai-desktop:*)",
       "Bash(ai-desktop:*)",
       "Bash(python3 ai_desktop.py:*)"
   ] } }
   ```
   Other agents have their own allow-list settings. Only do this if you'll be watching the screen. With the rule in place, nothing asks you before the agent clicks, types, or posts as you.
3. **Give the agent the instructions below.** Put them in its project instructions file (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex, `GEMINI.md` for Gemini CLI), or paste them into the chat.

### Instructions to give the agent

```markdown
## Desktop control
You can see and control this computer with the `ai-desktop` CLI
(run `ai-desktop`, or `python3 ai_desktop.py` from its folder).

Work in a loop:
1. `ai-desktop screenshot --grid -o /tmp/screen.png`, then open /tmp/screen.png and look at it.
2. Read the target's coordinates off the labeled grid. Image pixels are click coordinates.
3. Do ONE action: `click`, `type`, `key`, `hotkey`, `scroll`, `paste --text`, `copy`.
4. Take a new screenshot and check that the action worked before doing the next one.

Rules:
- Each command prints `OK ...` on success, or `ERROR ...` with exit code 1.
  Exit code 3 means the human aborted with the failsafe: stop and ask.
- If a target is small or between grid lines, re-run the screenshot with `--spacing 50`.
- `type` only handles ASCII. Use `paste --text "..."` for other characters or long text.
- `copy` prints the copied text after its first line.
- `launch-chrome --url URL` opens the user's signed-in Chrome. The window may open
  behind others (GNOME shows a "... is ready" notification; click it). Screenshot to check.
- Never type passwords, 2FA codes or payment details. Stop and ask the human.
- Ask the human before anything public or irreversible: posting, sending,
  buying, deleting, accepting terms.
```

### Building your own agent

If you're writing your own agent against a model API:
- **Give the model one tool**, say `run(command)`, that runs an `ai-desktop` command and returns its output.
- **Return the picture after a screenshot.** When the command is `screenshot`, send the PNG back in the tool result as an image, so the model actually sees it.
- **Use the instructions above as the system prompt.**

That is the whole loop: the model reads a screenshot, picks a command, sees the result, and repeats.

### Is this the right tool?

- **Web-only tasks.** Browser automation that reads the page's structure is usually faster and more reliable than clicking pixels, because the agent can target elements by their text or role instead of guessing coordinates. Examples are Playwright, browser-use, or Claude in Chrome.
- **Isolated testing.** A sandboxed setup, such as a VM or a Docker desktop like Anthropic's computer-use reference implementation, keeps the agent away from your real accounts.
- **What this tool is for.** Driving *your real desktop and signed-in apps* from *any* agent that has a shell. There's no server to run, it works on native Wayland, and it's one Python file.

## Using your signed-in Chrome

```bash
python ai_desktop.py launch-chrome --url https://mail.google.com
python ai_desktop.py screenshot --grid      # confirm you're signed in
```

`launch-chrome` opens your normal Chrome with your existing cookies, logins and extensions. If Chrome is already running, it opens a new window in that same session.
- **Windows:** finds `chrome.exe` under Program Files, `%LOCALAPPDATA%` or the registry, and points it at `%LOCALAPPDATA%\Google\Chrome\User Data`.
- **macOS:** runs `open -a "Google Chrome"`.
- **Linux:** looks for `google-chrome`/`chromium` on PATH.

Chrome opens the profile you used last. To pick a different one, pass `--profile` with the folder name, e.g. `"Profile 1"`. Folder names are listed under `chrome://version` → *Profile Path*.

Remember that the agent now acts as **you** on every site you're logged into.

## Platform notes

### macOS
Under **System Settings → Privacy & Security**, grant your terminal or IDE app (Terminal, iTerm, VS Code, …) both of these, then quit and reopen it:
- **Screen Recording.** Without it, screenshots show only the wallpaper and menu bar, with no windows.
- **Accessibility.** Without it, clicks and keystrokes are silently ignored.

Other notes:
- Use `cmd` in hotkeys (`hotkey --keys cmd+t`). `ctrl+c` on a Mac is Control-C, not copy.
- Scroll units are lines and feel small. Raise `--amount` if needed.

### Windows
- **Scaling.** 100% display scaling is recommended. Other scales work on the primary monitor because screenshot and clicks share the same physical-pixel coordinates.
- **Admin windows.** Input can't reach windows running as administrator (Task Manager, installers, UAC prompts) unless the tool itself runs from an elevated terminal.
- **Scroll.** `--amount` counts mouse-wheel notches.

### Linux on X11
In an X11 session, such as **"Ubuntu on Xorg"** picked from the gear icon on the login screen, everything goes through pyautogui exactly as on Windows and macOS, failsafe included.

### Linux on Wayland
pyautogui can't see or control native Wayland windows, so on Wayland the tool switches automatically:
- **Screenshots** use `gnome-screenshot` (GNOME), `grim` (Sway, Hyprland) or `spectacle` (KDE).
- **Input** goes through the kernel's `/dev/uinput`, using short-lived virtual mouse and keyboard devices. This needs no extra Python packages.

One-time setup, tested on Ubuntu 24.04 / GNOME 46:

```bash
sudo apt install gnome-screenshot xclip

# Let the logged-in user create virtual input devices (same rule Steam uses)
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/60-ai-desktop-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
getfacl /dev/uinput     # should list user:<you>:rw-  (if not, reboot)
```

Wayland caveats:
- **Security.** Any program running as you can now inject keyboard and mouse input. Delete the rule file to undo it.
- **US layout only.** `type` assumes the **US keyboard layout** is active, since it sends physical key positions. With another layout, symbols will come out wrong. `paste --text` doesn't depend on the layout.
- **Speed.** Each action takes about 0.6 s, because the compositor needs a moment to notice each new virtual device. If clicks are dropped, raise `AI_DESKTOP_UINPUT_DELAY` (default `0.5` seconds).
- **No pointer position.** `position` reports `x=unknown y=unknown`.
- **Screen size.** It is read from Xwayland. If that fails, set `AI_DESKTOP_SCREEN_SIZE=1920x1080`.
- **Choosing a backend.** Use `--backend pyautogui|uinput` (or `AI_DESKTOP_BACKEND`) to override the automatic choice.

## Known limits
- No OCR, template matching, or window-specific screenshots.
- **Single screen.** Windows and macOS capture and address the primary screen only. On Linux, the screenshot and the coordinates cover the whole desktop.
- **ASCII only.** `type` handles printable ASCII, newline and tab, and rejects anything else (é, emoji, CJK) with an error. Use `paste --text` for those.
- **Scroll distance** per notch varies by app and platform.
