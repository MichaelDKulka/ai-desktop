#!/usr/bin/env bash
# One-shot setup for macOS and Linux: virtualenv, install, Linux extras, then doctor.
#   ./setup.sh          install
#   ./setup.sh --mcp    also install the MCP server extra (Python 3.10+)
#   ./setup.sh --dev    also install pytest
#   ./setup.sh --yes    run the Linux sudo steps without asking
set -euo pipefail
cd "$(dirname "$0")"

DEV=0
MCP=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --dev) DEV=1 ;;
    --mcp) MCP=1 ;;
    --yes | -y) YES=1 ;;
    -h | --help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- Python ------------------------------------------------------------------------------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is required." >&2
  case "$(uname -s)" in
    Darwin) echo "Install it with: brew install python   (or from https://python.org)" >&2 ;;
    *) echo "Install it with: sudo apt install python3 python3-venv" >&2 ;;
  esac
  exit 1
fi
echo "Using $("$PY" --version) at $(command -v "$PY")"
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
  echo "The venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv" >&2
  exit 1
fi

# --- Virtualenv + install ----------------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
EXTRAS=""
[ "$DEV" = 1 ] && EXTRAS="dev"
[ "$MCP" = 1 ] && EXTRAS="${EXTRAS:+$EXTRAS,}mcp"
.venv/bin/python -m pip install --quiet -e ".${EXTRAS:+[$EXTRAS]}"
echo "Installed $(.venv/bin/ai-desktop --version) into .venv"

# --- Linux extras ------------------------------------------------------------------------
if [ "$(uname -s)" = Linux ]; then
  SESSION="${XDG_SESSION_TYPE:-}"
  if [ -z "$SESSION" ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then SESSION=wayland; fi
  PKGS=""
  if ! command -v gnome-screenshot >/dev/null && ! command -v grim >/dev/null && ! command -v spectacle >/dev/null; then
    PKGS="gnome-screenshot"
  fi
  if ! command -v xclip >/dev/null && ! command -v xsel >/dev/null && ! command -v wl-copy >/dev/null; then
    PKGS="$PKGS xclip"
  fi
  NEED_UDEV=0
  if [ "$SESSION" = wayland ] && [ ! -w /dev/uinput ]; then NEED_UDEV=1; fi

  if [ -n "$PKGS" ] || [ "$NEED_UDEV" = 1 ]; then
    echo
    echo "This Linux session (${SESSION:-unknown}) still needs, with sudo:"
    [ -n "$PKGS" ] && echo "  sudo apt install$( [ -n "$PKGS" ] && echo " $PKGS" | tr -s ' ')"
    if [ "$NEED_UDEV" = 1 ]; then
      echo "  a udev rule so your user may write to /dev/uinput (the virtual mouse and keyboard):"
      echo "    /etc/udev/rules.d/60-ai-desktop-uinput.rules"
    fi
    RUN=0
    if [ "$YES" = 1 ]; then
      RUN=1
    elif [ -t 0 ]; then
      printf 'Run these now? [y/N] '
      read -r answer
      case "$answer" in y | Y | yes | YES) RUN=1 ;; esac
    else
      echo "(not a terminal: skipping; re-run with --yes to apply)"
    fi
    if [ "$RUN" = 1 ]; then
      if [ -n "$PKGS" ]; then
        if command -v apt-get >/dev/null; then
          # shellcheck disable=SC2086
          sudo apt-get install -y $PKGS
        else
          echo "No apt-get here; install$PKGS with your package manager." >&2
        fi
      fi
      if [ "$NEED_UDEV" = 1 ]; then
        echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' |
          sudo tee /etc/udev/rules.d/60-ai-desktop-uinput.rules >/dev/null
        sudo udevadm control --reload-rules && sudo udevadm trigger && sudo udevadm settle || true
        [ -w /dev/uinput ] || echo "/dev/uinput is still not writable: log out and back in, or reboot." >&2
      fi
    fi
  fi
fi

# --- Doctor ------------------------------------------------------------------------------
echo
echo "Running ai-desktop doctor:"
.venv/bin/ai-desktop doctor || true
echo
echo "Done. Run it with:  .venv/bin/ai-desktop --help   (or: source .venv/bin/activate)"
if [ "$(uname -s)" = Darwin ]; then
  echo "macOS: grant Screen Recording and Accessibility to this terminal app (System Settings >"
  echo "Privacy & Security), quit and reopen the app, then run: .venv/bin/ai-desktop doctor"
fi
