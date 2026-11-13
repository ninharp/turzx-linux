"""Create a root script that adds a real 1920x480 'ghost' monitor on a free
NVIDIA connector (ConnectedMonitor + CustomEDID). After an X restart the
connector shows up as a normal monitor (e.g. DP-2) that can be arranged in the
XFCE display settings; `turzx desktop --monitor DP-2` streams it."""
from __future__ import annotations

import glob
import os
import re
import subprocess

from .edid import make_edid

EDID_PATH = "/etc/X11/turzx-1920x480.edid"


def _dpys() -> list[dict]:
    out = subprocess.run(["nvidia-settings", "-q", "dpys"], capture_output=True, text=True).stdout
    dpys, cur = [], None
    for line in out.splitlines():
        m = re.match(r"\s+\[\d+\] \S+\[dpy:\d+\] \((\S+)\)(.*)", line)
        if m:
            cur = {"xrandr": m.group(1), "connected": "connected" in m.group(2), "dfp": None, "connector": None}
            dpys.append(cur)
            continue
        if cur is not None:
            t = line.strip()
            if re.fullmatch(r"(DFP|CRT|TV)-\d+", t):
                cur["dfp"] = t
            elif t.startswith("Connector-"):
                cur["connector"] = t
    return dpys


def _xorg_conf() -> str | None:
    for f in sorted(glob.glob("/etc/X11/xorg.conf.d/*.conf")) + ["/etc/X11/xorg.conf"]:
        try:
            if re.search(r'Driver\s+"nvidia"', open(f).read()):
                return f
        except OSError:
            continue
    return None


def write_setup(path: str, connector: str | None = None) -> int:
    dpys = _dpys()
    if not dpys:
        print("nvidia-settings reports no displays - is the NVIDIA driver running under X11?")
        return 1
    used_conn = {d["connector"] for d in dpys if d["connected"]}
    connected = [d["dfp"] for d in dpys if d["connected"] and d["dfp"]]
    if connector:
        target = next((d for d in dpys if d["dfp"] == connector), None)
    else:
        target = next((d for d in dpys if not d["connected"] and d["connector"] not in used_conn), None)
    if not target:
        print("no free connector found")
        return 1
    conf = _xorg_conf()
    if not conf:
        print("no X configuration with Driver \"nvidia\" found")
        return 1
    edid_local = os.path.abspath(os.path.splitext(path)[0] + ".edid")
    with open(edid_local, "wb") as fh:
        fh.write(make_edid())
    monitors = ", ".join(connected + [target["dfp"]])
    script = f"""#!/bin/sh
# TURZX: real 1920x480 ghost monitor on {target['dfp']} ({target['xrandr']}, {target['connector']})
#   sudo sh {os.path.basename(path)}            install
#   sudo sh {os.path.basename(path)} uninstall  revert
# Then restart X (log out/in or reboot).
# Emergency (X does not start): log in on a console (Ctrl+Alt+F2) and run
#   sudo cp {conf}.turzx-backup {conf}
set -e
CONF="{conf}"
if [ "$1" = "uninstall" ]; then
    sed -i '/# turzx-begin/,/# turzx-end/d' "$CONF"
    rm -f {EDID_PATH}
    echo "removed - restart X"
    exit 0
fi
[ -f "$CONF.turzx-backup" ] || cp "$CONF" "$CONF.turzx-backup"
install -m 644 "{edid_local}" {EDID_PATH}
sed -i '/# turzx-begin/,/# turzx-end/d' "$CONF"
# insert into the Device section right after 'Driver "nvidia"'
sed -i '/^[[:space:]]*Driver[[:space:]]*"nvidia"/a\\
    # turzx-begin\\
    Option "ConnectedMonitor" "{monitors}"\\
    Option "CustomEDID" "{target['dfp']}:{EDID_PATH}"\\
    # turzx-end' "$CONF"
echo "added to $CONF (backup: $CONF.turzx-backup)"
echo "After restarting X, {target['xrandr']} shows up as monitor 'TURZX 8.8' (1920x480)."
echo "Arrange it in the display settings, then run: turzx desktop --monitor {target['xrandr']}"
"""
    with open(path, "w") as fh:
        fh.write(script)
    print(f"script: {os.path.abspath(path)}")
    print(f"EDID:   {edid_local}")
    print(f"target: {target['dfp']} = {target['xrandr']} ({target['connector']}), "
          f"ConnectedMonitor = {monitors}")
    print(f"install: sudo sh {path}   (then restart X)")
    return 0
