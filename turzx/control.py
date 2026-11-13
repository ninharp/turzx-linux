"""Display control while a turzx process owns the display.

Only one process can claim the USB interface. The process that plays a theme,
video or desktop (or the editor's live view) therefore runs a small control
server on a Unix socket; `turzx brightness/reboot/...` and the control window
send their commands there and fall back to opening the display directly when
nothing is running.

Protocol: one JSON object per line, e.g. {"cmd": "brightness", "value": 60},
answered by one JSON object per line ({"ok": true, ...} or {"ok": false, "error": ...}).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Callable

from .device import TurzxDevice, TurzxError

log = logging.getLogger(__name__)

DEFAULT_BRIGHTNESS = 80
SETTINGS_PATH = os.path.expanduser("~/.config/turzx/settings.json")


def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache/turzx")
    return os.path.join(base, "turzx.sock")


# --------------------------------------------------------------------- settings
def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value) -> None:
    d = load_settings()
    d[key] = value
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def saved_brightness() -> int:
    try:
        return max(0, min(100, int(load_settings().get("brightness", DEFAULT_BRIGHTNESS))))
    except (TypeError, ValueError):
        return DEFAULT_BRIGHTNESS


# --------------------------------------------------------------------- device ops
class Controller:
    """Commands on an open device; shared by the server and the direct fallback."""

    def __init__(self, dev: TurzxDevice, mode: str = "", detail: str = "",
                 on_stop: Callable[[], None] | None = None,
                 on_reconnect: Callable[[], None] | None = None):
        self.dev = dev
        self.mode = mode
        self.detail = detail
        self.on_stop = on_stop
        self.on_reconnect = on_reconnect
        self.brightness = saved_brightness()
        self.off = False

    def apply_brightness(self) -> None:
        self.dev.set_brightness(0 if self.off else self.brightness)

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        dev = self.dev
        if cmd == "status":
            info = dev.info
            return {"ok": True, "mode": self.mode, "detail": self.detail, "brightness": self.brightness,
                    "off": self.off, "firmware": info.firmware if info else "",
                    "serial": info.serial if info else "", "model": info.model if info else "",
                    "pid": os.getpid()}
        if cmd == "brightness":
            v = max(0, min(100, int(req["value"])))
            self.brightness, self.off = v, False
            dev.set_brightness(v)
            save_setting("brightness", v)
            return {"ok": True, "brightness": v}
        if cmd in ("off", "on"):
            self.off = cmd == "off"
            self.apply_brightness()
            return {"ok": True, "off": self.off}
        if cmd == "reboot":
            self.reboot()
            return {"ok": True}
        if cmd == "stop":
            if not self.on_stop:
                return {"ok": False, "error": "nothing to stop"}
            self.on_stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def reboot(self) -> None:
        """Reboot the display and reconnect. Holds the device lock meanwhile, so
        a running player simply blocks and continues with the next frame."""
        dev = self.dev
        with dev._lock:  # noqa: SLF001 - serialize against the player thread
            dev.reboot()
            time.sleep(3)
            dev.reconnect(attempts=30, delay=1.0)
            dev.failed = False
            dev.stop_playback()
            self.apply_brightness()
        if self.on_reconnect:
            self.on_reconnect()


class ControlServer:
    def __init__(self, controller: Controller):
        self.ctl = controller
        self.path = socket_path()
        self.sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "ControlServer":
        if ping():
            log.warning("another turzx process already answers on %s", self.path)
            return self
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            os.unlink(self.path)  # stale socket of a crashed process (ping failed)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        s.listen(4)
        self.sock = s
        self._thread = threading.Thread(target=self._serve, daemon=True, name="turzx-control")
        self._thread.start()
        log.debug("control socket %s", self.path)
        return self

    def _serve(self) -> None:
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rw", encoding="utf-8") as f:
            for line in f:
                try:
                    resp = self.ctl.handle(json.loads(line))
                except (TurzxError, ValueError, KeyError, TypeError) as e:
                    resp = {"ok": False, "error": str(e)}
                except Exception as e:  # noqa: BLE001 - never kill the player
                    log.exception("control command failed")
                    resp = {"ok": False, "error": str(e)}
                try:
                    f.write(json.dumps(resp) + "\n")
                    f.flush()
                except OSError:
                    return

    def close(self) -> None:
        s, self.sock = self.sock, None
        if s is None:
            return
        try:
            s.close()
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------- client
def request(cmd: str, timeout: float = 60.0, **kw) -> dict | None:
    """Send a command to the running turzx process; None if none is running."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(socket_path())
    except OSError:
        return None
    with s, s.makefile("rw", encoding="utf-8") as f:
        f.write(json.dumps({"cmd": cmd, **kw}) + "\n")
        f.flush()
        line = f.readline()
    return json.loads(line) if line else {"ok": False, "error": "no answer"}


def ping() -> bool:
    r = request("status", timeout=2.0)
    return bool(r and r.get("ok"))


def run_command(cmd: str, serial: str | None = None, **kw) -> dict:
    """Execute a control command: via the running turzx process if there is
    one, otherwise by opening the display for this single command."""
    r = request(cmd, **kw)
    if r is not None:
        return r
    if cmd == "stop":
        return {"ok": False, "error": "nothing is running"}
    try:
        dev = TurzxDevice(serial=serial)
    except TurzxError as e:
        return {"ok": False, "error": str(e)}
    try:
        ctl = Controller(dev, mode="idle")
        if cmd == "reboot":  # no need to wait for the display to come back
            dev.reboot()
            return {"ok": True}
        return ctl.handle({"cmd": cmd, **kw})
    except TurzxError as e:
        return {"ok": False, "error": str(e)}
    finally:
        dev.close()
