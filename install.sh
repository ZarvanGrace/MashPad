#!/usr/bin/env bash
# MashPad installer. Run it from the cloned repo:  ./install.sh
#
#   ./install.sh               full install (asks for sudo for system packages + uinput access)
#   ./install.sh --no-system   skip apt + uinput setup (no sudo, you handle those yourself)
#
# What ends up where:
#   ~/.local/share/mashpad/   app files, config.json and the Python venv
#   ~/.local/bin/mashpad      the command that starts the server
set -euo pipefail

APP_DIR="$HOME/.local/share/mashpad"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/mashpad"
UDEV_RULE="/etc/udev/rules.d/70-mashpad-uinput.rules"
MODULES_FILE="/etc/modules-load.d/mashpad-uinput.conf"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

SYSTEM=1
for arg in "$@"; do
  case "$arg" in
    --no-system) SYSTEM=0 ;;
    -h|--help)   sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "Unknown option: $arg (try --help)" ;;
  esac
done

[ "$(id -u)" -ne 0 ] || die "Don't run this as root. It installs for your user and uses sudo only where needed."

for f in app.py requirements.txt config.json assets/pad.html js/qrcode.js js/jsQR.js; do
  [ -e "$SRC/$f" ] || die "Missing $f. Run this script from inside the full cloned MashPad folder."
done

# ---- 1. system packages (Debian/Ubuntu) ------------------------------------
# evdev has no prebuilt wheels, so pip compiles it: that needs python3-dev and a C compiler.
if [ "$SYSTEM" -eq 1 ]; then
  if command -v apt-get >/dev/null 2>&1 && command -v dpkg >/dev/null 2>&1; then
    need=()
    for pkg in python3 python3-venv python3-dev build-essential openssl iw; do
      dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
    done
    if [ "${#need[@]}" -gt 0 ]; then
      say "Installing system packages: ${need[*]}"
      sudo apt-get install -y "${need[@]}" \
        || { sudo apt-get update && sudo apt-get install -y "${need[@]}"; }
    else
      say "System packages already installed."
    fi
  else
    warn "This isn't a Debian-style system, so I won't touch your packages."
    warn "Make sure you have: python3 + venv, Python headers, a C compiler, openssl."
  fi

  # ---- 2. uinput: lets your normal user create virtual gamepads ------------
  # Without this MashPad dies with "Can't create virtual gamepads".
  # TAG+="uaccess" gives the user sitting at the machine access to /dev/uinput,
  # no groups and no re-login needed. Same trick Steam uses.
  say "Setting up /dev/uinput access (sudo)..."
  sudo modprobe uinput || warn "modprobe uinput failed. Your kernel may lack uinput support."
  echo "uinput" | sudo tee "$MODULES_FILE" >/dev/null
  echo 'KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"' \
    | sudo tee "$UDEV_RULE" >/dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput || true
  sleep 1
  if [ -w /dev/uinput ]; then
    say "/dev/uinput is writable. Good."
  else
    warn "/dev/uinput isn't writable yet. Log out and back in, then run: mashpad"
    warn "(Over SSH or without a desktop session, uaccess does nothing. See README > Troubleshooting.)"
  fi
else
  say "Skipping system setup (--no-system)."
fi

# ---- 3. copy the app -------------------------------------------------------
say "Copying MashPad to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/assets" "$APP_DIR/js"        # drop stale files from older versions
cp -a "$SRC/app.py" "$SRC/requirements.txt" "$SRC/assets" "$SRC/js" "$APP_DIR/"
for f in LICENSE THIRD_PARTY_LICENSES.md; do
  [ -e "$SRC/$f" ] && cp -a "$SRC/$f" "$APP_DIR/"
done
if [ -e "$APP_DIR/config.json" ]; then
  say "Keeping your existing config.json"
else
  cp -a "$SRC/config.json" "$APP_DIR/"
fi

# ---- 4. virtualenv + dependencies ------------------------------------------
say "Creating virtualenv and installing Python packages (evdev compiles, give it a moment)..."
python3 -m venv --clear "$APP_DIR/venv" \
  || die "venv creation failed. On Debian: sudo apt install python3-venv"
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" \
  || die "pip failed. If evdev failed to build you need: sudo apt install python3-dev build-essential"

# ---- 5. the `mashpad` command ----------------------------------------------
say "Creating the mashpad command: $LAUNCHER"
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" "$APP_DIR/app.py" "\$@"
EOF
chmod +x "$LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR isn't in your PATH yet. Log out and back in (Debian adds it automatically"
     warn "once the folder exists), or run: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
esac

say "Done. Start the server with:  mashpad"
say "Config file: $APP_DIR/config.json"
