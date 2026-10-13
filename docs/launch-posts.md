# Launch post drafts (not posted)

Drafts for Michael to review and post himself.

## Show HN

**Title:** Show HN: ai-desktop – a CLI and MCP server that lets AI agents see and drive your desktop

**Body:**

I'm a CS teacher and software engineer, and I built ai-desktop because I wanted any AI agent that can run shell commands and read images to use my actual computer, not a sandboxed VM.

It's one Python file plus an optional MCP server. `ai-desktop screenshot --grid` saves a screenshot with labeled coordinate gridlines, so the model reads coordinates straight off the image. Then `click`, `type`, `hotkey`, `scroll`, `drag`, `focus` and the clipboard commands act on them. It handles multiple monitors (global coordinates, or `--monitor N` for coordinates relative to one screen), works on Windows, macOS, Linux X11 and Wayland (through uinput), and writes a JSON action log of everything the agent did. It can also launch your real, signed-in Chrome.

The README GIF is a scripted run: every keystroke, click and window drag in it comes from the CLI.

    pip install ai-desktop            # add [mcp] for the MCP server

https://github.com/MichaelDKulka/ai-desktop

The caveat is obvious: whatever runs this has full control of your desktop and logged-in accounts. On Windows, macOS and X11 there's a corner failsafe (slam the mouse into a corner and every command aborts). I'd like feedback on the grid approach compared with pure vision models, and on what's missing.

## r/LocalLLaMA

**Title:** I built ai-desktop: a CLI + MCP server so any local or hosted model can see and control your real desktop

**Body:**

Hi all. I'm a CS teacher and software engineer, and I made a small open-source tool for giving agents hands on a real desktop.

How it works: `ai-desktop screenshot --grid` produces a screenshot with labeled coordinate gridlines, so a vision model doesn't have to guess pixel positions; it reads them off the grid. The agent then calls `click`, `type`, `hotkey`, `scroll`, `drag`, `focus` and so on. Anything that can run shell commands can use it, and `ai-desktop mcp` exposes the same commands as MCP tools with inline screenshots.

- Windows, macOS, Linux X11 and Wayland
- Multi-monitor, with optional monitor-relative coordinates
- JSON action log of every command (typed text is reduced to a character count by default)
- Corner failsafe to abort

`pip install ai-desktop` (or `pip install 'ai-desktop[mcp]'`)

https://github.com/MichaelDKulka/ai-desktop

Be careful: it gives the model full control of your machine and signed-in accounts. I'd love to hear which local vision models handle the grid well for you.

## X

I built ai-desktop: a CLI + MCP server that lets AI agents see your screen through a coordinate grid and click, type, drag and scroll on your real desktop. Windows/macOS/Linux, multi-monitor.

pip install ai-desktop
https://github.com/MichaelDKulka/ai-desktop
