"""Generate a minimal EDID 1.3 for a 1920x480@60 "ghost" monitor.

Used with the NVIDIA driver options ConnectedMonitor + CustomEDID so that a
free connector reports a monitor of exactly the TURZX panel size. X then has a
real CRTC there: pointer and windows can move onto it, and turzx captures it.
"""
from __future__ import annotations

import struct


def _dtd(clock_khz: int, ha: int, hb: int, hfp: int, hs: int, va: int, vb: int, vfp: int, vs: int,
         wmm: int, hmm: int) -> bytes:
    c = clock_khz // 10
    return bytes([
        c & 0xFF, c >> 8,
        ha & 0xFF, hb & 0xFF, ((ha >> 8) << 4) | (hb >> 8),
        va & 0xFF, vb & 0xFF, ((va >> 8) << 4) | (vb >> 8),
        hfp & 0xFF, hs & 0xFF, ((vfp & 0xF) << 4) | (vs & 0xF),
        ((hfp >> 8) << 6) | ((hs >> 8) << 4) | ((vfp >> 4) << 2) | (vs >> 4),
        wmm & 0xFF, hmm & 0xFF, ((wmm >> 8) << 4) | (hmm >> 8),
        0, 0, 0x1E,  # no border, digital separate sync, +hsync +vsync
    ])


def _text(tag: int, s: str) -> bytes:
    b = s.encode("ascii")[:13]
    if len(b) < 13:
        b += b"\n" + b" " * (12 - len(b))
    return bytes([0, 0, 0, tag, 0]) + b


def make_edid(width: int = 1920, height: int = 480, refresh: int = 60, name: str = "TURZX 8.8") -> bytes:
    hfp, hs, hbp = 48, 32, 80
    vfp, vs, vbp = 3, 5, 6
    htot, vtot = width + hfp + hs + hbp, height + vfp + vs + vbp
    clock_khz = htot * vtot * refresh // 1000
    wmm, hmm = round(width * 0.11), round(height * 0.11)

    e = bytearray(b"\x00\xff\xff\xff\xff\xff\xff\x00")
    mfg = ((ord("T") - 64) << 10) | ((ord("R") - 64) << 5) | (ord("Z") - 64)
    e += struct.pack(">H", mfg) + struct.pack("<H", 0x0088) + struct.pack("<I", 1)
    e += bytes([1, 2025 - 1990, 1, 3])                       # week, year, EDID 1.3
    e += bytes([0x80, (wmm + 5) // 10, (hmm + 5) // 10, 120, 0x0A])  # digital, cm, gamma 2.2, RGB+preferred
    e += bytes([0xEE, 0x91, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])  # sRGB chromaticity
    e += bytes([0, 0, 0])                                    # no established timings
    e += b"\x01\x01" * 8                                     # no standard timings
    e += _dtd(clock_khz, width, htot - width, hfp, hs, height, vtot - height, vfp, vs, wmm, hmm)
    e += _text(0xFC, name)
    hk = clock_khz // htot
    e += bytes([0, 0, 0, 0xFD, 0, refresh - 10, refresh + 15, max(1, hk - 10), hk + 10,
                (clock_khz // 10000) + 2, 0, 0x0A, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20])
    e += _text(0xFE, "virtual")
    e += bytes([0])                                          # no extensions
    e += bytes([(-sum(e)) & 0xFF])
    assert len(e) == 128
    return bytes(e)
