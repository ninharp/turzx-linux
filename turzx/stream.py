"""ffmpeg based frame sources (video files, X11 desktop) -> JPEG -> display.

ffmpeg does decoding, scaling, rotation and MJPEG encoding; we only split the
JPEG stream (EOI marker FFD9 cannot occur inside entropy-coded data because of
byte stuffing) and push each frame with command 101.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterator

from .device import TurzxDevice

log = logging.getLogger(__name__)

# rotation (degrees clockwise applied to the *content*) -> ffmpeg filter
_ROT = {0: None, 90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}


def rotation_filter(rotation: int) -> str | None:
    if rotation not in _ROT:
        raise ValueError("rotation must be 0, 90, 180 or 270")
    return _ROT[rotation]


def content_size(dev_w: int, dev_h: int, rotation: int) -> tuple[int, int]:
    """Size of the logical (unrotated) canvas for a given rotation."""
    return (dev_h, dev_w) if rotation in (90, 270) else (dev_w, dev_h)


def iter_jpegs(stream) -> Iterator[bytes]:
    buf = bytearray()
    while True:
        chunk = stream.read1(1 << 16) if hasattr(stream, "read1") else stream.read(1 << 16)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                buf.clear()
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del buf[:start]
                break
            yield bytes(buf[start : end + 2])
            del buf[: end + 2]


class FFmpegStreamer:
    """Run ffmpeg with given input args and stream its MJPEG output."""

    def __init__(self, dev: TurzxDevice, rotation: int = 90, quality: int = 4,
                 on_frame: Callable[[int], None] | None = None):
        self.dev = dev
        self.rotation = rotation
        self.quality = quality  # ffmpeg -q:v (2 best .. 31 worst)
        self.on_frame = on_frame
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found")

    def build_filters(self, scale_mode: str = "cover") -> str:
        cw, ch = content_size(self.dev.width, self.dev.height, self.rotation)
        if scale_mode == "cover":
            f = [f"scale={cw}:{ch}:force_original_aspect_ratio=increase", f"crop={cw}:{ch}"]
        elif scale_mode == "fit":
            f = [f"scale={cw}:{ch}:force_original_aspect_ratio=decrease",
                 f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:black"]
        else:  # stretch
            f = [f"scale={cw}:{ch}"]
        rot = rotation_filter(self.rotation)
        if rot:
            f.append(rot)
        f.append("format=yuvj420p")
        return ",".join(f)

    @staticmethod
    def input_args(path: str, fps: float, loop: bool = True, realtime: bool = True) -> list[str]:
        """ffmpeg input options. Raw H.264 streams (the Windows app's '.h264'
        files) carry no timing, so they get the theme frame rate."""
        args = ["-re"] if realtime else []
        if path.lower().endswith((".h264", ".264")):
            args += ["-f", "h264", "-framerate", str(fps or 24)]
        if loop:
            args += ["-stream_loop", "-1"]
        return args + ["-i", path]

    def run(self, input_args: list[str], fps: float, scale_mode: str = "cover",
            extra_filters: str | None = None) -> None:
        vf = self.build_filters(scale_mode)
        if extra_filters:
            vf = extra_filters + "," + vf
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *input_args,
               "-vf", vf, "-r", str(fps), "-an", "-c:v", "mjpeg", "-q:v", str(self.quality),
               "-f", "image2pipe", "-"]
        log.debug("ffmpeg: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._log_stderr, args=(self.proc,), daemon=True).start()
        n = 0
        t0 = time.monotonic()
        try:
            assert self.proc.stdout
            for jpeg in iter_jpegs(self.proc.stdout):
                if self._stop.is_set():
                    break
                self.dev.show_jpeg(jpeg)
                n += 1
                if self.on_frame:
                    self.on_frame(n)
                if n % 300 == 0:
                    log.info("%.1f fps", n / (time.monotonic() - t0))
        finally:
            self.stop()

    def _log_stderr(self, proc: subprocess.Popen) -> None:
        """Forward ffmpeg errors to the log, except the noise of a requested stop."""
        assert proc.stderr
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line and not self._stop.is_set():
                log.warning("ffmpeg: %s", line)

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def play_video(dev: TurzxDevice, path: str, rotation: int = 90, loop: bool = True,
               fps: float | None = None, scale_mode: str = "cover", quality: int = 4) -> FFmpegStreamer:
    s = FFmpegStreamer(dev, rotation, quality)
    args = ["-re"]
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", path]
    s.run(args, fps or 30, scale_mode)
    return s
