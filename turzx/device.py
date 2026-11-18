"""USB protocol for TURZX "207 LCD" displays (8.8" = VID 0x1CBE / PID 0x0088).

Reverse engineered from TURZX V3.1.0 (UsbMonitorL.exe, class ``꾆``):

* Every command starts with a 512-byte block. The plain header is 500 bytes:
  ``[0]=cmd, [2]=0x1A, [3]=0x6D, [4..7]=LE uint32 ms timestamp, [8..]=payload``.
  It is DES-CBC encrypted (key = IV = ``b"slv3tuzx"``, PKCS7 -> 504 bytes),
  padded to 512 bytes, and bytes 510/511 are set to ``0xA1 0x1A``.
* Bulk data (images, files) follows the 512-byte block in the same transfer.
* After each write the device answers with one 512-byte packet on EP 0x81,
  whose first byte usually echoes the command.

Verified on hardware:
  10  sync / handshake (reply contains firmware string, e.g. ``turzx_0001_0008``)
  101 full-screen JPEG background (payload[0:4] = BE length)
  102 PNG overlay layer with alpha (payload[0:4] = BE length)
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
import struct
import threading
import time
from dataclasses import dataclass

import usb.core
import usb.util
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

log = logging.getLogger(__name__)

VID = 0x1CBE
KNOWN_PIDS = {
    0x0088: ("8.8 inch", 480, 1920),
    0x0080: ("8 inch", 800, 1280),
    0x0288: ("2.88 inch", 480, 1920),
}
_KEY = b"slv3tuzx"
EP_OUT = 0x01
EP_IN = 0x81


class Cmd:
    SYNC = 10
    REBOOT = 11
    SHUTDOWN = 12
    ROTATE = 13
    BRIGHTNESS = 14
    FRAMERATE = 15
    STORAGE_INFO = 17    # reply[8..11] = BE size (seen in app, unverified)
    MODE = 51            # payload: Y_hi Y_lo M D h m s mode(0=stop,1,2=video stream)
    JPEG = 101
    PNG_OVERLAY = 102
    STOP_VIDEO = 111
    STOP_MEDIA = 123
    DESKTOP_MODE = 150   # switch firmware into USB-display (IDD) mode, then reboot


@dataclass
class DeviceInfo:
    serial: str
    firmware: str
    model: str
    width: int
    height: int
    pid: int


class TurzxError(RuntimeError):
    pass


def _timestamp_ms() -> int:
    # Original: (DateTime.UtcNow - DateTime.Today.AddDays(-1)).TotalMilliseconds
    base = _dt.datetime.combine(_dt.date.today() - _dt.timedelta(days=1), _dt.time())
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    return int((now - base).total_seconds() * 1000) & 0xFFFFFFFF


def build_header(cmd: int, payload: bytes = b"") -> bytes:
    plain = bytearray(500)
    plain[0] = cmd & 0xFF
    plain[2] = 0x1A
    plain[3] = 0x6D
    plain[4:8] = struct.pack("<I", _timestamp_ms())
    plain[8 : 8 + min(len(payload), 492)] = payload[:492]
    enc = DES.new(_KEY, DES.MODE_CBC, iv=_KEY).encrypt(pad(bytes(plain), 8))
    out = bytearray(512)
    out[: len(enc)] = enc
    out[510] = 0xA1
    out[511] = 0x1A
    return bytes(out)


def find_devices() -> list[usb.core.Device]:
    return [d for d in usb.core.find(find_all=True, idVendor=VID) if d.idProduct in KNOWN_PIDS]


class TurzxDevice:
    """One connected display. Thread-safe; all transfers are serialized."""

    def __init__(self, serial: str | None = None, timeout_ms: int = 2000):
        self.timeout = timeout_ms
        self._lock = threading.RLock()
        self.dev: usb.core.Device | None = None
        self.info: DeviceInfo | None = None
        self._serial_wanted = serial
        self.failed = False
        self.open()

    # -- connection ---------------------------------------------------
    def open(self) -> None:
        devs = find_devices()
        if self._serial_wanted:
            devs = [d for d in devs if _serial(d) == self._serial_wanted]
        if not devs:
            raise TurzxError(
                "No TURZX display found (VID 1cbe). Is it connected? Is the udev rule installed?"
            )
        dev = devs[0]
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            try:
                configured = dev.get_active_configuration() is not None
            except usb.core.USBError:
                configured = False
            if not configured:
                dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno == 13:
                raise TurzxError(
                    "No permission for the USB device. Install the udev rule "
                    "(see udev/99-turzx.rules) and replug the display."
                ) from e
            if e.errno != 16:  # busy = already configured
                raise
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as e:
            if e.errno == 16:
                raise TurzxError("The display is in use by another program "
                                 "(the turzx service? see 'turzx status')") from e
            raise
        self.dev = dev
        self._drain()
        reply = self.command(Cmd.SYNC)
        if not reply or reply[0] != Cmd.SYNC:
            self.close()
            raise TurzxError(
                "Display does not answer the handshake. Unplug and replug the USB "
                "cable (preferably on a powered hub). Do not reset the USB device!"
            )
        fw = reply[8:40].split(b"\0", 1)[0].decode("ascii", "replace")
        model, w, h = KNOWN_PIDS[dev.idProduct]
        self.info = DeviceInfo(_serial(dev), fw, model, w, h, dev.idProduct)
        log.info("connected %s fw=%s serial=%s", model, fw, self.info.serial)

    def close(self) -> None:
        with self._lock:
            if self.dev is not None:
                try:
                    usb.util.release_interface(self.dev, 0)
                    usb.util.dispose_resources(self.dev)
                except usb.core.USBError:
                    pass
                self.dev = None

    def reconnect(self, attempts: int = 20, delay: float = 1.0) -> None:
        self.close()
        last: Exception | None = None
        for _ in range(attempts):
            try:
                self.open()
                return
            except (TurzxError, usb.core.USBError) as e:
                last = e
                time.sleep(delay)
        raise TurzxError(f"reconnect failed: {last}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def width(self) -> int:
        return self.info.width if self.info else 480

    @property
    def height(self) -> int:
        return self.info.height if self.info else 1920

    # -- transport ----------------------------------------------------
    def _drain(self) -> None:
        """Discard stale replies (equivalent of LibUsbDotNet ReadFlush)."""
        assert self.dev is not None
        for _ in range(8):
            try:
                self.dev.read(EP_IN, 512, timeout=20)
            except usb.core.USBError:
                return

    def _xfer(self, data: bytes, want_reply: bool = True) -> bytes | None:
        """Write one command (+data) and read its reply.

        The firmware answers with a 512-byte packet followed by a zero-length
        packet. Reading with a 1024-byte buffer lets libusb finish the
        transfer at that ZLP, so no stale ZLP is left for the next command.
        A timeout raises TurzxError: the caller must stop sending (never
        reset the device - that can block the whole xHCI controller).
        """
        with self._lock:
            if self.dev is None:
                raise TurzxError("device not open")
            try:
                self.dev.write(EP_OUT, data, timeout=max(self.timeout, 1000 + len(data) // 20000))
                if not want_reply:
                    return None
                r = bytes(self.dev.read(EP_IN, 1024, timeout=self.timeout))
                if not r:  # stray ZLP from an earlier reply
                    r = bytes(self.dev.read(EP_IN, 1024, timeout=self.timeout))
                return r
            except usb.core.USBTimeoutError as e:
                self.failed = True
                raise TurzxError("display not responding (timeout)") from e
            except usb.core.USBError as e:
                self.failed = True
                raise TurzxError(f"USB error: {e}") from e

    def command(self, cmd: int, payload: bytes = b"", want_reply: bool = True) -> bytes | None:
        return self._xfer(build_header(cmd, payload), want_reply)

    def send_data(self, cmd: int, data: bytes, want_reply: bool = True) -> bytes | None:
        hdr = build_header(cmd, struct.pack(">I", len(data)))
        return self._xfer(hdr + data, want_reply)

    # -- high level ---------------------------------------------------
    def show_jpeg(self, jpeg: bytes) -> None:
        if len(jpeg) > 1_048_576:
            raise TurzxError("JPEG > 1 MiB is rejected by the device")
        self.send_data(Cmd.JPEG, jpeg)

    def show_overlay_png(self, png: bytes) -> None:
        if len(png) > 1_048_576:
            raise TurzxError("PNG > 1 MiB is rejected by the device")
        self.send_data(Cmd.PNG_OVERLAY, png)

    def show_image(self, img, quality: int = 90) -> None:
        """Send a PIL image (already in device orientation) as background."""
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality)
        self.show_jpeg(buf.getvalue())

    def set_overlay(self, img) -> None:
        """Send a PIL RGBA image as transparent overlay layer."""
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, "PNG", compress_level=1)
        self.show_overlay_png(buf.getvalue())

    def clear_overlay(self) -> None:
        from PIL import Image

        self.set_overlay(Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0)))

    def stop_playback(self) -> None:
        """Stop anything the firmware plays on its own (video / stored theme)."""
        self.command(Cmd.STOP_VIDEO)
        self.set_mode(0)
        self.command(Cmd.STOP_MEDIA)
        self._drain()

    def set_mode(self, mode: int) -> bytes | None:
        now = _dt.datetime.now()
        payload = bytes(
            [now.year >> 8, now.year & 0xFF, now.month, now.day, now.hour, now.minute, now.second, mode]
        )
        return self.command(Cmd.MODE, payload)

    def set_brightness(self, percent: int) -> None:
        """0..100 (the Windows app maps its 0..255 slider to 0..100)."""
        self.command(Cmd.BRIGHTNESS, bytes([max(0, min(100, int(percent)))]))

    def set_rotation(self, r: int) -> None:
        """Firmware-side rotation index (0..3) as sent by the Windows app."""
        self.command(Cmd.ROTATE, bytes([r & 3]))

    def reboot(self) -> None:
        self.command(Cmd.REBOOT, want_reply=False)

    def shutdown(self) -> None:
        self.command(Cmd.SHUTDOWN, want_reply=False)

    def enter_native_desktop_mode(self) -> None:
        """Firmware USB-display mode (re-enumerates as 1A86:AD11, needs the
        Windows IddCx driver). Not usable on Linux - kept for completeness."""
        self.command(Cmd.DESKTOP_MODE)
        self.reboot()


def _serial(dev: usb.core.Device) -> str:
    try:
        return dev.serial_number or ""
    except (usb.core.USBError, ValueError):
        return ""
