"""Snapshot / restore of top-level window geometry (EWMH), used as a safety net
around X screen changes made for the virtual monitor."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class WindowSnapshot:
    def __init__(self):
        self.entries: list[tuple[int, int, int, int, int, bool, bool]] = []
        try:
            from Xlib import X, display
        except ImportError:
            log.warning("python-xlib missing - window positions are not saved")
            self.d = None
            return
        self.X = X
        self.d = display.Display()
        self.root = self.d.screen().root
        self.A = {n: self.d.intern_atom(n) for n in (
            "_NET_CLIENT_LIST", "_NET_WM_STATE", "_NET_WM_STATE_MAXIMIZED_VERT",
            "_NET_WM_STATE_MAXIMIZED_HORZ", "_NET_MOVERESIZE_WINDOW", "_NET_FRAME_EXTENTS",
            "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG")}
        self._take()

    def _clients(self):
        p = self.root.get_full_property(self.A["_NET_CLIENT_LIST"], self.X.AnyPropertyType)
        return list(p.value) if p else []

    def _is_normal(self, w) -> bool:
        p = w.get_full_property(self.A["_NET_WM_WINDOW_TYPE"], self.X.AnyPropertyType)
        if not p:
            return True
        return any(t in (self.A["_NET_WM_WINDOW_TYPE_NORMAL"], self.A["_NET_WM_WINDOW_TYPE_DIALOG"])
                   for t in p.value)

    def _geom(self, w):
        g = w.get_geometry()
        t = w.translate_coords(self.root, 0, 0)
        st = w.get_full_property(self.A["_NET_WM_STATE"], self.X.AnyPropertyType)
        states = set(st.value) if st else set()
        return (-t.x, -t.y, g.width, g.height,
                self.A["_NET_WM_STATE_MAXIMIZED_VERT"] in states,
                self.A["_NET_WM_STATE_MAXIMIZED_HORZ"] in states)

    def _take(self) -> None:
        for wid in self._clients():
            try:
                w = self.d.create_resource_object("window", wid)
                if self._is_normal(w):
                    self.entries.append((wid, *self._geom(w)))
            except Exception:  # noqa: BLE001 - window vanished
                continue
        log.debug("saved %d window positions", len(self.entries))

    def _send(self, w, atom, data):
        from Xlib.protocol import event

        ev = event.ClientMessage(window=w, client_type=atom, data=(32, (data + [0] * 5)[:5]))
        self.root.send_event(ev, event_mask=self.X.SubstructureRedirectMask | self.X.SubstructureNotifyMask)

    def restore(self, rescue: tuple[int, int, int, int] | None = None,
                target: tuple[int, int] | None = None) -> int:
        """Move windows back to their snapshot geometry. With `rescue`, windows
        that were not in the snapshot but now lie inside that area are moved
        to `target`."""
        if self.d is None:
            return 0
        moved = 0
        if rescue and target:
            known = {e[0] for e in self.entries}
            rx, ry, rw, rh = rescue
            for wid in self._clients():
                if wid in known:
                    continue
                try:
                    w = self.d.create_resource_object("window", wid)
                    if not self._is_normal(w):
                        continue
                    x, y, wd, ht, *_ = self._geom(w)
                except Exception:  # noqa: BLE001
                    continue
                cx, cy = x + wd // 2, y + ht // 2
                if rx <= cx < rx + rw and ry <= cy < ry + rh:
                    self._send(w, self.A["_NET_MOVERESIZE_WINDOW"], [10 | (0x3 << 8), target[0], target[1], 0, 0])
                    moved += 1
        mv, mh = self.A["_NET_WM_STATE_MAXIMIZED_VERT"], self.A["_NET_WM_STATE_MAXIMIZED_HORZ"]
        for wid, x, y, wd, ht, max_v, max_h in self.entries:
            try:
                w = self.d.create_resource_object("window", wid)
                cx, cy, cw, ch, cmv, cmh = self._geom(w)
            except Exception:  # noqa: BLE001
                continue
            if (cx, cy, cw, ch, cmv, cmh) == (x, y, wd, ht, max_v, max_h):
                continue
            if rescue:  # only bring back windows that ended up in the virtual area
                rx, ry, rw, rh = rescue
                mx, my = cx + cw // 2, cy + ch // 2
                if not (rx <= mx < rx + rw and ry <= my < ry + rh):
                    continue
            if cmv or cmh:  # un-maximize so the WM accepts the move
                self._send(w, self.A["_NET_WM_STATE"], [0, mv, mh, 1])
            # gravity static (10) + x,y,w,h flags
            self._send(w, self.A["_NET_MOVERESIZE_WINDOW"], [10 | (0xF << 8), x, y, wd, ht])
            if max_v or max_h:
                self._send(w, self.A["_NET_WM_STATE"], [1, mv if max_v else 0, mh if max_h else 0, 1])
            moved += 1
        self.d.flush()
        if moved:
            log.info("moved %d windows back to their previous position", moved)
        return moved


def list_windows() -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """(window id, title, (x, y, w, h)) of all managed top-level windows."""
    from Xlib import X, display

    d = display.Display()
    root = d.screen().root
    cl = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
    name_a, utf8 = d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING")
    out = []
    for wid in (cl.value if cl else []):
        try:
            w = d.create_resource_object("window", wid)
            n = w.get_full_property(name_a, utf8)
            title = n.value.decode(errors="replace") if n else (w.get_wm_name() or "")
            g = w.get_geometry()
            t = w.translate_coords(root, 0, 0)
            out.append((wid, str(title), (-t.x, -t.y, g.width, g.height)))
        except Exception:  # noqa: BLE001
            continue
    return out


def pick_window() -> int | None:
    """Let the user click a window (crosshair cursor); returns its client id."""
    from Xlib import X, Xcursorfont, display

    d = display.Display()
    root = d.screen().root
    font = d.open_font("cursor")
    cur = font.create_glyph_cursor(font, Xcursorfont.crosshair, Xcursorfont.crosshair + 1,
                                   (65535, 65535, 65535), (0, 0, 0))
    if root.grab_pointer(False, X.ButtonPressMask, X.GrabModeAsync, X.GrabModeAsync,
                         X.NONE, cur, X.CurrentTime) != X.GrabSuccess:
        return None
    try:
        while True:
            ev = d.next_event()
            if ev.type == X.ButtonPress:
                child = ev.child
                break
    finally:
        d.ungrab_pointer(X.CurrentTime)
        d.flush()
    if not child:
        return None
    clients = {w for w, _, _ in list_windows()}
    # the click hits the WM frame; search its subtree for a managed client
    stack = [child]
    while stack:
        w = stack.pop()
        if w.id in clients:
            return w.id
        try:
            stack.extend(w.query_tree().children)
        except Exception:  # noqa: BLE001
            pass
    return child.id
