"""Resolve Windows font family names (as stored in themes) to font files."""
from __future__ import annotations

import glob
import logging
import os
import subprocess
from functools import lru_cache

from PIL import ImageFont

log = logging.getLogger(__name__)

FONT_DIRS = [
    os.path.expanduser("~/.local/share/turzx/fonts"),
    os.path.join(os.path.dirname(__file__), "fonts"),
]
FALLBACKS = ["DejaVu Sans", "Noto Sans", "Liberation Sans"]
# common Windows fonts -> metric compatible / similar Linux fonts
ALIASES = {
    "arial": ["Liberation Sans", "Arimo", "DejaVu Sans"],
    "microsoftyahei": ["Noto Sans CJK SC", "WenQuanYi Micro Hei"],
    "微软雅黑": ["Noto Sans CJK SC", "WenQuanYi Micro Hei"],
    "bahnschrift": ["DejaVu Sans Condensed", "Liberation Sans Narrow", "DejaVu Sans"],
    "bahnschriftsemibold": ["DejaVu Sans Condensed:bold", "DejaVu Sans:bold"],
    "bahnschriftsemiboldcondensed": ["DejaVu Sans Condensed:bold", "DejaVu Sans:bold"],
    "bahnschriftcondensed": ["DejaVu Sans Condensed", "DejaVu Sans"],
    "segoeui": ["Noto Sans", "DejaVu Sans"],
    "timesnewroman": ["Liberation Serif", "DejaVu Serif"],
    "consolas": ["DejaVu Sans Mono", "Liberation Mono"],
    # DejaVu Sans descends from Bitstream Vera, which has Verdana-like proportions
    "verdana": ["DejaVu Sans", "Noto Sans"],
    "tahoma": ["DejaVu Sans Condensed", "DejaVu Sans"],
    "calibri": ["Carlito", "Noto Sans"],
    "cambria": ["Caladea", "Noto Serif"],
    "couriernew": ["Liberation Mono", "DejaVu Sans Mono"],
    "georgia": ["Gelasio", "DejaVu Serif"],
    "trebuchetms": ["DejaVu Sans", "Noto Sans"],
    "yugothic": ["Noto Sans CJK JP", "Noto Sans CJK SC"],
    "msgothic": ["Noto Sans CJK JP", "Noto Sans CJK SC"],
}
# OS/2 usWinAscent / usWinDescent (per em) of common Windows fonts. GDI+ puts the
# baseline at the cell ascent of the font the theme names; when that font is
# substituted on Linux, these keep text at the position the Windows app uses.
WIN_METRICS = {
    "verdana": (2059 / 2048, 430 / 2048),
    "tahoma": (2049 / 2048, 423 / 2048),
    "arial": (1854 / 2048, 434 / 2048),
    "timesnewroman": (1825 / 2048, 443 / 2048),
    "segoeui": (2210 / 2048, 514 / 2048),
    "consolas": (1884 / 2048, 514 / 2048),
    "calibri": (1950 / 2048, 550 / 2048),
    "georgia": (1878 / 2048, 449 / 2048),
    "couriernew": (1705 / 2048, 615 / 2048),
    "trebuchetms": (1923 / 2048, 455 / 2048),
    "impact": (2066 / 2048, 432 / 2048),
    "comicsansms": (2257 / 2048, 597 / 2048),
    "microsoftyahei": (2167 / 2048, 536 / 2048),
    "微软雅黑": (2167 / 2048, 536 / 2048),
}
CJK_FALLBACKS = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "Source Han Sans CN"]


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


