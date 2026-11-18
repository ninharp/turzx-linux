"""Background service: keeps the display running with the selected theme.

Runs as a systemd user service (`turzx service install`). The theme, font and
video directories come from ~/.config/turzx/settings.json; the editor and the
control window change them over the control socket ("play"). The editor's
"Live on display" borrows the display ("release" / "resume").
Survives unplugging, suspend and display reboots by reconnecting.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from PIL import Image

from .control import ControlServer, Controller, load_settings, request, save_setting
from .device import TurzxDevice, TurzxError

log = logging.getLogger(__name__)

FORWARD = ("brightness", "off", "on", "reboot")  # sent to the borrowing process


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Daemon(Controller):
    def __init__(self, serial: str | None = None, font_dirs: list[str] | None = None,
                 video_dirs: list[str] | None = None):
        super().__init__(None, mode="daemon")  # type: ignore[arg-type]
        self.serial = serial
        self.base_font_dirs = font_dirs or []
        self.base_video_dirs = video_dirs or []
        self.shutdown = threading.Event()
        self.wake = threading.Event()
        self.run_flag = None
        self.paused = False
        self.state = "starting"
        self.borrower: tuple[int, str] | None = None  # (pid, socket) of the process using the display

    # -- control commands ----------------------------------------------------------
    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            info = self.dev.info if self.dev else None
            r = {"ok": True, "daemon": True, "mode": self.state, "detail": self.detail,
                 "brightness": self.brightness, "off": self.off, "theme": load_settings().get("theme"),
                 "firmware": info.firmware if info else "", "serial": info.serial if info else "",
                 "model": info.model if info else "", "pid": os.getpid()}
            if self.borrower:
                b = request("status", timeout=1, sock_path=self.borrower[1])
                if b and b.get("ok"):
                    r.update(firmware=b.get("firmware"), serial=b.get("serial"), model=b.get("model"))
            return r
        if cmd == "play":
            if self.state == "theme error":
                self.state = "starting"
            path = load_settings().get("theme")
            if req.get("path"):
                path = os.path.abspath(os.path.expanduser(str(req["path"])))
                if not os.path.isfile(path):
                    return {"ok": False, "error": f"no such theme file: {path}"}
                save_setting("theme", path)
            if self.borrower and not self.borrower[0]:  # ends a one-shot borrow ('turzx image')
                self.borrower = None
            self.paused = False
            self._restart()
            return {"ok": True, "theme": path}
        if cmd == "stop":
            self.paused = True
            self._restart()
            return {"ok": True}
        if cmd == "release":
            self.borrower = (int(req.get("pid") or 0), str(req.get("socket") or ""))
            self._restart()
            for _ in range(150):  # answer once the device is closed
                if self.dev is None:
                    return {"ok": True}
                time.sleep(0.1)
            return {"ok": False, "error": "display not released in time"}
        if cmd == "resume":
            self.borrower = None
            self.wake.set()
            return {"ok": True}
        if cmd in FORWARD and self.borrower and not self.borrower[0]:
            # one-shot borrow: nobody holds the display, open it for this command
            try:
                dev = TurzxDevice(serial=self.serial)
            except TurzxError as e:
                return {"ok": False, "error": str(e)}
            try:
                tmp = Controller(dev)
                tmp.brightness, tmp.off = self.brightness, self.off
                r = tmp.handle(req) if cmd != "reboot" else (dev.reboot() or {"ok": True})
                self.brightness, self.off = tmp.brightness, tmp.off
                return r
            finally:
                dev.close()
        if cmd in FORWARD and self.borrower:
            if cmd == "brightness":
                self.brightness, self.off = max(0, min(100, int(req["value"]))), False
                save_setting("brightness", self.brightness)
            elif cmd in ("off", "on"):
                self.off = cmd == "off"
            r = request(cmd, sock_path=self.borrower[1], **{k: v for k, v in req.items() if k != "cmd"})
            return r or {"ok": False, "error": "display is borrowed by a process that does not answer"}
        if self.dev is None:
            if cmd == "brightness":
                self.brightness, self.off = max(0, min(100, int(req["value"]))), False
                save_setting("brightness", self.brightness)
                return {"ok": True, "brightness": self.brightness}
            if cmd in ("off", "on"):
                self.off = cmd == "off"
                return {"ok": True, "off": self.off}
            return {"ok": False, "error": f"display not connected ({self.state})"}
        return super().handle(req)

    def _restart(self) -> None:
        rf = self.run_flag
        if rf is not None:
            rf.ev.set()
        self.wake.set()

    def _sleep(self, t: float) -> None:
        self.wake.wait(t)
        self.wake.clear()

    # -- main loop -----------------------------------------------------------------
    def run(self) -> int:
        def _term(*_):
            self.shutdown.set()
            self._restart()

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        with ControlServer(self):
            backoff = 2.0
            while not self.shutdown.is_set():
                if self.borrower:
                    pid = self.borrower[0]
                    if pid and not _pid_alive(pid):
                        log.info("borrowing process %d is gone - taking the display back", pid)
                        self.borrower = None
                        continue
                    self.state = "lent"
                    self.detail = "until 'turzx play'" if not pid else f"to process {pid}"
                    self._sleep(5)
                    continue
                try:
                    dev = TurzxDevice(serial=self.serial)
                except Exception as e:  # noqa: BLE001 - not connected, busy, no permission
                    self.state = "waiting for display"
                    self.detail = str(e)
                    log.debug("display not available: %s", e)
                    self._sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 2.0
                self.dev = dev
                try:
                    self.apply_brightness()
                    self._play_loop(dev)
                except TurzxError as e:
                    # never reset the device; close it and try again later
                    log.warning("display error: %s - reconnecting", e)
                    self.state, self.detail = "display error", str(e)
                    self._sleep(10)
                finally:
                    self.dev = None
                    dev.close()
        log.info("stopped")
        return 0

    def _play_loop(self, dev: TurzxDevice) -> None:
        from . import app

        while not self.shutdown.is_set() and not self.borrower:
            self.run_flag = app.StopFlag(signals=False)
            self.wake.clear()
            st = load_settings()
            theme = st.get("theme")
            if self.paused or not theme or not os.path.isfile(theme):
                if self.state != "theme error":  # keep the error visible in the status
                    self.state = "stopped" if self.paused else "no theme selected"
                    self.detail = "" if self.paused or not theme else f"missing: {theme}"
                dev.stop_playback()
                dev.clear_overlay()
                dev.show_image(Image.new("RGB", (dev.width, dev.height)))
                self.run_flag.wait(1e9)
                continue
            self.state, self.detail = "theme", theme
            log.info("playing %s", theme)
            try:
                app.run_theme(dev, theme, font_dirs=list(st.get("font_dirs", [])) + self.base_font_dirs,
                              video_dirs=list(st.get("video_dirs", [])) + self.base_video_dirs,
                              stop=self.run_flag)
            except TurzxError:
                raise
            except Exception as e:  # noqa: BLE001 - broken theme file etc.
                log.exception("theme failed")
                self.paused = True
                self.state, self.detail = "theme error", f"{os.path.basename(theme)}: {e}"
                continue
            if not self.run_flag.set and not self.shutdown.is_set():
                time.sleep(2)  # the theme ended by itself (e.g. video error): start again
