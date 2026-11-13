#!/bin/sh
# TURZX: real 1920x480 ghost monitor on DFP-2 (DP-2, Connector-1)
#   sudo sh turzx-nvidia-setup.sh            install
#   sudo sh turzx-nvidia-setup.sh uninstall  revert
# Then restart X (log out/in or reboot).
# Emergency (X does not start): log in on a console (Ctrl+Alt+F2) and run
#   sudo cp /etc/X11/xorg.conf.d/90-mhwd.conf.turzx-backup /etc/X11/xorg.conf.d/90-mhwd.conf
set -e
CONF="/etc/X11/xorg.conf.d/90-mhwd.conf"
if [ "$1" = "uninstall" ]; then
    sed -i '/# turzx-begin/,/# turzx-end/d' "$CONF"
    rm -f /etc/X11/turzx-1920x480.edid
    echo "removed - restart X"
    exit 0
fi
[ -f "$CONF.turzx-backup" ] || cp "$CONF" "$CONF.turzx-backup"
install -m 644 turzx-nvidia-setup.edid /etc/X11/turzx-1920x480.edid
sed -i '/# turzx-begin/,/# turzx-end/d' "$CONF"
# insert into the Device section right after 'Driver "nvidia"'
sed -i '/^[[:space:]]*Driver[[:space:]]*"nvidia"/a\
    # turzx-begin\
    Option "ConnectedMonitor" "DFP-0, DFP-4, DFP-6, DFP-2"\
    Option "CustomEDID" "DFP-2:/etc/X11/turzx-1920x480.edid"\
    # turzx-end' "$CONF"
echo "added to $CONF (backup: $CONF.turzx-backup)"
echo "After restarting X, DP-2 shows up as monitor 'TURZX 8.8' (1920x480)."
echo "Arrange it in the display settings, then run: turzx desktop --monitor DP-2"
