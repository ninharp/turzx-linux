"""High level modes: theme player, image, video and desktop streaming."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time

from PIL import Image

from .device import TurzxDevice, TurzxError
from .fonts import FontIndex
from .render import Renderer, to_device
from .sensors import Sensors
from .stream import FFmpegStreamer
from .theme import load_theme

log = logging.getLogger(__name__)


class StopFlag:
    """Stop request for a running mode; by default also set by Ctrl+C / SIGTERM."""

    def __init__(self, signals: bool = True):
        self.ev = threading.Event()
        if signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.ev.set())

    def wait(self, t: float) -> bool:
        return self.ev.wait(t)

    @property
    def set(self) -> bool:
        return self.ev.is_set()


def run_theme(dev: TurzxDevice, path: str, *, rotation: int | None = None, interval: float = 1.0,
              video_dirs: list[str] | None = None, font_dirs: list[str] | None = None,
              no_video: bool = False, quality: int = 90, stop: StopFlag | None = None) -> None:
    stop = stop or StopFlag()
    theme = load_theme(path)
    sensors = Sensors()
    fonts = FontIndex(font_dirs)
    video = None if no_video else theme.find_video(video_dirs or [])
    if theme.video_name and not video and not no_video:
        log.warning("video %s not found - showing static background", theme.video_name)
    if video and theme.items and theme.items[0].type == "Image":
        # app converts GraphList[0] into the video placeholder (Theme.녃)
        theme.items[0].type = "Animation"
    renderer = Renderer(theme, sensors, fonts)
    renderer.video_background = bool(video)
    rot = rotation if rotation is not None else (90 if theme.landscape else 0)
    log.info("theme %s (%dx%d) rotation=%d video=%s", theme.name, theme.width, theme.height, rot, video)

    dev.stop_playback()
    # always: a previous video theme leaves its value overlay on the display,
    # which would otherwise stay on top of a static theme's background
    dev.clear_overlay()
    streamer: FFmpegStreamer | None = None
    if video:
        # theme videos are stored in device orientation already (480x1920)
        vrot = 0
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=width,height", "-of", "csv=p=0", video],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            vw, vh = (int(x) for x in out.split(",")[:2])
            if (vw > vh) != (dev.width > dev.height):
                vrot = rot or 90
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        streamer = FFmpegStreamer(dev, vrot)
        fps = theme.frame_rate or 24
        args = FFmpegStreamer.input_args(video, fps)
        th = threading.Thread(target=_guard, args=(streamer.run, stop, args, min(fps, 30)), daemon=True)
        th.start()

    try:
        sensors.update()
        while not stop.set:
            t0 = time.monotonic()
            sensors.update()
            frame = renderer.render()
            img = to_device(frame, dev.width, dev.height, rot)
            if video:
                dev.set_overlay(img)
            else:
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.getchannel("A"))
                dev.show_image(bg, quality=quality)
            if dev.failed:
                raise TurzxError("display stopped responding")
            stop.wait(max(0.05, interval - (time.monotonic() - t0)))
    finally:
        if streamer:
            streamer.stop()
        sensors.close()


def _guard(fn, stop: StopFlag, *args):
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        log.error("video stream ended: %s", e)
        stop.ev.set()


def show_image(dev: TurzxDevice, path: str, rotation: int | None = None, fit: str = "cover") -> None:
    img = Image.open(path).convert("RGB")
    rot = rotation if rotation is not None else (90 if img.width > img.height else 0)
    if rot in (90, 270):
        tw, th = dev.height, dev.width
    else:
        tw, th = dev.width, dev.height
    img = _fit(img, tw, th, fit)
    dev.stop_playback()
    dev.clear_overlay()
    dev.show_image(to_device(img, dev.width, dev.height, rot))


def _fit(img: Image.Image, w: int, h: int, mode: str) -> Image.Image:
    if mode == "stretch":
        return img.resize((w, h), Image.LANCZOS)
    s = (max if mode == "cover" else min)(w / img.width, h / img.height)
    r = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(r, ((w - r.width) // 2, (h - r.height) // 2))
    return out


def play_video(dev: TurzxDevice, path: str, rotation: int = 90, fps: float = 30, fit: str = "cover",
               loop: bool = True, stop: StopFlag | None = None) -> None:
    stop = stop or StopFlag()
    dev.stop_playback()
    dev.clear_overlay()
    s = FFmpegStreamer(dev, rotation)
    threading.Thread(target=lambda: (stop.wait(1e9), s.stop()), daemon=True).start()
    args = FFmpegStreamer.input_args(path, fps, loop=loop)
    s.run(args, min(fps, 30), fit)


# --------------------------------------------------------------------- desktop
def _xrandr_monitors() -> list[dict]:
    out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True).stdout
    mons = []
    for line in out.splitlines()[1:]:
        # " 0: +*DP-0 3440/800x1440/330+1920+0  DP-0"
        parts = line.split()
        if len(parts) < 3:
            continue
        geo = parts[2]
        try:
            w = int(geo.split("/")[0])
            h = int(geo.split("x")[1].split("/")[0])
            x, y = (int(v) for v in geo.split("+")[1:3])
        except (ValueError, IndexError):
            continue
        mons.append({"name": parts[1].lstrip("+*"), "w": w, "h": h, "x": x, "y": y})
    return mons


def _screen_size() -> tuple[int, int]:
    out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True).stdout
    first = out.splitlines()[0]  # "Screen 0: minimum 8 x 8, current 7280 x 1440, maximum ..."
    cur = first.split("current")[1].split(",")[0]
    w, h = (int(v) for v in cur.split("x"))
    return w, h


def _xrandr_outputs() -> list[dict]:
    """Active outputs with mode, rate, position, primary flag."""
    import re

    out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True).stdout
    outs: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        m_ = re.match(r"^(\S+) connected( primary)? (\d+)x(\d+)\+(\d+)\+(\d+)", line)
        if m_:
            cur = {"name": m_.group(1), "primary": bool(m_.group(2)), "w": int(m_.group(3)),
                   "h": int(m_.group(4)), "x": int(m_.group(5)), "y": int(m_.group(6)), "rate": None}
            outs.append(cur)
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is not None and "*" in line and cur["rate"] is None:
            parts = line.split()
            for p_ in parts[1:]:
                if "*" in p_:
                    cur["rate"] = p_.replace("*", "").replace("+", "")
                    cur["mode"] = parts[0]
    return outs


def find_free_area(outputs: list[dict], screen: tuple[int, int], aspect: float = 4.0,
                   prefer: str | None = None) -> tuple[int, int, int, int] | None:
    """Largest rectangle with width/height == aspect inside the current screen
    that no output scans out (e.g. the strip above a lower side monitor).
    Using such an area needs no framebuffer change, so the NVIDIA driver does
    not start panning. Ties prefer areas touching the `prefer`/primary output."""
    sw, sh = screen
    rects = [(o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]) for o in outputs]
    xs = sorted({0, sw, *[r[0] for r in rects], *[r[2] for r in rects]})
    ys = sorted({0, sh, *[r[1] for r in rects], *[r[3] for r in rects]})
    ref = next((o for o in outputs if o["name"] == prefer), None) or \
        next((o for o in outputs if o["primary"]), None)

    def free(x0, y0, x1, y1):
        return x1 <= sw and y1 <= sh and all(x1 <= a or x0 >= c or y1 <= b or y0 >= d for a, b, c, d in rects)

    best = None
    for x0 in xs:
        for y0 in ys:
            for x_end in xs:
                if x_end <= x0:
                    continue
                for y_end in ys:
                    if y_end <= y0 or not free(x0, y0, x_end, y_end):
                        continue
                    w, h = x_end - x0, y_end - y0
                    h2 = min(h, int(w / aspect))
                    w2 = int(h2 * aspect) // 8 * 8
                    h2 = int(w2 / aspect)
                    if w2 <= 0:
                        continue
                    # align to the edge that touches the reference output
                    x = x0 if not ref or abs(x0 - (ref["x"] + ref["w"])) < abs(x_end - ref["x"]) else x_end - w2
                    y = y_end - h2  # sit directly on top of the monitor below
                    touch = bool(ref) and (x + w2 == ref["x"] or x == ref["x"] + ref["w"] or
                                           y + h2 == ref["y"] or y == ref["y"] + ref["h"])
                    key = (w2, touch)
                    if best is None or key > best[0]:
                        best = (key, (x, y, w2, h2))
    return best[1] if best else None


class VirtualMonitor:
    """Extend the X screen and declare a RandR monitor that no GPU output scans
    out. Window managers treat it as a real monitor; we capture it with x11grab.

    position: "below[:OUTPUT]" (centered under OUTPUT, default primary),
              "right" (right of everything) or "X,Y" absolute.
    The original layout is saved and restored exactly on exit (NVIDIA: never
    use --panning 0x0, it collapses all outputs to +0+0)."""

    NAME = "TURZX"

    def __init__(self, w: int, h: int, position: str = "below"):
        self.w, self.h = w, h
        self.windows = None
        self.orig_fb = _screen_size()
        self.orig_outputs = _xrandr_outputs()
        sw, sh = self.orig_fb
        if position.startswith("free"):
            area = find_free_area(self.orig_outputs, self.orig_fb, w / h, position.partition(":")[2] or None)
            if not area:
                raise TurzxError("no free area found in the current monitor layout")
            self.x, self.y, self.w, self.h = area
        elif position.startswith("below"):
            name = position.partition(":")[2]
            ref = next((o for o in self.orig_outputs if o["name"] == name), None) or \
                next((o for o in self.orig_outputs if o["primary"]), None) or self.orig_outputs[0]
            self.x = max(0, ref["x"] + (ref["w"] - w) // 2)
            self.y = ref["y"] + ref["h"]
        elif position == "right":
            self.x, self.y = sw, 0
        else:
            self.x, self.y = (int(v) for v in position.split(","))
        self.fb = (max(sw, self.x + self.w), max(sh, self.y + self.h))
        self.resize = self.fb != self.orig_fb

    def _restore_cmd(self) -> list[str]:
        cmd = ["xrandr"]
        for o in self.orig_outputs:
            cmd += ["--output", o["name"], "--mode", o.get("mode", f"{o['w']}x{o['h']}"),
                    "--pos", f"{o['x']}x{o['y']}"]
            if o["rate"]:
                cmd += ["--rate", o["rate"]]
            if o["primary"]:
                cmd += ["--primary"]
        return cmd

    def __enter__(self):
        from .x11windows import WindowSnapshot

        self.windows = WindowSnapshot()
        # enlarge the screen and, in the SAME call (xrandr recomputes the screen
        # size on output changes), lock panning of every real output to its own
        # area - NVIDIA otherwise pans the CRTCs with the mouse
        lock = ["xrandr", "--fb", f"{self.fb[0]}x{self.fb[1]}"]
        for o in self.orig_outputs:
            g = f"{o['w']}x{o['h']}+{o['x']}+{o['y']}"
            lock += ["--output", o["name"], "--panning", f"{g}/{g}/0/0/0/0"]
        try:
            if self.resize:
                log.warning("enlarging the X screen - with NVIDIA the whole layout will pan with the mouse")
                subprocess.run(lock, check=True)
                if _screen_size() != self.fb:
                    raise TurzxError(f"could not enlarge the X screen to {self.fb}")
            mm_w, mm_h = round(self.w * 0.11), round(self.h * 0.11)  # ~0.11 mm/px (8.8" panel)
            subprocess.run(["xrandr", "--setmonitor", self.NAME,
                            f"{self.w}/{mm_w}x{self.h}/{mm_h}+{self.x}+{self.y}", "none"], check=True)
        except Exception:
            self.__exit__(None, None, None)
            raise
        log.info("virtual monitor %s %dx%d+%d+%d created", self.NAME, self.w, self.h, self.x, self.y)
        return self

    def __exit__(self, *exc):
        subprocess.run(["xrandr", "--delmonitor", self.NAME], check=False)
        if not self.resize:
            log.info("virtual monitor removed")
            time.sleep(0.3)
            if self.windows:
                # windows left in the (now invisible) area go back where they were,
                # windows opened there go to the primary monitor
                prim = next((o for o in self.orig_outputs if o["primary"]), self.orig_outputs[0])
                self.windows.restore(rescue=(self.x, self.y, self.w, self.h),
                                     target=(prim["x"] + 50, prim["y"] + 50))
            return
        subprocess.run(["xrandr", "--fb", f"{self.orig_fb[0]}x{self.orig_fb[1]}"], check=False)
        subprocess.run(self._restore_cmd(), check=False)
        now = [(o["name"], o["x"], o["y"], o["w"], o["h"]) for o in _xrandr_outputs()]
        want = [(o["name"], o["x"], o["y"], o["w"], o["h"]) for o in self.orig_outputs]
        if sorted(now) != sorted(want):
            log.error("monitor layout differs! Restore with: %s", " ".join(self._restore_cmd()))
        else:
            log.info("virtual monitor removed, layout restored")
        time.sleep(0.5)  # let the window manager settle before moving windows back
        if self.windows:
            self.windows.restore()


def run_window(dev: TurzxDevice, window: str, *, rotation: int = 90, fps: float = 20,
               fit: str = "fit", cursor: bool = False, stop: StopFlag | None = None) -> None:
    """Mirror a single window (by title substring, hex/decimal id, or 'pick')."""
    from .x11windows import list_windows, pick_window

    stop = stop or StopFlag()
    wins = list_windows()
    wid = None
    if window == "pick":
        print("Click the window that should appear on the display ...")
        wid = pick_window()
    elif window.lower().startswith("0x") or window.isdigit():
        wid = int(window, 0)
    else:
        hits = [w for w in wins if window.lower() in w[1].lower()]
        if not hits:
            raise TurzxError(f"no window with '{window}' in its title. Available: " +
                             ", ".join(t for _, t, _ in wins))
        wid = hits[0][0]
    if wid is None:
        raise TurzxError("no window selected")
    title = next((t for w, t, _ in wins if w == wid), hex(wid))
    log.info("mirroring window %s (%s)", title, hex(wid))
    display = os.environ.get("DISPLAY", ":0")
    dev.stop_playback()
    dev.clear_overlay()
    s = FFmpegStreamer(dev, rotation, quality=5)
    threading.Thread(target=lambda: (stop.wait(1e9), s.stop()), daemon=True).start()
    # x11grab captures the window's own pixmap; with a compositor (xfwm4)
    # this also works while the window is covered by others
    args = ["-f", "x11grab", "-window_id", str(wid), "-draw_mouse", "1" if cursor else "0",
            "-framerate", str(fps), "-i", display]
    s.run(args, fps, fit)


def run_desktop(dev: TurzxDevice, *, rotation: int = 90, fps: float = 20, region: str | None = None,
                monitor: str | None = None, virtual: str | None = None, cursor: bool = True,
                stop: StopFlag | None = None) -> None:
    stop = stop or StopFlag()
    display = os.environ.get("DISPLAY", ":0")
    cw, ch = (dev.height, dev.width) if rotation in (90, 270) else (dev.width, dev.height)
    vm = None
    if virtual:
        vm = VirtualMonitor(cw, ch, virtual)
        x, y, w, h = vm.x, vm.y, vm.w, vm.h
    elif monitor:
        m = next((m for m in _xrandr_monitors() if m["name"] == monitor), None)
        if not m:
            raise TurzxError(f"monitor {monitor} not found: {[m['name'] for m in _xrandr_monitors()]}")
        x, y, w, h = m["x"], m["y"], m["w"], m["h"]
    elif region:
        x, y, w, h = (int(v) for v in region.replace("x", ",").split(","))
    else:
        x, y, w, h = 0, 0, cw, ch
    dev.stop_playback()
    dev.clear_overlay()
    s = FFmpegStreamer(dev, rotation, quality=5)
    threading.Thread(target=lambda: (stop.wait(1e9), s.stop()), daemon=True).start()
    args = ["-f", "x11grab", "-draw_mouse", "1" if cursor else "0", "-framerate", str(fps),
            "-video_size", f"{w}x{h}", "-i", f"{display}+{x},{y}"]
    if vm:
        with vm:
            s.run(args, fps, "stretch")
    else:
        s.run(args, fps, "fit")
