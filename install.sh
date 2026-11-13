#!/bin/sh
# Install turzx-linux for the current user and import assets from the Windows package.
#   ./install.sh [/path/to/TURZX-V3.1.0-ENG] [/path/to/88inchthemeENG]
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
WIN=${1:-}
THEMES=${2:-}
DATA="$HOME/.local/share/turzx"

pip install --user --break-system-packages -e "$HERE" 2>/dev/null || pip install --user -e "$HERE"

mkdir -p "$DATA/fonts" "$DATA/theme" "$DATA/video" "$HOME/.config/turzx" "$HOME/.config/systemd/user"
for src in "$WIN" "$THEMES"; do
  [ -n "$src" ] && [ -d "$src" ] || continue
  [ -d "$src/fonts" ] && cp -n "$src/fonts/"* "$DATA/fonts/" 2>/dev/null || true
  [ -d "$src/theme" ] && cp -rn "$src/theme/." "$DATA/theme/"
  [ -d "$src/video" ] && cp -rn "$src/video/." "$DATA/video/"
done
cp "$HERE/systemd/turzx.service" "$HOME/.config/systemd/user/"
[ -f "$HOME/.config/turzx/turzx.env" ] || echo 'TURZX_ARGS=info' > "$HOME/.config/turzx/turzx.env"

echo
echo "Done. For USB access without root, once (as root):"
echo "  sudo cp $HERE/udev/99-turzx.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger"
echo "Test:      turzx info"
echo "Theme:     turzx theme \"$DATA/theme/4801920/Dragon Ball.turtheme\" --video-dir $DATA/video/4801920"
echo "Window:    turzx desktop --window pick"
echo "Editor:    turzx editor"
