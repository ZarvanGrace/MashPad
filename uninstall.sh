#!/usr/bin/env bash
# MashPad uninstaller.
#
#   ./uninstall.sh        asks before removing anything
#   ./uninstall.sh -y     no questions
#
# Removes what install.sh created. It does NOT remove apt packages (python3-dev etc.)
# and does NOT delete the git clone you ran install.sh from.
set -euo pipefail

APP_DIR="$HOME/.local/share/mashpad"
LAUNCHER="$HOME/.local/bin/mashpad"
UDEV_RULE="/etc/udev/rules.d/70-mashpad-uinput.rules"
MODULES_FILE="/etc/modules-load.d/mashpad-uinput.conf"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)  YES=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         die "Unknown option: $arg (try --help)" ;;
  esac
done

[ "$(id -u)" -ne 0 ] || die "Don't run this as root. It cleans up your user's install and uses sudo only where needed."

ask() {  # ask "question"  -> true if yes
  [ "$YES" -eq 1 ] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]]
}

if pgrep -f "$APP_DIR/app.py" >/dev/null 2>&1; then
  warn "MashPad is running right now. Stop it first (Ctrl+C in its terminal), then run this again."
  exit 1
fi

# ---- app + command ---------------------------------------------------------
if [ -e "$LAUNCHER" ]; then
  rm -f "$LAUNCHER"
  say "Removed $LAUNCHER"
fi

if [ -d "$APP_DIR" ]; then
  [ -e "$APP_DIR/app.py" ] || die "$APP_DIR doesn't look like a MashPad install. Not deleting it."
  if ask "Delete $APP_DIR (app, venv AND your config.json)?"; then
    rm -rf "$APP_DIR"
    say "Removed $APP_DIR"
  else
    say "Kept $APP_DIR"
  fi
else
  say "No app folder at $APP_DIR, nothing to remove."
fi

# ---- uinput setup ----------------------------------------------------------
if [ -e "$UDEV_RULE" ] || [ -e "$MODULES_FILE" ]; then
  if ask "Remove the uinput access rule MashPad added (needs sudo)? Other apps like Steam or Sunshine may rely on it."; then
    sudo rm -f "$UDEV_RULE" "$MODULES_FILE"
    sudo udevadm control --reload-rules
    say "Removed uinput rule. (The uinput module stays loaded until you reboot.)"
  else
    say "Kept the uinput rule."
  fi
fi

say "MashPad is gone. You can delete the cloned folder yourself if you want."
