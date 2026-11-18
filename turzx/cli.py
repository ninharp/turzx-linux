"""turzx - Linux control tool for TURZX USB displays."""
from __future__ import annotations

import argparse
import logging
import os
import sys

DEFAULT_FONT_DIRS = [os.path.expanduser("~/.local/share/turzx/fonts")]
DEFAULT_VIDEO_DIRS = [os.path.expanduser("~/.local/share/turzx/video")]


def _with_settings(cli_dirs: list[str], key: str, defaults: list[str]) -> list[str]:
    """Command line dirs first, then the ones in settings.json (also used by the
    service and saved by the editor), then the defaults."""
    from .control import load_settings

    out: list[str] = []
    for d in list(cli_dirs) + list(load_settings().get(key, [])) + defaults:
        if d and d not in out:
            out.append(d)
    return out


def _rot(v: str) -> int:
    r = int(v)
    if r not in (0, 90, 180, 270):
        raise argparse.ArgumentTypeError("0, 90, 180 or 270")
    return r


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    ap = argparse.ArgumentParser(prog="turzx", description=__doc__,
                                 epilog=f"turzx {__version__} - 'turzx --version' shows details")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-V", "--version", action="store_true", help="show version information and exit")
    ap.add_argument("--serial", help="select a display by USB serial number")
    ap.add_argument("--brightness", type=int, default=None,
                    help="brightness 0-100 for this run (default: last value set, initially 80)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("info", help="find the display and show its firmware")
    p = sub.add_parser("brightness", help="set brightness (also while a theme/video/desktop is running)")
    p.add_argument("percent", type=int)
    sub.add_parser("status", help="show what is currently running on the display")
    sub.add_parser("off", help="switch the backlight off (picture keeps running)")
    sub.add_parser("on", help="switch the backlight back on")
    sub.add_parser("stop", help="stop the running theme/video/desktop")
    p = sub.add_parser("control", help="control window: brightness, on/off, reboot, theme selection")
    p.add_argument("--theme-dir", action="append", default=[], help="additional theme directory")
    p.add_argument("--font-dir", action="append", default=[], help="passed on to the theme player")
    p.add_argument("--video-dir", action="append", default=[], help="passed on to the theme player")
    p = sub.add_parser("image", help="show an image")
    p.add_argument("file")
    p.add_argument("--rotate", type=_rot)
    p.add_argument("--fit", choices=["cover", "fit", "stretch"], default="cover")
    sub.add_parser("clear", help="clear the overlay layer and show black")

    p = sub.add_parser("theme", help="play a .turtheme/.turzx theme (sensors, clock, video)")
    p.add_argument("file")
    p.add_argument("--rotate", type=_rot, help="rotation of the theme image (default: 90 for landscape)")
    p.add_argument("--interval", type=float, default=1.0, help="update interval in seconds")
    p.add_argument("--video-dir", action="append", default=[], help="search path for theme videos")
    p.add_argument("--font-dir", action="append", default=[], help="additional font directory")
    p.add_argument("--no-video", action="store_true")

    p = sub.add_parser("preview", help="render a theme to PNG (no display needed)")
    p.add_argument("file")
    p.add_argument("-o", "--output", default="preview.png")
    p.add_argument("--font-dir", action="append", default=[])
    p.add_argument("--device", action="store_true", help="output in display orientation (480x1920)")

    p = sub.add_parser("video", help="play a video file")
    p.add_argument("file")
    p.add_argument("--rotate", type=_rot, default=None,
                   help="default: 90 for landscape videos, 0 for portrait")
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--fit", choices=["cover", "fit", "stretch"], default="cover")
    p.add_argument("--once", action="store_true", help="play once instead of looping")

    p = sub.add_parser("desktop", help="desktop mode (X11): stream screen content to the display")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--virtual", nargs="?", const="free", metavar="POS",
                   help="experimental: declare a virtual monitor via xrandr: 'free' (default: largest unused 4:1 "
                        "area inside the layout), 'free:DP-0', 'below', 'right' or 'X,Y' (these enlarge the "
                        "X screen - NVIDIA then pans the layout). X11 keeps the pointer off areas without "
                        "a real output; use 'nvidia-setup' for a real monitor")
    g.add_argument("--monitor", help="show a monitor on the display, e.g. HDMI-0 or the "
                                     "NVIDIA ghost monitor DP-2 (see 'turzx nvidia-setup')")
    g.add_argument("--window", metavar="TITEL|ID|pick",
                   help="mirror a single window (title substring, window id, or 'pick' to click it)")
    g.add_argument("--region", help="mirror area X,Y,WxH, e.g. 0,0,1920x480")
    p.add_argument("--rotate", type=_rot, default=90)
    p.add_argument("--fps", type=float, default=20)
    p.add_argument("--no-cursor", action="store_true")

    sub.add_parser("reboot", help="reboot the display (a running theme continues afterwards)")
    p = sub.add_parser("play", help="switch the background service to a theme (and remember it); "
                                    "without FILE: continue the service theme")
    p.add_argument("file", nargs="?")
    sub.add_parser("daemon", help="run the background service in the foreground (used by systemd)")
    p = sub.add_parser("service", help="manage the systemd user service")
    p.add_argument("action", choices=["install", "uninstall", "status", "restart"])
    p.add_argument("--theme", help="theme the service plays")
    p.add_argument("--font-dir", action="append", default=[], help="font directory for the service")
    p.add_argument("--video-dir", action="append", default=[], help="theme video directory for the service")
    p = sub.add_parser("editor", help="graphical theme editor")
    p.add_argument("file", nargs="?", help=".turzx or .turtheme to open")
    p.add_argument("--font-dir", action="append", default=[])
    p.add_argument("--video-dir", action="append", default=[])
    sub.add_parser("windows", help="list windows for 'desktop --window'")
    p = sub.add_parser("nvidia-setup", help="generate a root script for a real 1920x480 ghost monitor (NVIDIA)")
    p.add_argument("--connector", help="free connector, e.g. DFP-2 (default: auto-detect)")
    p.add_argument("-o", "--output", default="turzx-nvidia-setup.sh")

    a = ap.parse_args(argv)
    if a.version:
        from . import version_text

        print(version_text())
        return 0
    if not a.cmd:
        ap.print_help()
        return 1
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("fontTools", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if a.cmd == "preview":
        return _preview(a)
    if a.cmd in ("brightness", "status", "off", "on", "stop", "reboot", "play"):
        return _control(a)
    if a.cmd == "daemon":
        from .daemon import Daemon

        return Daemon(a.serial, DEFAULT_FONT_DIRS, DEFAULT_VIDEO_DIRS).run()
    if a.cmd == "service":
        return _service(a)
    if a.cmd == "control":
        from .controlwin import run

        return run(a.font_dir, a.video_dir, a.theme_dir)
    if a.cmd == "editor":
        from .editor import run

        return run(a.file, _with_settings(a.font_dir, "font_dirs", DEFAULT_FONT_DIRS),
                   _with_settings(a.video_dir, "video_dirs", DEFAULT_VIDEO_DIRS))
    if a.cmd == "windows":
        from .x11windows import list_windows

        for wid, title, (x, y, w, h) in list_windows():
            print(f"{hex(wid):>11}  {w:>4}x{h:<4} @{x},{y}  {title}")
        return 0
    if a.cmd == "nvidia-setup":
        from .nvidia import write_setup

        return write_setup(a.output, a.connector)

    from .control import Borrow, request
    from .device import TurzxError

    if a.cmd == "info":
        r = request("status", timeout=2)
        if r and r.get("ok") and r.get("firmware"):  # display in use: ask its owner
            from . import __version__

            print(f"Model:    {r.get('model')}")
            print(f"Firmware: {r.get('firmware')}")
            print(f"Serial:   {r.get('serial')}")
            print(f"turzx:    {__version__}")
            return 0

    borrow = Borrow(hold=a.cmd in ("image", "clear"))
    try:
        borrow.__enter__()
        dev = borrow.open(a.serial)
    except TurzxError as e:
        borrow.__exit__(None, None, None)
        print(f"Error: {e}", file=sys.stderr)
        return 2
    try:
        info = dev.info
        if a.cmd == "info":
            from . import __version__

            print(f"Model:    {info.model} ({info.width}x{info.height})")
            print(f"Firmware: {info.firmware}")
            print(f"Serial:   {info.serial}")
            print(f"turzx:    {__version__}")
            return 0
        from . import app
        from .control import ControlServer, Controller

        ctl = Controller(dev)
        if a.brightness is not None:
            ctl.brightness = max(0, min(100, a.brightness))
        ctl.apply_brightness()

        if a.cmd == "clear":
            from PIL import Image

            dev.stop_playback()
            dev.clear_overlay()
            dev.show_image(Image.new("RGB", (dev.width, dev.height)))
            _hold_hint(borrow)
            return 0
        if a.cmd == "image":
            app.show_image(dev, a.file, a.rotate, a.fit)
            _hold_hint(borrow)
            return 0

        # long running modes: accept control commands while playing
        stop = app.StopFlag()
        ctl.on_stop = stop.ev.set
        ctl.mode = "desktop" if a.cmd == "desktop" else a.cmd
        ctl.detail = getattr(a, "file", None) or a.__dict__.get("window") or a.__dict__.get("monitor") \
            or a.__dict__.get("region") or a.__dict__.get("virtual") or ""
        if a.cmd == "theme":
            ctl.detail = os.path.abspath(a.file)
        with ControlServer(ctl, path=borrow.sock if borrow.active else None):
            if borrow.active:
                print("Display borrowed from the turzx service; it continues its theme when this ends.")
            if a.cmd == "theme":
                app.run_theme(dev, a.file, rotation=a.rotate, interval=a.interval,
                              video_dirs=_with_settings(a.video_dir, "video_dirs", DEFAULT_VIDEO_DIRS),
                              font_dirs=_with_settings(a.font_dir, "font_dirs", DEFAULT_FONT_DIRS), no_video=a.no_video, stop=stop)
            elif a.cmd == "video":
                rot = a.rotate
                if rot is None:
                    rot = 90 if _is_landscape(a.file) else 0
                app.play_video(dev, a.file, rot, a.fps, a.fit, loop=not a.once, stop=stop)
            elif a.cmd == "desktop" and a.window:
                app.run_window(dev, a.window, rotation=a.rotate, fps=a.fps, cursor=not a.no_cursor, stop=stop)
            elif a.cmd == "desktop":
                app.run_desktop(dev, rotation=a.rotate, fps=a.fps, region=a.region, monitor=a.monitor,
                                virtual=a.virtual, cursor=not a.no_cursor, stop=stop)
        return 0
    except TurzxError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0
    finally:
        dev.close()
        borrow.__exit__(None, None, None)


def _hold_hint(borrow) -> None:
    if borrow.active:
        print("The turzx service pauses until 'turzx play' (continues its theme).")


def _control(a) -> int:
    from .control import run_command

    kw = {"value": a.percent} if a.cmd == "brightness" else {}
    if a.cmd == "play":
        from .control import request, save_setting

        path = os.path.abspath(a.file) if a.file else None
        if path and not os.path.isfile(path):
            print(f"Error: no such file: {path}", file=sys.stderr)
            return 2
        r = request("play", path=path)
        if r is None:  # no service running: just remember it for the next start
            if not path:
                print("Error: the turzx service is not running ('turzx service install')", file=sys.stderr)
                return 2
            save_setting("theme", path)
            print("No service running - theme saved for the service's next start. "
                  f"To play it now without the service: turzx theme \"{path}\"")
            return 0
    else:
        r = run_command(a.cmd, serial=a.serial, **kw)
    if not r.get("ok"):
        print(f"Error: {r.get('error')}", file=sys.stderr)
        return 2
    if a.cmd == "status":
        mode = r["mode"] if r["mode"] not in ("", "idle") else "nothing"
        print(f"Running:    {mode} {r.get('detail') or ''}".rstrip() + ("  (service)" if r.get("daemon") else ""))
        print(f"Brightness: {r['brightness']}%" + (" (backlight off)" if r.get("off") else ""))
        print(f"Display:    {r.get('model')} fw {r.get('firmware')} serial {r.get('serial')}")
    return 0


def _service(a) -> int:
    from . import service
    from .control import load_settings, save_setting

    if a.action == "install":
        if a.theme:
            save_setting("theme", os.path.abspath(a.theme))
        if a.font_dir:
            save_setting("font_dirs", [os.path.abspath(d) for d in a.font_dir])
        if a.video_dir:
            save_setting("video_dirs", [os.path.abspath(d) for d in a.video_dir])
        rc = service.install()
        st = load_settings()
        print(f"Theme:      {st.get('theme') or '(none - choose one with turzx play FILE or in the editor)'}")
        return rc
    if a.action == "uninstall":
        return service.uninstall()
    if a.action == "restart":
        service._systemctl("restart", service.UNIT_NAME)
    return service.status()


def _is_landscape(path: str) -> bool:
    import subprocess

    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        w, h = (int(x) for x in out.split(",")[:2])
        return w > h
    except (ValueError, OSError, subprocess.SubprocessError):
        return True


def _preview(a) -> int:
    import time

    from PIL import Image

    from .fonts import FontIndex
    from .render import Renderer, to_device
    from .sensors import Sensors
    from .theme import load_theme

    t = load_theme(a.file)
    s = Sensors()
    time.sleep(1.0)
    s.update()
    img = Renderer(t, s, FontIndex(_with_settings(a.font_dir, "font_dirs", DEFAULT_FONT_DIRS))).render()
    s.close()
    if a.device:
        img = to_device(img, 480, 1920)
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    bg.alpha_composite(img)
    bg.convert("RGB").save(a.output)
    print(f"{a.output}: {t.name} {t.width}x{t.height}, video: {t.video_name or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