class FontIndex:
    def __init__(self, extra_dirs: list[str] | None = None):
        self.by_name: dict[str, list[tuple[str, int, bool, bool]]] = {}
        self.families: set[str] = set()  # human readable family names (for UI lists)
        dirs = list(extra_dirs or []) + FONT_DIRS
        files: list[str] = []
        for d in dirs:
            for ext in ("ttf", "otf", "ttc", "TTF", "OTF", "TTC"):
                files += glob.glob(os.path.join(d, "**", f"*.{ext}"), recursive=True)
        for f in files:
            self._index_file(f)
        self._fc_cache: dict[str, str | None] = {}

    def _index_file(self, path: str) -> None:
        try:
            from fontTools.ttLib import TTCollection, TTFont

            fonts = TTCollection(path).fonts if path.lower().endswith(".ttc") else [TTFont(path, lazy=True)]
        except Exception:  # noqa: BLE001 - broken font files are skipped
            return
        for idx, tt in enumerate(fonts):
            try:
                name = tt["name"]
                sub = (name.getDebugName(2) or "").lower()
                full = name.getDebugName(4) or ""
                # all platforms/languages: GDI uses the Windows (often legacy
                # or localized, e.g. Chinese) family name stored in the theme
                keys = set()
                for rec in name.names:
                    if rec.nameID in (1, 4, 16, 21):
                        try:
                            keys.add(rec.toUnicode())
                            if rec.nameID == 1 and rec.platformID == 3:
                                self.families.add(rec.toUnicode())
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                continue
            # GDI style linking uses OS/2.fsSelection (bit 0 italic, bit 5 bold)
            try:
                fs = tt["OS/2"].fsSelection
                bold, italic = bool(fs & 0x20), bool(fs & 0x01)
            except Exception:  # noqa: BLE001
                bold = "bold" in sub or "bold" in full.lower()
                italic = "italic" in sub or "oblique" in sub
            for key in keys:
                if key:
                    self.by_name.setdefault(_norm(key), []).append((path, idx, bold, italic))

    def _fc_match(self, family: str, bold: bool) -> str | None:
        key = f"{family}|{bold}"
        if key not in self._fc_cache:
            try:
                pat = family + (":bold" if bold else "")
                out = subprocess.run(["fc-match", "-f", "%{file}|%{family}", pat],
                                     capture_output=True, text=True, timeout=5).stdout
                path, fam = (out.split("|", 1) + [""])[:2]
                # only accept when fontconfig actually found that family
                self._fc_cache[key] = path if _norm(family) in _norm(fam) else None
            except (OSError, subprocess.SubprocessError):
                self._fc_cache[key] = None
        return self._fc_cache[key]

    def resolve(self, family: str | None, bold: bool = False) -> tuple[str, int, bool]:
        """Return (path, index, needs_fake_bold)."""
        fam = family or ""
        cands = self.by_name.get(_norm(fam), [])
        if cands:
            # prefer upright, then matching weight (GDI regular/bold style link)
            p, i, b, _ = min(cands, key=lambda c: (c[3], c[2] != bold))
            return p, i, bold and not b
        p = self._fc_match(fam, bold) if fam else None
        if p:
            return p, 0, False
        for alias in ALIASES.get(_norm(fam), ALIASES.get(fam, [])):
            a, _, style = alias.partition(":")
            p = self._fc_match(a, bold or style == "bold")
            if p:
                return p, 0, False
        # Windows app falls back to "Microsoft YaHei"; use a CJK capable sans
        for fb in CJK_FALLBACKS + FALLBACKS:
            p = self._fc_match(fb, bold)
            if p:
                return p, 0, False
        raise RuntimeError("no fallback font found")

    def family_list(self) -> list[str]:
        """Theme/app font families plus the system's fontconfig families."""
        fams = set(self.families)
        try:
            out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                fams.add(line.split(",")[0].strip())
        except (OSError, subprocess.SubprocessError):
            pass
        return sorted((f for f in fams if f), key=str.lower)

    def is_installed(self, family: str | None) -> bool:
        """True if the theme's font itself is available (not a substitute)."""
        if not family:
            return False
        return bool(self.by_name.get(_norm(family))) or bool(self._fc_match(family, False))

    @lru_cache(maxsize=256)
    def get(self, family: str | None, size_px: int, bold: bool = False) -> tuple[ImageFont.FreeTypeFont, bool]:
        path, idx, fake_bold = self.resolve(family, bold)
        font = ImageFont.truetype(path, max(1, size_px), index=idx)
        asc, desc = _win_metrics(path, idx)
        key = _norm(family or "")
        win = WIN_METRICS.get(key) or WIN_METRICS.get(family or "")
        if win and not self.is_installed(family):
            asc, desc = win  # substitute font: keep the original font's line placement
        # GDI+ places the line box top at y and the baseline at the cell
        # ascent (OS/2 usWinAscent), not at the hhea ascender FreeType uses.
        font.gdi_ascent = asc * size_px if asc else font.getmetrics()[0]
        font.gdi_descent = desc * size_px if desc else font.getmetrics()[1]
        return font, fake_bold


@lru_cache(maxsize=256)
def _win_metrics(path: str, idx: int) -> tuple[float, float]:
    try:
        from fontTools.ttLib import TTFont

        tt = TTFont(path, fontNumber=idx, lazy=True)
        upem = tt["head"].unitsPerEm
        os2 = tt["OS/2"]
        return os2.usWinAscent / upem, os2.usWinDescent / upem
    except Exception:  # noqa: BLE001
        return 0.0, 0.0
