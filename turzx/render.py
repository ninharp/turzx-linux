"""Theme renderer: draws widgets like UsbMonitorL (GDI+) does.

Conventions taken from the decompiled app (see docs/PROTOCOL.md):
* canvas = theme.width x theme.height, transparent; items drawn in list order;
  Hidden items are skipped. 'Animation' (video placeholder) is skipped while a
  video plays behind the frame; otherwise its still image is the background.
* GDI+ fonts are in points on a 96-dpi canvas -> px = pt * 4/3.
* DrawString adds ~1/6 em horizontal padding; posY is the top of the line box,
  the baseline sits at the font's OS/2 winAscent.

After render(), `Renderer.bboxes` maps id(item) -> (x0, y0, x1, y1) in canvas
coordinates (used by the editor for hit testing).
"""
from __future__ import annotations

import logging
import math

from PIL import Image, ImageChops, ImageDraw

from .fonts import FontIndex
from .theme import Item, Theme, compute

log = logging.getLogger(__name__)
SS = 3  # supersampling for vector shapes


def _lerp(c1, c2, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _col(v, default=(128, 128, 128, 255)):
    if isinstance(v, (tuple, list)) and len(v) == 4:
        return tuple(int(c) for c in v)
    return default


class _Layer:
    """Supersampled RGBA layer covering a bounding box of the canvas."""

    def __init__(self, x0: float, y0: float, x1: float, y1: float, pad: int = 4):
        self.ox = int(math.floor(x0)) - pad
        self.oy = int(math.floor(y0)) - pad
        self.w = max(1, int(math.ceil(x1)) + pad - self.ox)
        self.h = max(1, int(math.ceil(y1)) + pad - self.oy)
        self.img = Image.new("RGBA", (self.w * SS, self.h * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    def p(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.ox) * SS, (y - self.oy) * SS)

    def box(self, x0, y0, x1, y1):
        a, b = self.p(x0, y0)
        c, d = self.p(x1, y1)
        return [min(a, c), min(b, d), max(a, c), max(b, d)]

    def paste_into(self, canvas: Image.Image) -> None:
        small = self.img.resize((self.w, self.h), Image.LANCZOS)
        _composite_clip(canvas, small, self.ox, self.oy)


def _composite_clip(canvas: Image.Image, img: Image.Image, x: int, y: int) -> None:
    cx0, cy0 = max(0, x), max(0, y)
    cx1, cy1 = min(canvas.width, x + img.width), min(canvas.height, y + img.height)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    if (cx0, cy0, cx1, cy1) == (x, y, x + img.width, y + img.height):
        canvas.alpha_composite(img, (x, y))
        return
    canvas.alpha_composite(img.crop((cx0 - x, cy0 - y, cx1 - x, cy1 - y)), (cx0, cy0))


def _gradient(size, c1, c2, mode: int) -> Image.Image:
    """LinearGradientMode: 0 horizontal, 1 vertical, 2 forward diag, 3 backward diag."""
    import numpy as np

    w, h = max(1, int(size[0])), max(1, int(size[1]))
    xs = np.linspace(0, 1, w)[None, :]
    ys = np.linspace(0, 1, h)[:, None]
    if mode == 0:
        t = np.broadcast_to(xs, (h, w))
    elif mode == 1:
        t = np.broadcast_to(ys, (h, w))
    elif mode == 2:
        t = (xs + ys) / 2
    else:
        t = (1 - xs + ys) / 2
    a = np.array(c1, dtype=np.float32)
    b = np.array(c2, dtype=np.float32)
    arr = a + (b - a) * t[..., None]
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGBA")


class Renderer:
    def __init__(self, theme: Theme, sensors, fonts: FontIndex | None = None):
        self.theme = theme
        self.sensors = sensors
        self.fonts = fonts or FontIndex()
        self.bboxes: dict[int, tuple[float, float, float, float]] = {}
        # False = re-draw only (e.g. while editing): chart histories don't advance
        self.update_values = True
        # True while the theme video is shown behind the rendered frame
        self.video_background = False

    # ------------------------------------------------------------------
    def render(self, background: Image.Image | None = None, include_hidden: bool = False) -> Image.Image:
        """Return the theme frame (theme orientation, RGBA)."""
        t = self.theme
        canvas = Image.new("RGBA", (t.width, t.height), (0, 0, 0, 0))
        if background is not None:
            canvas.alpha_composite(background.convert("RGBA").resize((t.width, t.height)))
        self.bboxes = {}
        for it in t.items:
            if it.hidden and not include_hidden:
                continue
            if it.type == "Animation":
                if self.video_background:
                    continue
                # video missing / not playing: show the placeholder's still frame
                bb = self._draw_Image(canvas, it)
                if bb:
                    self.bboxes[id(it)] = bb
                continue
            compute(it, self.sensors)  # cheap: uses the sensors' cached values
            fn = getattr(self, "_draw_" + it.type, None)
            if not fn:
                continue
            try:
                bb = fn(canvas, it)
                if bb:
                    self.bboxes[id(it)] = bb
            except Exception as e:  # noqa: BLE001 - one broken widget must not kill the frame
                log.debug("widget %s failed: %s", it.type, e)
        return canvas

    # ------------------------------------------------------------------ image
    def _draw_Image(self, canvas: Image.Image, it: Item):
        im = it.image
        if im is None:
            return (it.x, it.y, it.x + 40, it.y + 40)
        _composite_clip(canvas, im, it.x, it.y)
        return (it.x, it.y, it.x + im.width, it.y + im.height)

    # ------------------------------------------------------------------ text
    def _draw_text(self, canvas: Image.Image, it: Item):
        p = it.p
        text = it.text or ""
        if not text:
            return (it.x, it.y, it.x + 20, it.y + 20)
        size_pt = float(p.get("font_size", 24) or 24)
        px = max(1, round(size_pt * 96 / 72))
        font, fake_bold = self.fonts.get(p.get("font_name"), px, bool(p.get("bold", False)))
        col = _col(p.get("color"))
        align = int(p.get("align", 0) or 0)
        interval = float(p.get("interval", 0.0) or 0.0)
        gr_dir = int(p.get("gr_dir", 0) or 0)
        gr_col = _col(p.get("gr_color"), col)
        pad = px / 6.0
        stroke = max(1, px // 30) if fake_bold else 0
        ascent, descent = font.gdi_ascent, font.gdi_descent
        line_h = int(math.ceil(ascent + descent))

        if interval:
            widths = [font.getlength(ch) + 2 * pad for ch in text]
            total = sum(w + interval for w in widths) - interval
            if align == 1:
                x = it.x - total / 2
            elif align == 2:
                x = it.x - total + widths[-1]
            else:
                x = it.x
            positions = []
            for ch, w in zip(text, widths):
                positions.append((x + pad, ch))
                x += w + interval
            x0 = positions[0][0]
            x1 = positions[-1][0] + font.getlength(positions[-1][1])
        else:
            tw = font.getlength(text)
            if align == 1:
                x0 = it.x - tw / 2
            elif align == 2:
                x0 = it.x - tw - pad
            else:
                x0 = it.x + pad
            positions = [(x0, text)]
            x1 = x0 + tw

        ox, oy = int(math.floor(x0)) - 4 - stroke, it.y - 4
        w = int(math.ceil(x1 - x0)) + 8 + 2 * stroke
        h = line_h + 8 + 2 * stroke
        if w <= 0 or h <= 0:
            return None
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        for px_, s in positions:
            md.text((px_ - ox, it.y - oy + ascent), s, font=font, fill=255, anchor="ls",
                    stroke_width=stroke, stroke_fill=255)
        if gr_dir and not interval:
            fill = _gradient((w, h), col, gr_col, gr_dir - 1)
        else:
            fill = Image.new("RGBA", (w, h), col)
        fill.putalpha(ImageChops.multiply(fill.getchannel("A"), mask))
        _composite_clip(canvas, fill, ox, oy)
        return (x0, it.y, x1, it.y + line_h)

    _draw_Data = _draw_text
    _draw_Text = _draw_text

    # ------------------------------------------------------------------ bars
    def _draw_StatuBar(self, canvas: Image.Image, it: Item):
        p = it.p
        width = int(p.get("width", 100) or 0)
        h = int(p.get("height", 10) or 0)
        direction = int(p.get("direction", 0) or 0)
        radius = int(p.get("radius", 0) or 0)
        lw = int(p.get("lineWidth", 0) or 0)
        tr_back = bool(p.get("trBack", False))
        fill_back = bool(p.get("fillBack", True))
        use_grad = bool(p.get("useGradient", False))
        use_sub = bool(p.get("useSubsection", False))
        front = _col(p.get("FrontColor"))
        back = _col(p.get("BackColor"))
        grad = _col(p.get("GradientColor"))
        block = int(p.get("SplitBlockWidth", 0) or 0) or 10
        gap = int(p.get("SplitBlankWidth", 0) or 0) or 2
        rate = 1 - it.rate if p.get("revert") else it.rate
        L = int(width * rate)
        if radius > 0 and 0 < rate < 0.08:
            L = int(width * 0.08)
        x, y = it.x, it.y
        vertical = direction in (2, 3)
        bw, bh = (h, width) if vertical else (width, h)
        lay = _Layer(x - lw - 2, y - lw - 2, x + bw + lw + 2, y + bh + lw + 2)
        d = lay.d

        def rect(x0, y0, w_, h_, fill=None, outline=None, wid=1, rad=0):
            b = lay.box(x0, y0, x0 + w_, y0 + h_)
            if rad > 0:
                d.rounded_rectangle(b, radius=rad * SS, fill=fill, outline=outline, width=wid * SS)
            else:
                d.rectangle(b, fill=fill, outline=outline, width=wid * SS)

        def blocks(x0, y0, total, thick, fill=None, outline=None, reverse=False):
            pos = 0
            while pos < total:
                n = min(block, total - pos)
                if vertical:
                    yy = (y0 + total - pos - n) if reverse else (y0 + pos)
                    rect(x0, yy, thick, n, fill=fill, outline=outline)
                else:
                    xx = (x0 + total - pos - n) if reverse else (x0 + pos)
                    rect(xx, y0, n, thick, fill=fill, outline=outline)
                pos += block + gap

        rev = direction in (1, 3)
        if not tr_back:
            if use_sub:
                blocks(x, y, width, h, fill=back if fill_back else None,
                       outline=None if fill_back else back, reverse=rev)
            elif fill_back:
                rect(x - lw, y - lw, bw + 2 * lw, bh + 2 * lw, fill=back, rad=radius)
        if L > 0:
            if direction == 0:
                fr = (x, y, L, h)
            elif direction == 1:
                fr = (x + width - L, y, L, h)
            elif direction == 2:
                fr = (x, y, h, L)
            else:
                fr = (x, y + width - L, h, L)
            if use_grad:
                gw, gh = int(fr[2] * SS), int(fr[3] * SS)
                g = _gradient((gw, gh), front[:3] + (255,), grad, 1 if direction <= 1 else 0)
                mask = Image.new("L", (gw, gh), 0)
                if radius > 0:
                    ImageDraw.Draw(mask).rounded_rectangle([0, 0, gw - 1, gh - 1], radius=radius * SS, fill=255)
                else:
                    mask.paste(255, (0, 0, gw, gh))
                px0, py0 = lay.p(fr[0], fr[1])
                lay.img.paste(g, (int(px0), int(py0)), mask)
            elif use_sub:
                if vertical:
                    blocks(fr[0], fr[1], fr[3], h, fill=front, reverse=rev)
                else:
                    blocks(fr[0], fr[1], fr[2], h, fill=front, reverse=rev)
            else:
                rect(*fr, fill=front, rad=radius)
        if not tr_back and not fill_back and lw > 0:
            rect(x, y, bw, bh, outline=back[:3] + (255,), wid=2 * lw, rad=radius)
        lay.paste_into(canvas)
        return (x - lw, y - lw, x + bw + lw, y + bh + lw)

    def _draw_ArchBar(self, canvas: Image.Image, it: Item):
        p = it.p
        dia = int(p.get("diameter", 100) or 100)
        aw = int(p.get("archWidth", 10) or 1)
        pw = aw + 1 if aw % 2 == 0 else aw
        total = int(p.get("totalAngel", 360) or 360)
        start = int((p.get("startPer", 0) or 0) * 0.01 * 360) - 90
        revert = bool(p.get("revert", False))
        v = 1 - it.rate if revert else it.rate
        sweep = int(total * v)
        s0 = start + (total - sweep if revert else 0)
        round_cap = bool(p.get("round", False))
        use_block = bool(p.get("useBlock", False))
        use_grad = bool(p.get("useGradient", False))
        tr_back = bool(p.get("trBack", False))
        front = _col(p.get("FrontColor"))
        back = _col(p.get("BackColor"))
        grad = _col(p.get("GradientColor"))
        blk = int(p.get("SplitBlockWidth", 0) or 0) or 15
        gap = int(p.get("SplitBlankWidth", 0) or 0) or 4
        x, y = it.x, it.y
        lay = _Layer(x - pw, y - pw, x + dia + pw, y + dia + pw)
        cx, cy, rad = x + dia / 2, y + dia / 2, dia / 2

        def arc(a0: float, span: float, col):
            if span <= 0:
                return
            b = lay.box(cx - rad - pw / 2, cy - rad - pw / 2, cx + rad + pw / 2, cy + rad + pw / 2)
            lay.d.arc(b, a0, a0 + span, fill=col, width=max(1, int(pw * SS)))
            if round_cap:
                for a in (a0, a0 + span):
                    px_, py_ = lay.p(cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a)))
                    rr = pw * SS / 2
                    lay.d.ellipse([px_ - rr, py_ - rr, px_ + rr, py_ + rr], fill=col)

        if not tr_back:
            if use_block:
                k = 0
                while blk + k * (blk + gap) < total:
                    arc(start + k * (blk + gap), blk, back)
                    k += 1
            else:
                arc(start, total, back)
        if sweep > 0:
            if use_block:
                a = 0
                while a < sweep:
                    col = _lerp(front, grad, a / sweep) if use_grad else front
                    arc(s0 + a, min(blk, sweep - a), col)
                    a += blk + gap
            elif use_grad:
                steps = max(2, sweep // 2)
                for i in range(steps):
                    a = s0 + sweep * i / steps
                    arc(a, sweep / steps + 0.6, _lerp(front, grad, i / (steps - 1)))
            else:
                arc(s0, sweep, front)
        if p.get("HasRingBorder"):
            for dd in (pw / 2, -pw / 2):
                lay.d.ellipse(lay.box(x - dd, y - dd, x + dia + dd, y + dia + dd), outline=front, width=SS)
        lay.paste_into(canvas)
        return (x - pw / 2, y - pw / 2, x + dia + pw / 2, y + dia + pw / 2)

    # ------------------------------------------------------------------ clock
    def _draw_Clock(self, canvas: Image.Image, it: Item):
        p = it.p
        cx, cy = int(p.get("centerX", 0) or 0), int(p.get("centerY", 0) or 0)
        im = it.image
        if im is None:
            return (cx - 10, cy - 10, cx + 10, cy + 10)
        angle = float(p.get("angle", 0) or 0)
        sweep = float(p.get("endAngle", 360) or 0)
        offset = float(p.get("offset", 0) or 0)
        aoff = float(p.get("AngleOffset", 0) or 0)
        delta = (it.rate - offset) * sweep
        theta = (angle - delta if p.get("revert") else angle + delta) - aoff
        # needle image placed at (posX, posY) relative to the pivot, rotated about the pivot
        reach = int(math.hypot(abs(it.x) + im.width, abs(it.y) + im.height)) + 2
        tile = Image.new("RGBA", (2 * reach, 2 * reach), (0, 0, 0, 0))
        _composite_clip(tile, im, reach + it.x, reach + it.y)
        rot = tile.rotate(-theta, resample=Image.BICUBIC, center=(reach, reach))
        _composite_clip(canvas, rot, cx - reach, cy - reach)
        bb = rot.getbbox()
        if bb:
            return (cx - reach + bb[0], cy - reach + bb[1], cx - reach + bb[2], cy - reach + bb[3])
        return (cx - 10, cy - 10, cx + 10, cy + 10)

    # ------------------------------------------------------------------ chart
    def _draw_Chart(self, canvas: Image.Image, it: Item):
        p = it.p
        w = int(p.get("width", 200) or 200)
        h = int(p.get("height", 50) or 50)
        col_w = max(1, int(p.get("columnWidth", 5) or 5))
        maxv = float(p.get("maxValue", 100) or 100)
        if it.data_name in ("RAM", "RAMVALID"):
            maxv = float(self.sensors.get("RAMTOTAL") or maxv)
        elif it.data_name and it.data_name.endswith("_GB"):
            maxv = float(self.sensors.get("RAMTOTAL") or maxv * 1024) / 1024
        coef = h / maxv if maxv else 0
        try:
            v = float(it.value)
        except ValueError:
            v = 0.0
        n_max = w // col_w + 1
        hist = it.history
        if self.update_values:
            hist.append(v)
        while len(hist) > n_max:
            hist.popleft()
        vals = list(hist)
        n = len(vals)
        roll = bool(p.get("rollDirection", False))
        pts = []
        for i, val in enumerate(vals):
            px_ = it.x + w - col_w * (n - 1 - i) if roll else it.x + col_w * (n - 1 - i)
            py_ = max(it.y, it.y + h - val * coef)
            pts.append((px_, py_))
        bottom = it.y + h
        lay = _Layer(it.x - 4, it.y - 4, it.x + w + 4, it.y + h + 4)
        lw = int(p.get("lineWidth", 1) or 0)
        if n >= 2:
            if lw > 0:
                lay.d.line([lay.p(*q) for q in pts], fill=_col(p.get("LineColor")), width=lw * SS, joint="curve")
            fill_c = _col(p.get("FillColor"))
            if fill_c[3]:
                if roll:
                    poly = [(pts[0][0], bottom)] + pts + [(it.x + w, bottom)]
                else:
                    poly = [(it.x, bottom)] + pts[::-1] + [(pts[0][0], bottom)]
                lay.d.polygon([lay.p(*q) for q in poly], fill=fill_c)
        bwid = int(p.get("borderWidth", 0) or 0)
        if bwid > 0:
            lay.d.rectangle(lay.box(it.x, it.y, it.x + w, it.y + h), outline=_col(p.get("BorderColor")),
                            width=bwid * SS)
        lay.paste_into(canvas)
        return (it.x, it.y, it.x + w, it.y + h)


def to_device(img: Image.Image, dev_w: int, dev_h: int, rotation: int | None = None) -> Image.Image:
    """Rotate a theme frame for the portrait device (app rule: landscape -> 90° clockwise)."""
    if rotation is None:
        rotation = 90 if img.width > img.height and dev_w < dev_h else 0
    if rotation == 90:
        img = img.transpose(Image.ROTATE_270)  # PIL rotates counter-clockwise
    elif rotation == 180:
        img = img.transpose(Image.ROTATE_180)
    elif rotation == 270:
        img = img.transpose(Image.ROTATE_90)
    if img.size != (dev_w, dev_h):
        img = img.resize((dev_w, dev_h), Image.LANCZOS)
    return img
