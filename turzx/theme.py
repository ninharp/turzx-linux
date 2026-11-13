"""Load TURZX .turtheme files and compute widget values (port of M_Data)."""
from __future__ import annotations

import datetime as _dt
import io
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageColor

from . import nrbf
from .nrbf import NObj, unwrap_list

# System.Drawing.KnownColor enumeration (index -> name)
_KNOWN = (
    "ActiveBorder ActiveCaption ActiveCaptionText AppWorkspace Control ControlDark "
    "ControlDarkDark ControlLight ControlLightLight ControlText Desktop GrayText Highlight "
    "HighlightText HotTrack InactiveBorder InactiveCaption InactiveCaptionText Info InfoText "
    "Menu MenuText ScrollBar Window WindowFrame WindowText Transparent AliceBlue AntiqueWhite "
    "Aqua Aquamarine Azure Beige Bisque Black BlanchedAlmond Blue BlueViolet Brown BurlyWood "
    "CadetBlue Chartreuse Chocolate Coral CornflowerBlue Cornsilk Crimson Cyan DarkBlue "
    "DarkCyan DarkGoldenrod DarkGray DarkGreen DarkKhaki DarkMagenta DarkOliveGreen DarkOrange "
    "DarkOrchid DarkRed DarkSalmon DarkSeaGreen DarkSlateBlue DarkSlateGray DarkTurquoise "
    "DarkViolet DeepPink DeepSkyBlue DimGray DodgerBlue Firebrick FloralWhite ForestGreen "
    "Fuchsia Gainsboro GhostWhite Gold Goldenrod Gray Green GreenYellow Honeydew HotPink "
    "IndianRed Indigo Ivory Khaki Lavender LavenderBlush LawnGreen LemonChiffon LightBlue "
    "LightCoral LightCyan LightGoldenrodYellow LightGray LightGreen LightPink LightSalmon "
    "LightSeaGreen LightSkyBlue LightSlateGray LightSteelBlue LightYellow Lime LimeGreen Linen "
    "Magenta Maroon MediumAquamarine MediumBlue MediumOrchid MediumPurple MediumSeaGreen "
    "MediumSlateBlue MediumSpringGreen MediumTurquoise MediumVioletRed MidnightBlue MintCream "
    "MistyRose Moccasin NavajoWhite Navy OldLace Olive OliveDrab Orange OrangeRed Orchid "
    "PaleGoldenrod PaleGreen PaleTurquoise PaleVioletRed PapayaWhip PeachPuff Peru Pink Plum "
    "PowderBlue Purple Red RosyBrown RoyalBlue SaddleBrown Salmon SandyBrown SeaGreen SeaShell "
    "Sienna Silver SkyBlue SlateBlue SlateGray Snow SpringGreen SteelBlue Tan Teal Thistle "
    "Tomato Turquoise Violet Wheat White WhiteSmoke Yellow YellowGreen ButtonFace "
    "ButtonHighlight ButtonShadow GradientActiveCaption GradientInactiveCaption MenuBar "
    "MenuHighlight"
).split()
_SYSTEM_COLORS = {  # rough Windows 10 defaults
    "Control": (240, 240, 240), "ControlText": (0, 0, 0), "Window": (255, 255, 255),
    "WindowText": (0, 0, 0), "Highlight": (0, 120, 215), "HighlightText": (255, 255, 255),
    "GrayText": (109, 109, 109), "ButtonFace": (240, 240, 240),
}

Color = tuple[int, int, int, int]


