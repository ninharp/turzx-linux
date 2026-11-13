"""Linux sensor provider for the TURZX theme data names.

Maps the DataName identifiers used in .turtheme files (CPULOAD, GPUTEMP, ...)
to values read from psutil, /sys/class/hwmon, /sys/class/drm (AMD) and
NVIDIA's ``nvidia-smi``. Every value is a float in the unit the Windows app
displays (°C, %, MHz, W, RPM, MB, KB/s ...). Missing sensors return None.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable

import psutil

log = logging.getLogger(__name__)


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _readf(path: str, scale: float = 1.0) -> float | None:
    v = _read(path)
    try:
        return float(v) / scale if v is not None else None
    except ValueError:
        return None


class _NvidiaSmi:
    """Keeps one ``nvidia-smi -lms`` process alive and parses its CSV lines."""

    FIELDS = [
        "temperature.gpu",
        "utilization.gpu",
        "clocks.gr",
        "power.draw",
        "memory.used",
        "memory.total",
        "fan.speed",
        "utilization.memory",
        "name",
    ]

    def __init__(self, interval_ms: int = 1000):
        self.values: dict[str, str] = {}
        self.proc: subprocess.Popen | None = None
        exe = shutil.which("nvidia-smi")
        if not exe:
            return
        try:
            self.proc = subprocess.Popen(
                [exe, f"--query-gpu={','.join(self.FIELDS)}", "--format=csv,noheader,nounits",
                 f"-lms={interval_ms}", "-i", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except OSError:
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= len(self.FIELDS):
                self.values = dict(zip(self.FIELDS, parts))

    def get(self, key: str) -> float | None:
        v = self.values.get(key)
        try:
            return float(v) if v not in (None, "", "[N/A]") else None
        except ValueError:
            return None

    @property
    def available(self) -> bool:
        return self.proc is not None

    def close(self) -> None:
        if self.proc:
            self.proc.terminate()


class Sensors:
    def __init__(self, net_iface: str | None = None):
        self._hwmon = self._scan_hwmon()
        self._nv = _NvidiaSmi()
        self._amd = self._find_amdgpu()
        self._net_iface = net_iface
        self._net_last = (time.monotonic(), self._net_bytes())
        self._net_rate = (0.0, 0.0)
        self._rapl = self._find_rapl()
        self._rapl_last: tuple[float, float] | None = None
        self._rapl_power: float | None = None
        psutil.cpu_percent(None)
        self._cache: dict[str, float | str | None] = {}
        self._cpu_model = self._read_cpu_model()

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _scan_hwmon() -> dict[str, str]:
        out = {}
        for h in glob.glob("/sys/class/hwmon/hwmon*"):
            name = _read(os.path.join(h, "name")) or ""
            out.setdefault(name, h)
        return out

    @staticmethod
    def _find_amdgpu() -> str | None:
        for card in glob.glob("/sys/class/drm/card*/device"):
            if os.path.exists(os.path.join(card, "gpu_busy_percent")):
                return card
        return None

    @staticmethod
    def _find_rapl() -> str | None:
        for p in glob.glob("/sys/class/powercap/*/energy_uj"):
            if os.access(p, os.R_OK) and (_read(os.path.join(os.path.dirname(p), "name")) or "").startswith("package"):
                return p
        return None

    @staticmethod
    def _read_cpu_model() -> str:
        txt = _read("/proc/cpuinfo") or ""
        m = re.search(r"model name\s*:\s*(.+)", txt)
        name = m.group(1).strip() if m else "CPU"
        name = re.sub(r"\((R|TM)\)|CPU @.*| Processor|\s+\d+-Core.*|\s+w/.*", "", name)
        return re.sub(r"\s+", " ", name).strip()

    def _net_bytes(self) -> tuple[int, int]:
        counters = psutil.net_io_counters(pernic=True)
        if self._net_iface and self._net_iface in counters:
            c = counters[self._net_iface]
            return c.bytes_sent, c.bytes_recv
        up = down = 0
        for name, c in counters.items():
            if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
                continue
            up += c.bytes_sent
            down += c.bytes_recv
        return up, down

    # -- individual sensors ---------------------------------------------
    def cpu_temp(self) -> float | None:
        for chip in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
            h = self._hwmon.get(chip)
            if not h:
                continue
            # Prefer Tctl/Tdie/Package label
            best = None
            for lab in sorted(glob.glob(os.path.join(h, "temp*_label"))):
                label = _read(lab) or ""
                if label in ("Tctl", "Tdie", "Package id 0"):
                    best = lab.replace("_label", "_input")
                    if label != "Tctl":
                        break
            v = _readf(best or os.path.join(h, "temp1_input"), 1000)
            if v is not None:
                return v
        return None

    def cpu_fan(self) -> float | None:
        for h in self._hwmon.values():
            for f in sorted(glob.glob(os.path.join(h, "fan*_input"))):
                v = _readf(f)
                if v:
                    return v
        return None

    def cpu_power(self) -> float | None:
        if not self._rapl:
            return None
        now = time.monotonic()
        e = _readf(self._rapl)
        if e is None:
            return None
        if self._rapl_last:
            dt = now - self._rapl_last[0]
            de = e - self._rapl_last[1]
            if dt > 0 and de >= 0:
                self._rapl_power = de / dt / 1e6
        self._rapl_last = (now, e)
        return self._rapl_power

    def gpu(self, key: str) -> float | None:
        if self._nv.available:
            m = {
                "GPUTEMP": "temperature.gpu", "GPULOAD": "utilization.gpu",
                "GPUCLOCK": "clocks.gr", "GPUPWR": "power.draw", "GPUFAN": "fan.speed",
                "GPURAM": "memory.used",
            }
            if key in m:
                return self._nv.get(m[key])
            used, total = self._nv.get("memory.used"), self._nv.get("memory.total")
            if key == "GPURAMLOAD" and used is not None and total:
                return used / total * 100
            if key == "GPUVALIDRAM" and used is not None and total is not None:
                return total - used
            return None
        if self._amd:
            d = self._amd
            hw = (glob.glob(os.path.join(d, "hwmon/hwmon*")) or [None])[0]
            if key == "GPULOAD":
                return _readf(os.path.join(d, "gpu_busy_percent"))
            if key == "GPUTEMP" and hw:
                return _readf(os.path.join(hw, "temp1_input"), 1000)
            if key == "GPUPWR" and hw:
                return _readf(os.path.join(hw, "power1_average"), 1e6) or _readf(os.path.join(hw, "power1_input"), 1e6)
            if key == "GPUCLOCK" and hw:
                return _readf(os.path.join(hw, "freq1_input"), 1e6)
            if key == "GPUFAN" and hw:
                return _readf(os.path.join(hw, "fan1_input"))
            used = _readf(os.path.join(d, "mem_info_vram_used"), 1 << 20)
            total = _readf(os.path.join(d, "mem_info_vram_total"), 1 << 20)
            if key == "GPURAM":
                return used
            if key == "GPURAMLOAD" and used is not None and total:
                return used / total * 100
            if key == "GPUVALIDRAM" and used is not None and total is not None:
                return total - used
        return None

    def gpu_model(self) -> str:
        if self._nv.available:
            return self._nv.values.get("name", "GPU")
        return "GPU"

    def volume(self) -> float | None:
        now = time.monotonic()
        if now - getattr(self, "_vol_t", 0) < 2:
            return getattr(self, "_vol", None)
        self._vol_t = now
        vol = None
        try:
            if shutil.which("wpctl"):
                out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                                     capture_output=True, text=True, timeout=1).stdout
                m = re.search(r"([0-9.]+)", out)
                vol = 0.0 if "MUTED" in out else (float(m.group(1)) * 100 if m else None)
            elif shutil.which("pactl"):
                out = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                                     capture_output=True, text=True, timeout=1).stdout
                m = re.search(r"(\d+)%", out)
                vol = float(m.group(1)) if m else None
        except (OSError, subprocess.SubprocessError):
            vol = None
        self._vol = vol
        return vol

    def disk_usage(self, sub: str | None) -> float | None:
        """DRVLOAD: Windows drive letters are mapped: C -> /, D -> /home, else mount points."""
        mounts = {"C": "/", "D": "/home"}
        path = mounts.get((sub or "C").upper(), sub if sub and sub.startswith("/") else "/")
        try:
            return psutil.disk_usage(path).percent
        except OSError:
            return None

    def hdd_temp(self) -> float | None:
        for name, h in self._hwmon.items():
            if name in ("nvme", "drivetemp"):
                return _readf(os.path.join(h, "temp1_input"), 1000)
        return None

    # -- update loop -----------------------------------------------------
    def update(self) -> dict[str, float | str | None]:
        now = time.monotonic()
        up, down = self._net_bytes()
        t0, (u0, d0) = self._net_last
        dt = now - t0
        if dt > 0.2:
            self._net_rate = ((up - u0) / dt / 1024, (down - d0) / dt / 1024)
            self._net_last = (now, (up, down))
        vm = psutil.virtual_memory()
        freq = psutil.cpu_freq()
        c: dict[str, float | str | None] = {
            "CPULOAD": psutil.cpu_percent(None),
            "CPUTEMP": self.cpu_temp(),
            "CPUCLOCK": freq.current if freq else None,
            "CPUPWR": self.cpu_power(),
            "CPUFAN": self.cpu_fan(),
            "CPUVOLTAGE": None,
            "CPUMODEL": self._cpu_model,
            "GPUTEMP": self.gpu("GPUTEMP"),
            "GPUCLOCK": self.gpu("GPUCLOCK"),
            "GPULOAD": self.gpu("GPULOAD"),
            "GPUPWR": self.gpu("GPUPWR"),
            "GPUFAN": self.gpu("GPUFAN"),
            "GPURAM": self.gpu("GPURAM"),
            "GPURAMLOAD": self.gpu("GPURAMLOAD"),
            "GPUVALIDRAM": self.gpu("GPUVALIDRAM"),
            "GPUVOLTAGE": None,
            "GPUMODEL": self.gpu_model(),
            "RAMLOAD": vm.percent,
            "RAM": (vm.total - vm.available) / (1 << 20),
            "RAMVALID": vm.available / (1 << 20),
            "RAMTOTAL": vm.total / (1 << 20),
            "RAM_GB": (vm.total - vm.available) / (1 << 30),
            "RAMVALID_GB": vm.available / (1 << 30),
            "RAMTOTAL_GB": vm.total / (1 << 30),
            "RAMMODEL": "RAM",
            "HDDTEMP": self.hdd_temp(),
            "UPSPEED": self._net_rate[0],
            "DOWNDSPEED": self._net_rate[1],
            "WATERPUMP": None,
            "FPS": None,
            "Volume": self.volume(),
            "Weather": None,
        }
        self._cache = c
        return c

    def get(self, name: str, sub: str | None = None) -> float | str | None:
        if name == "DRVLOAD":
            return self.disk_usage(sub)
        if name == "HDDUSED":
            return self.disk_usage(sub)
        return self._cache.get(name)

    def close(self) -> None:
        self._nv.close()


SensorFn = Callable[[str, "str | None"], "float | str | None"]
