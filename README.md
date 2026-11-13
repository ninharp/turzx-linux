# turzx-linux

Linux replacement for the Windows app **TURZX V3.1.0** for the TURZX 8.8" USB
display (480×1920, USB `1cbe:0088`). The USB protocol, the theme format and the
widget drawing logic were reconstructed from the Windows app (`UsbMonitorL.exe`);
see [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Features

| Command | What it does |
|---|---|
| `turzx info` | Find the display, show firmware and serial number |
| `turzx theme FILE` | Play a theme (`.turtheme` from the Windows app or `.turzx` from the editor): sensor values (CPU/GPU/RAM/disk/network/volume), date/time, bars, arcs, gauges, charts; video themes play the video in the background with the values as an overlay |
| `turzx editor [FILE]` | Graphical theme editor (see "Theme editor") |
| `turzx control` | Control window: brightness, backlight on/off, restart display, choose/start/stop themes (see "Display control") |
| `turzx preview FILE -o x.png` | Render a theme to PNG without the display |
| `turzx image IMAGE` | Show an image |
| `turzx video FILE` | Play any video file (ffmpeg) |
| `turzx desktop --window pick` | **Window mode**: click a window (or use `--window TITLE`) and it appears on the display, even when covered by other windows; `turzx windows` lists windows |
| `turzx desktop --monitor DP-2` | **Desktop mode** with a real extra monitor (see "NVIDIA ghost monitor"), or mirror any monitor |
| `turzx desktop --region X,Y,WxH` | Mirror a screen area |
| `turzx desktop --virtual` | Experimental: monitor declared via xrandr without an output; under X11 the pointer cannot reach it (see below) |
| `turzx nvidia-setup` | Generate a root script and EDID for a real 1920×480 extra monitor |
| `turzx brightness 0-100`, `turzx off`, `turzx on`, `turzx reboot` | Brightness, backlight off/on, restart the display; work while a theme is running |
| `turzx status`, `turzx stop` | Show / stop what is currently running on the display |
| `turzx clear` | Clear the display |

All modes run until Ctrl+C. For the virtual monitor, the screen layout and
window positions are saved beforehand and restored on exit.

## Installation

```sh
./install.sh ../TURZX-V3.1.0-ENG ../88inchthemeENG   # package + copy fonts/themes/videos
sudo cp udev/99-turzx.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
```

Dependencies: Python ≥ 3.10, `pyusb`, `pycryptodome`, `Pillow`, `psutil`,
`fonttools`, `numpy`, `python-xlib` (desktop mode), `PySide6` (editor),
`ffmpeg`, `xrandr`. GPU values: NVIDIA via `nvidia-smi`, AMD via sysfs.

Running directly from the source directory without installing:

```sh
python3 -m turzx theme "../88inchthemeENG/theme/4801920/Dragon Ball.turtheme" \
    --font-dir ../TURZX-V3.1.0-ENG/fonts --video-dir ../88inchthemeENG/video/4801920
python3 -m turzx editor --font-dir ../TURZX-V3.1.0-ENG/fonts --video-dir ../88inchthemeENG/video/4801920
```

### Autostart (systemd user service)

```sh
mkdir -p ~/.config/turzx
echo 'TURZX_ARGS=theme "/home/USER/.local/share/turzx/themes/My Theme.turzx"' > ~/.config/turzx/turzx.env
systemctl --user enable --now turzx.service
```

## Theme editor

`turzx editor` opens an editor for your own themes; `.turtheme` files from the
Windows app can be imported via Open.

* **Left:** widget list. The checkbox shows/hides a widget, ▲/▼ change the
  drawing order, "+ Add" adds a sensor value, text, image, bar, arc/ring,
  gauge or history chart; plus Duplicate and Delete.
* **Center:** preview with live sensor values, updated every second. Video
  themes show a still frame of the video underneath.
  Click and drag widgets; arrow keys move by 1 px (Shift: 10 px);
  Ctrl+mouse wheel zooms, Ctrl+0 fits the view.
* **Right:** properties of the selected widget (sensor and format, font,
  size, color with alpha, alignment, history, bar/arc parameters, images).
  Without a selection: theme settings (name, background image, background
  video, frame rate).
* **Toolbar:** New, Open, Save, Save as, Undo (Ctrl+Z), Redo (Ctrl+Y),
  Export PNG.
* **Display row:** **Live on display** shows every change immediately on the
  device; brightness, backlight on/off and restart (see "Display control").

Themes are saved as `.turzx` in `~/.local/share/turzx/themes/` by default.
A `.turzx` file is a zip archive containing `theme.json` (format
`turzx-theme/1`), the widget images under `assets/` and, if used, the
background video under `video/`, so a theme is a single self-contained file.

## Display control

Only one process can use the display at a time. The process that plays a
theme, video or desktop (and the editor's "Live on display") therefore listens
on a control socket (`$XDG_RUNTIME_DIR/turzx.sock`). `turzx brightness`,
`off`, `on`, `reboot`, `status` and `stop` send their command there; when
nothing is running they open the display directly.

In the editor, the second toolbar row ("Display") has "Live on display", the
brightness slider, backlight on/off and "Restart display"; it shows what is
running on the display. "Live on display" takes the display over from a running
theme.

Without the editor, `turzx control` is a small window for the same commands
plus a theme list: double-click or "Play" stops the running
theme and starts the selected one in the background (log:
`~/.cache/turzx/player.log`). It lists `.turzx` and `.turtheme` files from
`~/.local/share/turzx/themes` and `~/.local/share/turzx/theme`; "Add…" adds
other folders, `--theme-dir`/`--font-dir`/`--video-dir` set them on the command
line.

The brightness is remembered in `~/.config/turzx/settings.json` and used by
every mode on start; `--brightness N` overrides it for one run. "Restart
display" reboots the display and reconnects; a running theme continues
afterwards.

## NVIDIA ghost monitor (real desktop mode)

The X server only lets the pointer move over areas shown by a real video
output. A plain `xrandr --setmonitor` area is therefore unreachable, and an
enlarged screen makes the NVIDIA driver pan the whole layout with the mouse.
Solution: the driver reports a "ghost monitor" with a 1920×480 EDID on a free
connector.

```sh
turzx nvidia-setup -o setup/turzx-nvidia-setup.sh   # detects a free connector (here DFP-2 = DP-2)
sudo sh setup/turzx-nvidia-setup.sh                 # adds ConnectedMonitor + CustomEDID (with backup)
# restart X, arrange monitor "TURZX 8.8" in the XFCE display settings
turzx desktop --monitor DP-2
sudo sh setup/turzx-nvidia-setup.sh uninstall       # undo
```

Emergency, if X does not start: switch to a console (Ctrl+Alt+F2) and run
`sudo cp /etc/X11/xorg.conf.d/90-mhwd.conf.turzx-backup /etc/X11/xorg.conf.d/90-mhwd.conf`.
Note: `mhwd` may rewrite this file when the driver is reinstalled.

## Notes

* **Power supply:** connected directly to a mainboard port, the display
  dropped off the bus after ~14 s (kernel: `error -71`, `Cannot enable`).
  It runs stable on a powered USB hub.
* **If the display hangs:** unplug and replug the USB cable. Do not trigger a
  USB reset in software: that blocked the whole xHCI controller for about
  2 minutes.
* **Backlight pulsing:** the display's backlight can pulse until a brightness
  command is received, so every mode sets the brightness on start (the last
  value set, initially 80).
* **Rotation:** landscape themes are rotated 90° clockwise, as in the
  Windows app; use `--rotate 270` to turn the image the other way round.
* **Native firmware desktop mode** (command 150): the display then reboots as
  `1A86:AD11` and needs the Windows IddCx driver, so it is not used on Linux;
  `turzx desktop` achieves the same over the normal protocol.
* Differences from the Windows app: bars for RAM/GPU RAM use "used / total"
  instead of an app bug that always shows them full. Fans, voltages, FPS and
  weather are not available on Linux or depend on the hardware.