def color(c: Any, default: Color = (128, 128, 128, 255)) -> Color:
    """System.Drawing.Color -> RGBA tuple."""
    if not isinstance(c, dict):
        return default
    state = c.get("state", 0)
    if state & 2:  # ARGB value valid
        v = c.get("value", 0) & 0xFFFFFFFF
        return ((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)
    kc = c.get("knownColor", 0)
    if state & 1 and 1 <= kc <= len(_KNOWN):
        name = _KNOWN[kc - 1]
        if name == "Transparent":
            return (255, 255, 255, 0)
        if name in _SYSTEM_COLORS:
            return (*_SYSTEM_COLORS[name], 255)
        try:
            return (*ImageColor.getrgb(name.lower()), 255)
        except ValueError:
            return default
    if c.get("name"):
        try:
            return (*ImageColor.getrgb(str(c["name"]).lower()), 255)
        except ValueError:
            pass
    return (0, 0, 0, 0) if state == 0 else default


def bitmap(b: Any) -> Image.Image | None:
    """System.Drawing.Bitmap (serialized as PNG bytes in 'Data') -> PIL image."""
    if isinstance(b, dict) and isinstance(b.get("Data"), (bytes, bytearray)):
        try:
            im = Image.open(io.BytesIO(b["Data"]))
            im.load()
            return im.convert("RGBA")
        except Exception:  # noqa: BLE001
            return None
    return None


def m(o: Any, name: str, default: Any = None) -> Any:
    """Member lookup tolerant to auto-property backing fields and base-class prefixes."""
    if not isinstance(o, dict):
        return default
    if name in o:
        return o[name]
    bk = f"<{name}>k__BackingField"
    for k, v in o.items():
        if k == bk or k.endswith("+" + bk) or k.endswith("+" + name):
            return v
    return default


# ---------------------------------------------------------------------------
# editable theme model

# property schema per widget type: key -> (kind, default, label[, choices])
# kinds: int, float, bool, color, choice, font, image, text
DATA_NAMES = [
    "CPULOAD", "CPUTEMP", "CPUCLOCK", "CPUCLOCK_G", "CPUPWR", "CPUFAN", "CPUVOLTAGE", "CPUMODEL",
    "GPULOAD", "GPUTEMP", "GPUCLOCK", "GPUCLOCK_G", "GPUPWR", "GPUFAN", "GPURAM", "GPURAMLOAD",
    "GPUVALIDRAM", "GPUVOLTAGE", "GPUMODEL", "RAMLOAD", "RAM", "RAMVALID", "RAMTOTAL", "RAM_GB",
    "RAMVALID_GB", "RAMTOTAL_GB", "RAMMODEL", "DRVLOAD", "HDDTEMP", "HDDUSED", "UPSPEED",
    "DOWNDSPEED", "Volume", "FPS", "WATERPUMP", "TIME", "DATE", "DAY", "APM", "Weather",
]
SUB_NAMES = {
    "TIME": ["h:m:s", "h:m", "h_12", "h_24", "m", "s"],
    "DATE": ["Y-M-D", "Y", "M", "M_en", "M_cn", "D"],
    "DAY": ["Day_en", "Day_cn", "Num", "Num_cn"],
    "DRVLOAD": ["C", "D", "/", "/home"],
    "HDDUSED": ["C", "D", "/", "/home"],
}
DATA_LABELS = {
    "CPULOAD": "CPU load %", "CPUTEMP": "CPU temperature", "CPUCLOCK": "CPU clock MHz",
    "CPUCLOCK_G": "CPU clock GHz", "CPUPWR": "CPU power W", "CPUFAN": "CPU fan", "CPUMODEL": "CPU model",
    "GPULOAD": "GPU load %", "GPUTEMP": "GPU temperature", "GPUCLOCK": "GPU clock MHz", "GPUCLOCK_G": "GPU clock GHz",
    "GPUPWR": "GPU power W", "GPUFAN": "GPU fan", "GPURAM": "VRAM used MB",
    "GPURAMLOAD": "VRAM load %", "GPUVALIDRAM": "VRAM free MB", "GPUMODEL": "GPU model",
    "RAMLOAD": "RAM load %", "RAM": "RAM used MB", "RAMVALID": "RAM free MB", "RAMTOTAL": "RAM total MB",
    "RAM_GB": "RAM used GB", "RAMVALID_GB": "RAM free GB", "RAMTOTAL_GB": "RAM total GB",
    "DRVLOAD": "Drive used %", "HDDTEMP": "SSD/HDD temperature", "HDDUSED": "Drive used %",
    "UPSPEED": "Network upload", "DOWNDSPEED": "Network download", "Volume": "Volume", "TIME": "Time",
    "DATE": "Date", "DAY": "Weekday", "APM": "AM/PM",
}

_SOURCE = {
    "data_name": ("choice", "CPULOAD", "Sensor", DATA_NAMES),
    "sub_name": ("choice", "", "Format / sub value", []),
    "show_unit": ("bool", True, "Show unit"),
    "fahrenheit": ("bool", False, "Fahrenheit"),
}
_FONT = {
    "font_name": ("font", "HarmonyOS Sans", "Font"),
    "font_size": ("float", 24.0, "Size (pt)"),
    "bold": ("bool", False, "Bold"),
    "color": ("color", (255, 255, 255, 255), "Color"),
    "align": ("choice", 0, "Alignment", [(0, "left"), (1, "center"), (2, "right")]),
    "interval": ("float", 0.0, "Letter spacing"),
    "gr_dir": ("choice", 0, "Gradient", [(0, "off"), (1, "horizontal"), (2, "vertical"),
                                        (3, "diagonal ↘"), (4, "diagonal ↗")]),
    "gr_color": ("color", (255, 255, 255, 255), "Gradient color"),
}
_BAR_COLORS = {
    "FrontColor": ("color", (0, 200, 255, 255), "Foreground"),
    "BackColor": ("color", (60, 60, 60, 255), "Background"),
    "GradientColor": ("color", (255, 0, 128, 255), "Gradient to"),
    "useGradient": ("bool", False, "Use gradient"),
    "trBack": ("bool", False, "Transparent background"),
    "revert": ("bool", False, "Reversed"),
    "SplitBlockWidth": ("int", 0, "Block size (0=default)"),
    "SplitBlankWidth": ("int", 0, "Block gap (0=default)"),
}
SCHEMA: dict[str, dict[str, tuple]] = {
    "Text": {"text": ("text", "Text", "Text"), **_FONT},
    "Data": {**_SOURCE, **_FONT},
    "Image": {"image": ("image", None, "Image")},
    "StatuBar": {
        **_SOURCE, "width": ("int", 200, "Length"), "height": ("int", 12, "Thickness"),
        "direction": ("choice", 0, "Direction", [(0, "→"), (1, "←"), (2, "↓"), (3, "↑")]),
        "radius": ("int", 0, "Corner radius"), "lineWidth": ("int", 0, "Border"),
        "fillBack": ("bool", True, "Fill background"), "useSubsection": ("bool", False, "Segments"),
        **_BAR_COLORS,
    },
    "ArchBar": {
        **_SOURCE, "diameter": ("int", 150, "Diameter"), "archWidth": ("int", 12, "Arc width"),
        "totalAngel": ("int", 360, "Total angle"), "startPer": ("int", 0, "Start (% of circle)"),
        "round": ("bool", False, "Round caps"), "useBlock": ("bool", False, "Segments"),
        "HasRingBorder": ("bool", False, "Ring border"), **_BAR_COLORS,
    },
    "Clock": {
        **_SOURCE, "image": ("image", None, "Needle image"), "centerX": ("int", 120, "Pivot X"),
        "centerY": ("int", 120, "Pivot Y"), "angle": ("float", 0.0, "Start angle"),
        "endAngle": ("float", 270.0, "Angle range"), "offset": ("float", 0.0, "Value offset"),
        "AngleOffset": ("float", 0.0, "Image angle offset"), "revert": ("bool", False, "Counter-clockwise"),
    },
    "Chart": {
        **_SOURCE, "width": ("int", 300, "Width"), "height": ("int", 60, "Height"),
        "columnWidth": ("int", 5, "Point spacing"), "maxValue": ("float", 100.0, "Max value"),
        "lineWidth": ("int", 2, "Line width"), "LineColor": ("color", (0, 200, 255, 255), "Line color"),
        "FillColor": ("color", (0, 200, 255, 80), "Fill color"), "borderWidth": ("int", 0, "Border width"),
        "BorderColor": ("color", (255, 255, 255, 255), "Border color"),
        "rollDirection": ("bool", True, "Newest on the right"),
    },
}
TYPE_LABELS = {"Text": "Text", "Data": "Sensor value", "Image": "Image", "StatuBar": "Bar",
               "ArchBar": "Arc / ring", "Clock": "Needle", "Chart": "History chart",
               "Animation": "Video placeholder"}


@dataclass
class Item:
    type: str
    x: int = 0
    y: int = 0
    hidden: bool = False
    name: str = ""
    p: dict = field(default_factory=dict)       # editable properties (see SCHEMA)
    image: Image.Image | None = None           # for Image / Clock
    history: deque = field(default_factory=lambda: deque(maxlen=400))
    rate: float = 0.0
    value: str = ""
    text: str = ""

    # convenience accessors used by compute()
    @property
    def data_name(self) -> str | None:
        if self.type == "Text":
            return "StaticText"
        return self.p.get("data_name")

    @property
    def sub_name(self) -> str | None:
        return self.p.get("sub_name") or None

    @property
    def show_unit(self) -> bool:
        return bool(self.p.get("show_unit", True))

    @property
    def fahrenheit(self) -> bool:
        return bool(self.p.get("fahrenheit", False))

    @property
    def static_text(self) -> str:
        return str(self.p.get("text", ""))

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        t = TYPE_LABELS.get(self.type, self.type)
        if self.type == "Text":
            return f"{t}: {self.static_text[:20]}"
        if self.data_name and self.type != "Image":
            return f"{t}: {DATA_LABELS.get(self.data_name, self.data_name)}"
        return t

    def copy(self) -> "Item":
        import copy as _copy

        n = Item(self.type, self.x, self.y, self.hidden, self.name, _copy.deepcopy(self.p), self.image)
        n.history = _copy.copy(self.history)
        n.rate, n.value, n.text = self.rate, self.value, self.text
        return n


def new_item(typ: str, x: int = 50, y: int = 50) -> Item:
    p = {k: v[1] for k, v in SCHEMA.get(typ, {}).items() if v[0] != "image"}
    return Item(typ, x, y, p=p)


@dataclass
class Theme:
    path: str
    name: str
    width: int
    height: int
    video_name: str | None
    video_path: str | None
    frame_rate: int
    items: list[Item]
    preview: Image.Image | None = None

    @property
    def landscape(self) -> bool:
        return self.width > self.height

    def find_video(self, search_dirs: list[str]) -> str | None:
        if self.video_path and os.path.isfile(self.video_path):
            return self.video_path
        if not self.video_name and not self.video_path:
            return None
        names = [n for n in (self.video_name, os.path.basename((self.video_path or "").replace("\\", "/"))) if n]
        theme_dir = os.path.dirname(self.path)
        res = f"{min(self.width, self.height)}{max(self.width, self.height)}"
        dirs = list(search_dirs) + [
            theme_dir,
            os.path.join(os.path.dirname(os.path.dirname(theme_dir)), "video", res),
            os.path.join(os.path.dirname(os.path.dirname(theme_dir)), "video", os.path.basename(theme_dir)),
            os.path.expanduser(f"~/.local/share/turzx/video/{res}"),
        ]
        for d in dirs:
            for n in names:
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    return p
        return None


def new_theme(width: int = 1920, height: int = 480, name: str = "New theme") -> Theme:
    return Theme("", name, width, height, None, None, 24, [])


# --- import from .turtheme ---------------------------------------------------
def _alpha_fold(c: Color, a) -> Color:
    if a is None:
        return c
    a = int(a) or 255
    return (c[0], c[1], c[2], a)


def _from_nobj(g: NObj) -> Item:
    typ = m(g, "TypeName") or g.cls.rsplit(".", 1)[-1]
    it = Item(typ, int(m(g, "posX", 0) or 0), int(m(g, "posY", 0) or 0), bool(m(g, "hide", False)))
    p = it.p
    p["fahrenheit"] = bool(m(g, "fahrenheit", False))
    md = m(g, "m_data")
    if isinstance(md, dict):
        p["data_name"] = m(md, "DataName")
        p["sub_name"] = m(md, "SubName") or ""
        p["show_unit"] = bool(m(md, "ShowUnit", True))
        if p["data_name"] == "StaticText":
            p["text"] = m(md, "ValueWithUnit") or m(md, "_Value") or ""
            if typ == "Data":
                it.type = typ = "Text"
        try:
            it.rate = float(m(md, "Rate", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        it.value = str(m(md, "_Value", "") or "")
        it.text = str(m(md, "ValueWithUnit", "") or "")
    fc = m(g, "fontConfig")
    if isinstance(fc, dict):
        al = m(fc, "alignment")
        col = color(m(fc, "color"), (128, 128, 128, 255))
        p.update(font_name=m(fc, "name"), font_size=float(m(fc, "size", 24) or 24),
                 bold=bool(m(fc, "isBold", False)), color=col,
                 align=int(m(al, "index", 0) or 0) if isinstance(al, dict) else 0,
                 interval=float(m(fc, "interval", 0.0) or 0.0), gr_dir=int(m(fc, "GrDirection", 0) or 0),
                 gr_color=color(m(fc, "GrColor"), col))
    if typ in ("Image", "Clock", "Animation"):
        it.image = bitmap(m(g, "bitmap")) or bitmap(m(g, "O_bitmap"))
    if typ in ("StatuBar", "ArchBar", "DynamicBar"):
        if typ == "DynamicBar":
            it.type = typ = "StatuBar"
        for k in ("width", "height", "direction", "radius", "lineWidth", "diameter", "archWidth",
                  "totalAngel", "startPer", "SplitBlockWidth", "SplitBlankWidth"):
            v = m(g, k)
            if v is not None:
                p[k] = int(v)
        for k in ("trBack", "fillBack", "useGradient", "useSubsection", "round", "useBlock",
                  "HasRingBorder", "revert"):
            v = m(g, k)
            if v is not None:
                p[k] = bool(v)
        p["FrontColor"] = _alpha_fold(color(m(g, "FrontColor")), m(g, "FrontAlpha"))
        p["BackColor"] = _alpha_fold(color(m(g, "BackColor")), m(g, "BackAlpha"))
        p["GradientColor"] = color(m(g, "GradientColor"))
    if typ == "Clock":
        for k in ("centerX", "centerY"):
            p[k] = int(m(g, k, 0) or 0)
        for k in ("angle", "endAngle", "offset", "AngleOffset"):
            p[k] = float(m(g, k, 0) or 0)
        p["revert"] = bool(m(g, "revert", False))
    if typ == "Chart":
        p.update(width=int(m(g, "_width", 200) or 200), height=int(m(g, "_height", 50) or 50),
                 columnWidth=int(m(g, "columnWidth", 5) or 5), maxValue=float(m(g, "maxValue", 100) or 100),
                 lineWidth=int(m(g, "lineWidth", 1) or 0), borderWidth=int(m(g, "borderWidth", 0) or 0),
                 LineColor=color(m(g, "LineColor")), FillColor=color(m(g, "FillColor")),
                 BorderColor=color(m(g, "BorderColor")), rollDirection=bool(m(g, "rollDirection", False)))
    return it


def import_turtheme(path: str) -> Theme:
    t = nrbf.load(path)
    if not isinstance(t, NObj) or not t.cls.endswith("Theme"):
        raise ValueError(f"{path}: not a TURZX theme file")
    gl = m(t, "GraphList")
    raw_items = unwrap_list(gl.get("Collection`1+items")) if isinstance(gl, dict) else []
    items = [_from_nobj(g) for g in raw_items if g is not None]
    th = Theme(
        path=path, name=m(t, "name") or os.path.splitext(os.path.basename(path))[0],
        width=int(m(t, "width", 1920)), height=int(m(t, "height", 480)),
        video_name=m(t, "videoName"), video_path=None,
        frame_rate=int(m(t, "FrameRate", 0) or 0), items=items, preview=bitmap(m(t, "themePic")),
    )
    vp = m(t, "videoPath")
    if vp:
        th.video_name = th.video_name or os.path.basename(vp.replace("\\", "/"))
    return th


# --- .turzx package (zip: theme.json + assets/) -------------------------------
FORMAT = "turzx-theme/1"


def _enc(v):
    if isinstance(v, tuple) and len(v) == 4 and all(isinstance(c, int) for c in v):
        return "#%02x%02x%02x%02x" % v
    return v


def _dec(kind: str, v):
    if kind == "color" and isinstance(v, str) and v.startswith("#"):
        h = v[1:]
        if len(h) == 6:
            h += "ff"
        return tuple(int(h[i:i + 2], 16) for i in range(0, 8, 2))
    return v


def save_turzx(theme: Theme, path: str, embed_video: bool = True) -> None:
    import json
    import zipfile

    video = theme.find_video([])
    with zipfile.ZipFile(path + ".tmp", "w", zipfile.ZIP_DEFLATED) as z:
        items = []
        for i, it in enumerate(theme.items):
            d = {"type": it.type, "x": it.x, "y": it.y, "hidden": it.hidden,
                 "props": {k: _enc(v) for k, v in it.p.items()}}
            if it.name:
                d["name"] = it.name
            if it.image is not None:
                fn = f"assets/item{i:03d}.png"
                buf = io.BytesIO()
                it.image.save(buf, "PNG")
                z.writestr(fn, buf.getvalue())
                d["image"] = fn
            items.append(d)
        meta = {"format": FORMAT, "name": theme.name, "width": theme.width, "height": theme.height,
                "frame_rate": theme.frame_rate, "items": items}
        if video:
            vname = os.path.basename(video)
            if embed_video:
                z.write(video, f"video/{vname}", compress_type=zipfile.ZIP_STORED)
                meta["video"] = f"video/{vname}"
            else:
                meta["video_path"] = os.path.abspath(video)
        z.writestr("theme.json", json.dumps(meta, indent=1, ensure_ascii=False))
    os.replace(path + ".tmp", path)
    theme.path = path


def load_turzx(path: str) -> Theme:
    import hashlib
    import json
    import zipfile

    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("theme.json"))
        if meta.get("format") != FORMAT:
            raise ValueError(f"{path}: unknown format {meta.get('format')}")
        items = []
        for d in meta["items"]:
            typ = d["type"]
            schema = SCHEMA.get(typ, {})
            p = {k: _dec(schema.get(k, ("",))[0], v) for k, v in d.get("props", {}).items()}
            it = Item(typ, int(d.get("x", 0)), int(d.get("y", 0)), bool(d.get("hidden", False)),
                      d.get("name", ""), p)
            if d.get("image"):
                it.image = Image.open(io.BytesIO(z.read(d["image"]))).convert("RGBA")
            items.append(it)
        th = Theme(path, meta.get("name", ""), int(meta["width"]), int(meta["height"]), None, None,
                   int(meta.get("frame_rate", 24)), items)
        if meta.get("video"):
            st = os.stat(path)
            key = hashlib.sha1(f"{os.path.abspath(path)}{st.st_mtime}{st.st_size}".encode()).hexdigest()[:12]
            cache = os.path.expanduser(f"~/.cache/turzx/{key}")
            out = os.path.join(cache, os.path.basename(meta["video"]))
            if not os.path.isfile(out):
                os.makedirs(cache, exist_ok=True)
                with z.open(meta["video"]) as src, open(out + ".part", "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                os.replace(out + ".part", out)
            th.video_path, th.video_name = out, os.path.basename(out)
        elif meta.get("video_path"):
            th.video_path = meta["video_path"]
            th.video_name = os.path.basename(th.video_path)
    return th


def load_theme(path: str) -> Theme:
    """Load .turzx (own format) or import a Windows .turtheme."""
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head.startswith(b"PK"):
        return load_turzx(path)
    return import_turtheme(path)


# ---------------------------------------------------------------------------
# value formatting (port of 땯.cs / M_Data.cs)

_PERCENT = {"CPULOAD", "GPULOAD", "RAMLOAD", "GPURAMLOAD", "HDDUSED", "Volume"}
_TEMP = {"CPUTEMP", "GPUTEMP"}
_MEGA = {"CPUCLOCK", "GPUCLOCK", "RAM", "RAMVALID", "RAMTOTAL", "GPURAM", "GPUVALIDRAM"}
_GIGA = {"CPUCLOCK_G", "GPUCLOCK_G", "RAM_GB", "RAMVALID_GB", "RAMTOTAL_GB"}
_WATT = {"CPUPWR", "GPUPWR"}
_RPM = {"CPUFAN", "GPUFAN", "CASEFAN1", "CASEFAN2", "WATERPUMP", "SLCWATERPUMP"}
_VOLT = {"CPUVOLTAGE", "GPUVOLTAGE"}

_DAY_CN = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
_DAY_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_MON_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def unit_for(name: str) -> str:
    if name in _PERCENT or "DRVLOAD" in name:
        return "%"
    if name in _TEMP or "HDDTEMP" in name:
        return "°"
    if name in _MEGA:
        return "M"
    if name in _GIGA:
        return "G"
    if name in _WATT:
        return "W"
    if name in _RPM:
        return "R"
    if name in _VOLT:
        return "V"
    if name == "FPS":
        return "FPS"
    if name in ("UPSPEED", "DOWNDSPEED"):
        return "KB/s"
    return ""


def compute(it: Item, sensors, now: _dt.datetime | None = None) -> None:
    """Fill it.value / it.text / it.rate like the Windows app does."""
    name = it.data_name
    if not name:
        return
    if name == "StaticText":
        it.text = it.static_text or ""
        return
    now = now or _dt.datetime.now()
    sub = it.sub_name
    rate: float | None = None
    val: str
    if name == "TIME":
        fmts = {None: "%H:%M:%S", "": "%H:%M:%S", "h:m:s": "%H:%M:%S", "h:m": "%H:%M",
                "h_12": "%I", "h_24": "%H", "m": "%M", "s": "%S"}
        val = now.strftime(fmts.get(sub, "%H:%M:%S"))
        secs = now.hour * 3600 + now.minute * 60 + now.second
        rate = {"h:m": (now.hour * 60 + now.minute) / 1440, "h_12": (now.hour % 12) / 12,
                "h_24": now.hour / 24, "m": now.minute / 60, "s": now.second / 60}.get(sub, secs / 86400)
    elif name == "DATE":
        if sub == "Y":
            val = f"{now.year:04d}"
        elif sub == "M":
            val, rate = f"{now.month:02d}", now.month / 12
        elif sub == "M_en":
            val, rate = _MON_EN[now.month - 1], now.month / 12
        elif sub == "M_cn":
            val, rate = f"{now.month}月", now.month / 12
        elif sub == "D":
            val, rate = f"{now.day:02d}", now.day / 31
        else:
            val = now.strftime("%Y-%m-%d")
    elif name == "DAY":
        dow = (now.weekday() + 1) % 7  # .NET: Sunday = 0
        val = {"Day_en": _DAY_EN[dow], "Num_cn": _DAY_CN[dow][1], "Num": str(dow)}.get(sub, _DAY_CN[dow])
        rate = dow / 7
    elif name == "APM":
        val = "PM" if now.hour >= 12 else "AM"
    elif name in ("CPUMODEL", "GPUMODEL", "RAMMODEL", "Weather"):
        v = sensors.get(name)
        val = str(v or "").replace("GeForce", "").strip() if name == "GPUMODEL" else str(v or "")
    else:
        base = name[:-2] if name.endswith("_G") else name
        v = sensors.get(base if name.endswith("CLOCK_G") else name, sub)
        if v is None:
            v = 0.0
        v = float(v)
        if it.fahrenheit and (name in _TEMP or "HDDTEMP" in name):
            v = 32 + 1.8 * v
        if name.endswith("CLOCK_G"):
            val = f"{v / 1000:.2f}"
        elif name in _VOLT:
            val = f"{v:.2f}"
        elif name in ("RAM_GB", "RAMVALID_GB", "RAMTOTAL_GB"):
            val = f"{v:.1f}"
        else:
            val = str(int(v))
        if name in ("CPUCLOCK", "GPUCLOCK"):
            rate = v / 6000
        elif "FAN" in name or "WATERPUMP" in name:
            rate = v / 8000
        elif "PWR" in name:
            rate = v / 500
        elif name == "RAMVALID":
            tot = sensors.get("RAMTOTAL") or 0
            rate = v / tot if tot else 0
        elif name in ("RAM", "GPURAM"):
            tot = sensors.get("RAMTOTAL") or 0
            if name == "GPURAM":
                free = sensors.get("GPUVALIDRAM") or 0
                tot = v + float(free)
            rate = v / tot if tot else 0
        else:
            try:
                rate = float(val) / 100
            except ValueError:
                rate = None
    unit = unit_for(name) if it.show_unit else ""
    if name in ("UPSPEED", "DOWNDSPEED") and it.show_unit:
        iv = int(float(val or 0))
        if iv > 1024:
            val, unit = str(iv // 1024), "MB/s"
    it.value = val
    it.text = val + unit
    if rate is not None:
        it.rate = max(0.0, min(rate, 1.0))
