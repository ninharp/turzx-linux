"""Theme editor (PySide6) - Linux counterpart of the TURZX ThemeEditForm.

Opens .turzx (own format) and imports Windows .turtheme files; saves .turzx.
The preview is produced by the same Renderer that drives the display, so what
you see is exactly what the panel shows. Optional live mirroring to the device.
"""
from __future__ import annotations

import copy
import io
import logging
import os
import subprocess
import sys
import threading
import time

from PIL import Image
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QImage, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .control import load_settings, save_setting
from .fonts import FontIndex
from .render import Renderer, to_device
from .sensors import Sensors
from .theme import (DATA_LABELS, DATA_NAMES, SCHEMA, SUB_NAMES, TYPE_LABELS, Item, Theme, load_theme,
                    new_item, new_theme, save_turzx)
from . import __version__

log = logging.getLogger(__name__)
THEME_DIR = os.path.expanduser("~/.local/share/turzx/themes")
ADDABLE = ["Data", "Text", "Image", "StatuBar", "ArchBar", "Clock", "Chart"]
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif)"


def last_dir(kind: str, fallback: str = "") -> str:
    """Folder the last file dialog of this kind (theme, video, image, export) used."""
    d = load_settings().get("last_dirs", {}).get(kind)
    if isinstance(d, str) and os.path.isdir(d):
        return d
    return fallback if fallback and os.path.isdir(fallback) else os.path.expanduser("~")


def remember_dir(kind: str, path: str) -> None:
    dirs = load_settings().get("last_dirs")
    dirs = dirs if isinstance(dirs, dict) else {}
    dirs[kind] = os.path.dirname(os.path.abspath(path))
    try:
        save_setting("last_dirs", dirs)
    except OSError as e:
        log.warning("cannot save the last folder: %s", e)


def open_file(parent: QWidget, kind: str, title: str, filt: str, fallback: str = "") -> str:
    """File open dialog that starts in, and remembers, the last folder of this kind."""
    path, _ = QFileDialog.getOpenFileName(parent, title, last_dir(kind, fallback), filt)
    if path:
        remember_dir(kind, path)
    return path


def save_file(parent: QWidget, kind: str, title: str, name: str, filt: str, fallback: str = "") -> str:
    path, _ = QFileDialog.getSaveFileName(parent, title, os.path.join(last_dir(kind, fallback), name), filt)
    if path:
        remember_dir(kind, path)
    return path


def pil_to_qimage(im: Image.Image) -> QImage:
    im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    q = QImage(data, im.width, im.height, im.width * 4, QImage.Format_RGBA8888)
    return q.copy()  # detach from the Python buffer


def qcolor(c) -> QColor:
    return QColor(int(c[0]), int(c[1]), int(c[2]), int(c[3]))


# --------------------------------------------------------------------------- canvas
class Canvas(QGraphicsView):
    itemClicked = Signal(object)          # Item or None
    itemMoved = Signal(object, int, int)  # Item, dx, dy (during drag)
    dragFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setBackgroundBrush(QBrush(QColor(40, 40, 40)))
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.bg_item = QGraphicsPixmapItem()
        self.frame_item = QGraphicsPixmapItem()
        self.scene_.addItem(self.bg_item)
        self.scene_.addItem(self.frame_item)
        pen = QPen(QColor(0, 170, 255), 0)
        pen.setCosmetic(True)
        pen.setWidth(2)
        self.sel_rect = QGraphicsRectItem()
        self.sel_rect.setPen(pen)
        self.sel_rect.setZValue(10)
        self.scene_.addItem(self.sel_rect)
        self.hits: list[tuple[Item, tuple]] = []
        self._drag: tuple[Item, QPointF] | None = None
        self._fitted = False

    def set_frame(self, frame: Image.Image, background: Image.Image | None) -> None:
        self.frame_item.setPixmap(QPixmap.fromImage(pil_to_qimage(frame)))
        if background is not None:
            self.bg_item.setPixmap(QPixmap.fromImage(pil_to_qimage(background)))
            self.bg_item.setVisible(True)
        else:
            black = Image.new("RGBA", frame.size, (0, 0, 0, 255))
            self.bg_item.setPixmap(QPixmap.fromImage(pil_to_qimage(black)))
        self.scene_.setSceneRect(QRectF(0, 0, frame.width, frame.height))
        if not self._fitted:
            self.fit()

    def fit(self) -> None:
        self.fitInView(self.scene_.sceneRect(), Qt.KeepAspectRatio)
        self._fitted = True

    def show_selection(self, bbox) -> None:
        if bbox:
            x0, y0, x1, y1 = bbox
            self.sel_rect.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            self.sel_rect.setVisible(True)
        else:
            self.sel_rect.setVisible(False)

    def hit(self, pos: QPointF) -> Item | None:
        for it, (x0, y0, x1, y1) in reversed(self.hits):
            if x0 <= pos.x() <= x1 and y0 <= pos.y() <= y1:
                return it
        return None

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
        else:
            super().wheelEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._fitted:
            self.fit()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            pos = self.mapToScene(ev.position().toPoint())
            it = self.hit(pos)
            self.itemClicked.emit(it)
            if it is not None and not it.hidden:
                self._drag = (it, pos)
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag:
            it, start = self._drag
            pos = self.mapToScene(ev.position().toPoint())
            dx, dy = int(round(pos.x() - start.x())), int(round(pos.y() - start.y()))
            if dx or dy:
                self.itemMoved.emit(it, dx, dy)
                self._drag = (it, QPointF(start.x() + dx, start.y() + dy))
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._drag:
            self._drag = None
            self.dragFinished.emit()
            return
        super().mouseReleaseEvent(ev)


# --------------------------------------------------------------------------- property panel
class PropertyPanel(QScrollArea):
    changed = Signal(str)  # property key

    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        self.setWidgetResizable(True)
        self._fonts: list[str] | None = None
        self.item: Item | None = None
        self.build(None)

    def font_families(self) -> list[str]:
        if self._fonts is None:
            self._fonts = self.editor.fonts.family_list()
        return self._fonts

    def build(self, item: Item | None) -> None:
        self.item = item
        w = QWidget()
        form = QFormLayout(w)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        if item is None:
            self._build_theme(form)
        else:
            self._build_item(form, item)
        old = self.takeWidget()
        if old is not None:  # delete later: one of its widgets may be emitting a signal
            old.hide()
            old.deleteLater()
        self.setWidget(w)

    # -- theme level ------------------------------------------------------
    def _build_theme(self, form: QFormLayout) -> None:
        t = self.editor.theme
        form.addRow(QLabel("<b>Theme</b>"))
        name = QLineEdit(t.name)
        name.editingFinished.connect(lambda: self._set_theme("name", name.text()))
        form.addRow("Name", name)
        form.addRow("Size", QLabel(f"{t.width} x {t.height} ({'landscape' if t.landscape else 'portrait'})"))
        vid = t.find_video(self.editor.video_dirs)
        if vid:
            vtext = os.path.basename(vid)
        elif t.video_name:
            vtext = f"{t.video_name} - not found, still image shown"
        else:
            vtext = "(none)"
        vlabel = QLabel(vtext)
        vlabel.setWordWrap(True)
        form.addRow("Background video", vlabel)
        row = QHBoxLayout()
        b1 = QPushButton("Choose video…")
        b1.clicked.connect(self.editor.choose_video)
        b2 = QPushButton("Remove")
        b2.clicked.connect(self.editor.remove_video)
        row.addWidget(b1)
        row.addWidget(b2)
        form.addRow(row)
        fps = QSpinBox()
        fps.setRange(1, 30)
        fps.setValue(min(30, t.frame_rate or 24))
        fps.valueChanged.connect(lambda v: self._set_theme("frame_rate", v))
        form.addRow("Video FPS", fps)
        bg = QPushButton("Set background image…")
        bg.clicked.connect(self.editor.set_background)
        form.addRow(bg)
        form.addRow(QLabel("<i>Select a widget in the preview or the list to edit it.</i>"))

    def _set_theme(self, key: str, value) -> None:
        self.editor.push_undo()
        setattr(self.editor.theme, key, value)
        self.changed.emit(key)

    # -- item level -------------------------------------------------------
    def _build_item(self, form: QFormLayout, it: Item) -> None:
        form.addRow(QLabel(f"<b>{TYPE_LABELS.get(it.type, it.type)}</b>"))
        name = QLineEdit(it.name)
        name.setPlaceholderText(it.label)
        name.editingFinished.connect(lambda: self._set_attr("name", name.text()))
        form.addRow("Label", name)
        for attr, label in (("x", "X"), ("y", "Y")):
            sb = QSpinBox()
            sb.setRange(-5000, 5000)
            sb.setValue(getattr(it, attr))
            sb.valueChanged.connect(lambda v, a=attr: self._set_attr(a, v))
            form.addRow(label + (" (needle offset)" if it.type == "Clock" else ""), sb)
        hid = QCheckBox("hidden")
        hid.setChecked(it.hidden)
        hid.toggled.connect(lambda v: self._set_attr("hidden", v))
        form.addRow("", hid)
        schema = SCHEMA.get(it.type, {})
        for key, spec in schema.items():
            kind, default, label = spec[0], spec[1], spec[2]
            choices = spec[3] if len(spec) > 3 else None
            val = it.p.get(key, default)
            wdg = self._editor_for(it, key, kind, val, choices)
            if wdg is not None:
                form.addRow(label, wdg)

    def _set_attr(self, attr: str, value) -> None:
        if self.item is None or getattr(self.item, attr) == value:
            return
        self.editor.push_undo()
        setattr(self.item, attr, value)
        self.changed.emit(attr)

    def _set(self, key: str, value) -> None:
        if self.item is None or self.item.p.get(key) == value:
            return
        self.editor.push_undo()
        self.item.p[key] = value
        self.changed.emit(key)
        if key == "data_name":
            # sub value choices depend on the sensor; rebuild after this signal
            # returns, the emitting combo box must not be deleted inside its slot
            it = self.item
            QTimer.singleShot(0, lambda: self.item is it and self.build(it))

    def _editor_for(self, it: Item, key: str, kind: str, val, choices):
        if kind == "int":
            w = QSpinBox()
            w.setRange(-5000, 10000)
            w.setValue(int(val or 0))
            w.valueChanged.connect(lambda v: self._set(key, v))
            return w
        if kind == "float":
            w = QDoubleSpinBox()
            w.setRange(-10000, 100000)
            w.setDecimals(2)
            w.setValue(float(val or 0))
            w.valueChanged.connect(lambda v: self._set(key, v))
            return w
        if kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(val))
            w.toggled.connect(lambda v: self._set(key, v))
            return w
        if kind == "text":
            w = QLineEdit(str(val or ""))
            w.textChanged.connect(lambda v: self._set(key, v))
            return w
        if kind == "color":
            b = QPushButton()
            self._paint_color_button(b, val)
            b.clicked.connect(lambda: self._pick_color(key, b))
            return b
        if kind == "font":
            w = QComboBox()
            w.setEditable(True)
            w.addItems(self.font_families())
            w.setCurrentText(str(val or ""))
            w.currentTextChanged.connect(lambda v: self._set(key, v))
            return w
        if kind == "image":
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            info = QLabel(f"{it.image.width}x{it.image.height}" if it.image else "(none)")
            b = QPushButton("Load…")
            b.clicked.connect(lambda: self.editor.load_item_image(it))
            s = QPushButton("Scale…")
            s.clicked.connect(lambda: self.editor.scale_item_image(it))
            s.setEnabled(it.image is not None)
            lay.addWidget(info)
            lay.addWidget(b)
            lay.addWidget(s)
            return row
        if kind == "choice":
            w = QComboBox()
            if key == "data_name":
                for dn in DATA_NAMES:
                    w.addItem(f"{DATA_LABELS.get(dn, dn)}  [{dn}]", dn)
            elif key == "sub_name":
                w.setEditable(True)
                opts = [""] + SUB_NAMES.get(it.p.get("data_name") or "", [])
                for o in opts:
                    w.addItem(o or "(default)", o)
                if val not in opts:
                    w.addItem(str(val), val)
            else:
                for v, lab in choices or []:
                    w.addItem(lab, v)
            idx = w.findData(val)
            w.setCurrentIndex(max(0, idx))
            if key == "sub_name":
                w.currentIndexChanged.connect(lambda i: self._set(key, w.itemData(i) or ""))
                w.lineEdit().editingFinished.connect(
                    lambda: self._set(key, w.currentData() if w.currentText() == "(default)" or
                                      w.findText(w.currentText()) >= 0 else w.currentText()))
            else:
                w.currentIndexChanged.connect(lambda i: self._set(key, w.itemData(i)))
            return w
        return None

    @staticmethod
    def _paint_color_button(b: QPushButton, c) -> None:
        c = c or (0, 0, 0, 255)
        b.setText("#%02x%02x%02x  α%d" % (c[0], c[1], c[2], c[3]))
        fg = "black" if (c[0] * 0.3 + c[1] * 0.59 + c[2] * 0.11) > 128 else "white"
        b.setStyleSheet(f"background-color: rgba({c[0]},{c[1]},{c[2]},{c[3]}); color: {fg};")

    def _pick_color(self, key: str, b: QPushButton) -> None:
        cur = self.item.p.get(key) or (255, 255, 255, 255)
        c = QColorDialog.getColor(qcolor(cur), self, "Choose color", QColorDialog.ShowAlphaChannel)
        if c.isValid():
            val = (c.red(), c.green(), c.blue(), c.alpha())
            self._set(key, val)
            self._paint_color_button(b, val)


# --------------------------------------------------------------------------- display worker
class DisplayLink(QObject):
    status = Signal(str)

    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        self.frame: Image.Image | None = None
        self.video: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rotation: int | None = None

    def start(self, video: str | None) -> None:
        self.stop()
        self.video = video
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        from .device import TurzxDevice, TurzxError
        from .stream import FFmpegStreamer

        from .control import ControlServer, Controller, request, socket_path

        streamer = None
        own_socket = None  # set while the display is borrowed from the service
        r = request("status", timeout=2)
        if r and r.get("daemon"):  # background service: borrow the display, give it back afterwards
            self.status.emit("Display: borrowing from the service…")
            own_socket = socket_path(f"-editor-{os.getpid()}")
            rr = request("release", timeout=20, pid=os.getpid(), socket=own_socket)
            if not rr or not rr.get("ok"):
                self.status.emit(f"Display: service did not release it ({(rr or {}).get('error')})")
                request("resume", timeout=5)
                return
        elif r and r.get("ok"):  # another turzx process plays on the display: take it over
            self.status.emit(f"Display: stopping {r.get('mode')}…")
            request("stop", timeout=5)
            for _ in range(50):
                if request("status", timeout=1) is None:
                    break
                time.sleep(0.1)
            time.sleep(0.5)
        dev = None
        for attempt in range(5):
            try:
                dev = TurzxDevice()
                break
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(0.5)
        if dev is None:
            self.status.emit(f"Display: {err}")
            if own_socket:
                request("resume", timeout=5)
            return

        ctl = Controller(dev, mode="editor", detail=self.editor.theme.name)
        server = ControlServer(ctl, path=own_socket).start()
        try:
            ctl.apply_brightness()
            dev.stop_playback()
            dev.clear_overlay()
            if self.video:
                streamer = FFmpegStreamer(dev, 0 if _is_portrait_video(self.video) else 90)
                args = FFmpegStreamer.input_args(self.video, self.editor.theme.frame_rate or 24)
                threading.Thread(target=streamer.run, args=(args, 24), daemon=True).start()
            self.status.emit(f"Display: live ({dev.info.firmware})")
            while not self._stop.wait(1.0):
                fr = self.frame
                if fr is None:
                    continue
                img = to_device(fr, dev.width, dev.height, self.rotation)
                if self.video:
                    dev.set_overlay(img)
                else:
                    bg = Image.new("RGB", img.size, (0, 0, 0))
                    bg.paste(img, mask=img.getchannel("A"))
                    dev.show_image(bg)
        except TurzxError as e:
            self.status.emit(f"Display error: {e}")
        finally:
            server.close()
            if streamer:
                streamer.stop()
            dev.close()
            if own_socket:
                request("resume", timeout=5)
                self.status.emit("Display: back to the service theme")
            else:
                self.status.emit("Display: off")


def _is_portrait_video(path: str) -> bool:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        w, h = (int(x) for x in out.split(",")[:2])
        return h > w
    except (ValueError, OSError, subprocess.SubprocessError):
        return True


def video_still(path: str, theme: Theme) -> Image.Image | None:
    """First frame of the background video, oriented like the theme canvas."""
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1",
                              "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, timeout=20).stdout
        im = Image.open(io.BytesIO(out)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
    if (im.width > im.height) != theme.landscape:
        im = im.transpose(Image.ROTATE_90)  # device-oriented video -> theme orientation
    return im.resize((theme.width, theme.height))


# --------------------------------------------------------------------------- main window
class EditorWindow(QMainWindow):
    def __init__(self, path: str | None = None, font_dirs: list[str] | None = None,
                 video_dirs: list[str] | None = None):
        super().__init__()
        self.font_dirs = font_dirs or []
        self.fonts = FontIndex(font_dirs)
        self.video_dirs = video_dirs or []
        self.sensors = Sensors()
        self.sensors.update()
        self.theme: Theme = new_theme()
        self.renderer = Renderer(self.theme, self.sensors, self.fonts)
        self.selected: Item | None = None
        self.undo_stack: list = []
        self.redo_stack: list = []
        self.dirty = False
        self.bg_still: Image.Image | None = None
        self.last_frame: Image.Image | None = None
        self.display = DisplayLink(self)
        self.display.status.connect(self.statusBar().showMessage)

        self.canvas = Canvas()
        self.setCentralWidget(self.canvas)
        self.canvas.itemClicked.connect(self.select)
        self.canvas.itemMoved.connect(self.on_drag)
        self.canvas.dragFinished.connect(self.after_drag)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.on_list_row)
        self.list.itemChanged.connect(self.on_list_check)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(2, 2, 2, 2)
        lv.addWidget(self.list)
        btns = QHBoxLayout()
        add = QToolButton()
        add.setText("+ Add")
        add.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(add)
        for t in ADDABLE:
            menu.addAction(TYPE_LABELS[t], lambda t=t: self.add_item(t))
        add.setMenu(menu)
        btns.addWidget(add)
        for text, fn in (("Duplicate", self.duplicate), ("Delete", self.delete), ("▲", self.move_up),
                         ("▼", self.move_down)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lv.addLayout(btns)
        dock = QDockWidget("Widgets", self)
        dock.setWidget(left)
        dock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        self.props = PropertyPanel(self)
        self.props.changed.connect(self.on_prop_changed)
        dock2 = QDockWidget("Properties", self)
        dock2.setWidget(self.props)
        dock2.setFeatures(QDockWidget.DockWidgetMovable)
        dock2.setMinimumWidth(330)
        self.addDockWidget(Qt.RightDockWidgetArea, dock2)

        self._build_toolbar()
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("About turzx…", self.about)
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.rerender)
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.tick)
        self.live_timer.start(1000)
        self.resize(1600, 800)
        if path:
            self.open_path(path)
        else:
            self.set_theme(new_theme())

    # -- toolbar / actions --------------------------------------------------
    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        self.addToolBar(tb)

        def act(text, fn, shortcut=None, checkable=False):
            a = QAction(text, self)
            a.triggered.connect(fn)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setCheckable(checkable)
            tb.addAction(a)
            return a

        act("New", self.new_theme_dialog, QKeySequence.New)
        act("Open…", self.open_dialog, QKeySequence.Open)
        act("Save", self.save, QKeySequence.Save)
        act("Save as…", self.save_as, "Ctrl+Shift+S")
        tb.addSeparator()
        act("Undo", self.undo, QKeySequence.Undo)
        act("Redo", self.redo, "Ctrl+Y")
        tb.addSeparator()
        act("Fit", self.canvas.fit, "Ctrl+0")
        act("Export PNG…", self.export_png)

        # second row: the display itself
        from .controlwin import DisplayControls

        self.addToolBarBreak()
        dtb = QToolBar("Display")
        self.addToolBar(dtb)
        self.live_act = QAction("Live on display", self)
        self.live_act.setCheckable(True)
        self.live_act.setToolTip("Show the edited theme on the display while editing")
        self.live_act.triggered.connect(self.toggle_display)
        dtb.addAction(self.live_act)
        use_act = QAction("Use as display theme", self)
        use_act.setToolTip("Let the background service show this theme permanently (saves it first if needed)")
        use_act.triggered.connect(self.use_as_display_theme)
        dtb.addAction(use_act)
        dtb.addSeparator()
        self.display_controls = DisplayControls(compact=True)
        dtb.addWidget(self.display_controls)
        for seq, fn in (("Delete", self.delete), ("Ctrl+D", self.duplicate),
                        ("Left", lambda: self.nudge(-1, 0)), ("Right", lambda: self.nudge(1, 0)),
                        ("Up", lambda: self.nudge(0, -1)), ("Down", lambda: self.nudge(0, 1)),
                        ("Shift+Left", lambda: self.nudge(-10, 0)), ("Shift+Right", lambda: self.nudge(10, 0)),
                        ("Shift+Up", lambda: self.nudge(0, -10)), ("Shift+Down", lambda: self.nudge(0, 10))):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            a.triggered.connect(fn)
            self.canvas.addAction(a)
            if seq in ("Delete", "Ctrl+D"):
                self.list.addAction(a)

    # -- theme handling ------------------------------------------------------
    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.renderer = Renderer(theme, self.sensors, self.fonts)
        vid = theme.find_video(self.video_dirs)
        if vid and theme.items and theme.items[0].type == "Image" and theme.items[0].x == 0 and theme.items[0].y == 0:
            theme.items[0].type = "Animation"  # like the Windows app: first image = video placeholder
        self.bg_still = video_still(vid, theme) if vid else None
        self.selected = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self.canvas._fitted = False
        self.refresh_list()
        self.props.build(None)
        self.rerender()
        self.update_title()
        if self.display.running:
            self.display.start(vid)

    def update_title(self) -> None:
        name = os.path.basename(self.theme.path) if self.theme.path else "untitled"
        self.setWindowTitle(f"TURZX Theme Editor {__version__} - {self.theme.name} [{name}]"
                            f"{' *' if self.dirty else ''}")

    def about(self) -> None:
        import html

        from . import version_info
        from .control import request

        rows = version_info()
        r = request("status", timeout=1)
        if r and r.get("ok"):
            rows.append(("Display", f"{r.get('model')}, firmware {r.get('firmware')}, serial {r.get('serial')}"))
            rows.append(("Running", f"{r.get('mode')} {os.path.basename(r.get('detail') or '')}".strip()))
        table = "".join(f"<tr><td><b>{html.escape(k)}</b>&nbsp;&nbsp;</td><td>{html.escape(str(v))}</td></tr>"
                        for k, v in rows)
        QMessageBox.about(self, "About turzx",
                          f"<h3>turzx {__version__}</h3>"
                          "<p>Linux driver, theme player and theme editor for TURZX USB displays "
                          "(8.8&quot; 480&times;1920).</p>"
                          "<p>The USB protocol, the theme format and the widget drawing were "
                          "reconstructed from the Windows app; this is not an official TURZX product.</p>"
                          f"<table>{table}</table>")

    def new_theme_dialog(self) -> None:
        if not self.confirm_discard():
            return
        choice, ok = QInputDialog.getItem(self, "New theme", "Layout:",
                                          ["Landscape 1920 x 480", "Portrait 480 x 1920"], 0, False)
        if ok:
            self.set_theme(new_theme(1920, 480) if choice.startswith("Landscape") else new_theme(480, 1920))

    def open_dialog(self) -> None:
        if not self.confirm_discard():
            return
        path = open_file(self, "theme", "Open theme", "Themes (*.turzx *.turtheme);;All files (*)", THEME_DIR)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        try:
            th = load_theme(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", f"{path}\n\n{e}")
            return
        if path.endswith(".turtheme"):
            th.path = ""  # imported: saving asks for a new .turzx file
            vid = th.find_video(self.video_dirs + [os.path.dirname(path)])
            if vid:
                th.video_path = vid
        self.set_theme(th)

    def save(self) -> bool:
        if not self.theme.path or not self.theme.path.endswith(".turzx"):
            return self.save_as()
        return self._save_to(self.theme.path)

    def save_as(self) -> bool:
        os.makedirs(THEME_DIR, exist_ok=True)
        path = save_file(self, "theme", "Save theme", f"{self.theme.name or 'theme'}.turzx",
                         "TURZX theme (*.turzx)", THEME_DIR)
        if not path:
            return False
        if not path.endswith(".turzx"):
            path += ".turzx"
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        try:
            save_turzx(self.theme, path, embed_video=True)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Save failed", str(e))
            return False
        self.dirty = False
        self.update_title()
        self.statusBar().showMessage(f"saved {path}", 5000)
        return True

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save changes first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def closeEvent(self, ev):
        if not self.confirm_discard():
            ev.ignore()
            return
        self.display.stop()
        self.sensors.close()
        ev.accept()

    def export_png(self) -> None:
        path = save_file(self, "export", "Export preview", f"{self.theme.name or 'theme'}.png", "PNG (*.png)")
        if path and self.last_frame is not None:
            img = Image.new("RGBA", self.last_frame.size, (0, 0, 0, 255))
            if self.bg_still is not None:
                img.alpha_composite(self.bg_still)
            img.alpha_composite(self.last_frame)
            img.convert("RGB").save(path)

    # -- video / background ----------------------------------------------------
    def choose_video(self) -> None:
        path = open_file(self, "video", "Background video",
                         "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.gif *.h264);;All files (*)")
        if not path:
            return
        self.push_undo()
        self.theme.video_path, self.theme.video_name = path, os.path.basename(path)
        self.bg_still = video_still(path, self.theme)
        self.after_change()
        self.props.build(None)
        if self.display.running:
            self.display.start(path)

    def remove_video(self) -> None:
        self.push_undo()
        self.theme.video_path = self.theme.video_name = None
        self.bg_still = None
        for it in self.theme.items:
            if it.type == "Animation":
                it.type = "Image"
        self.after_change()
        self.refresh_list()
        self.props.build(None)
        if self.display.running:
            self.display.start(None)

    def set_background(self) -> None:
        path = open_file(self, "image", "Background image", IMAGE_FILTER)
        if not path:
            return
        self.push_undo()
        im = Image.open(path).convert("RGBA")
        t = self.theme
        s = max(t.width / im.width, t.height / im.height)
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
        im = im.crop(((im.width - t.width) // 2, (im.height - t.height) // 2,
                      (im.width - t.width) // 2 + t.width, (im.height - t.height) // 2 + t.height))
        bg = Item("Image", 0, 0, name="Background", image=im)
        first = t.items[0] if t.items else None
        if first is not None and first.type == "Image" and first.x == 0 and first.y == 0:
            t.items[0] = bg
        else:
            t.items.insert(0, bg)
        self.after_change()
        self.refresh_list()

    def load_item_image(self, it: Item) -> None:
        path = open_file(self, "image", "Image", IMAGE_FILTER)
        if path:
            self.push_undo()
            it.image = Image.open(path).convert("RGBA")
            self.after_change()
            self.props.build(it)

    def scale_item_image(self, it: Item) -> None:
        if it.image is None:
            return
        pct, ok = QInputDialog.getDouble(self, "Scale image", "Percent:", 100.0, 1.0, 1000.0, 1)
        if ok and pct != 100.0:
            self.push_undo()
            f = pct / 100
            it.image = it.image.resize((max(1, round(it.image.width * f)), max(1, round(it.image.height * f))),
                                       Image.LANCZOS)
            self.after_change()
            self.props.build(it)

    # -- item list ---------------------------------------------------------------
    def refresh_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for it in self.theme.items:
            li = QListWidgetItem(it.label)
            li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
            li.setCheckState(Qt.Unchecked if it.hidden else Qt.Checked)
            if it.type == "Animation":
                li.setFlags(li.flags() & ~Qt.ItemIsUserCheckable)
                li.setForeground(QColor(140, 140, 140))
            self.list.addItem(li)
        if self.selected in self.theme.items:
            self.list.setCurrentRow(self.theme.items.index(self.selected))
        self.list.blockSignals(False)

    def on_list_row(self, row: int) -> None:
        if 0 <= row < len(self.theme.items):
            self.select(self.theme.items[row], from_list=True)

    def on_list_check(self, li: QListWidgetItem) -> None:
        row = self.list.row(li)
        it = self.theme.items[row]
        hidden = li.checkState() != Qt.Checked
        if it.hidden != hidden:
            self.push_undo()
            it.hidden = hidden
            self.after_change()

    def select(self, it: Item | None, from_list: bool = False) -> None:
        self.selected = it
        if not from_list:
            self.list.blockSignals(True)
            self.list.setCurrentRow(self.theme.items.index(it) if it in self.theme.items else -1)
            self.list.blockSignals(False)
        self.props.build(it)
        self.canvas.show_selection(self.renderer.bboxes.get(id(it)) if it else None)

    # -- editing operations --------------------------------------------------------
    def push_undo(self) -> None:
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-80]
        self.redo_stack.clear()
        self.dirty = True
        self.update_title()

    def _snapshot(self):
        t = self.theme
        return ([it.copy() for it in t.items], t.name, t.video_path, t.video_name, t.frame_rate,
                t.items.index(self.selected) if self.selected in t.items else -1)

    def _restore(self, snap) -> None:
        items, name, vp, vn, fr, sel = snap
        t = self.theme
        t.items, t.name, t.video_path, t.video_name, t.frame_rate = items, name, vp, vn, fr
        self.selected = items[sel] if 0 <= sel < len(items) else None
        self.refresh_list()
        self.props.build(self.selected)
        self.after_change(mark_dirty=False)

    def undo(self) -> None:
        if self.undo_stack:
            self.redo_stack.append(self._snapshot())
            self._restore(self.undo_stack.pop())

    def redo(self) -> None:
        if self.redo_stack:
            self.undo_stack.append(self._snapshot())
            self._restore(self.redo_stack.pop())

    def add_item(self, typ: str) -> None:
        self.push_undo()
        t = self.theme
        it = new_item(typ, t.width // 2 - 50, t.height // 2 - 20)
        if typ == "Text":
            it.p["text"] = "Text"
        if typ in ("Image", "Clock"):
            path = open_file(self, "image", "Image", IMAGE_FILTER)
            if path:
                it.image = Image.open(path).convert("RGBA")
            if typ == "Clock":
                it.p["centerX"], it.p["centerY"] = t.width // 2, t.height // 2
                it.x, it.y = (-(it.image.width // 2), -it.image.height) if it.image else (0, 0)
        idx = t.items.index(self.selected) + 1 if self.selected in t.items else len(t.items)
        t.items.insert(idx, it)
        self.selected = it
        self.refresh_list()
        self.after_change()
        self.select(it)

    def duplicate(self) -> None:
        if not self.selected:
            return
        self.push_undo()
        it = self.selected.copy()
        it.x += 10
        it.y += 10
        if it.type == "Clock":
            it.x -= 10
            it.y -= 10
            it.p["centerX"] = it.p.get("centerX", 0) + 10
            it.p["centerY"] = it.p.get("centerY", 0) + 10
        self.theme.items.insert(self.theme.items.index(self.selected) + 1, it)
        self.selected = it
        self.refresh_list()
        self.after_change()
        self.select(it)

    def delete(self) -> None:
        if not self.selected:
            return
        self.push_undo()
        self.theme.items.remove(self.selected)
        self.selected = None
        self.refresh_list()
        self.props.build(None)
        self.after_change()

    def _move(self, d: int) -> None:
        it = self.selected
        if not it:
            return
        items = self.theme.items
        i = items.index(it)
        j = i + d
        if 0 <= j < len(items):
            self.push_undo()
            items[i], items[j] = items[j], items[i]
            self.refresh_list()
            self.after_change()

    def move_up(self) -> None:
        self._move(-1)

    def move_down(self) -> None:
        self._move(1)

    def _shift(self, it: Item, dx: int, dy: int) -> None:
        if it.type == "Clock":  # x/y are needle offsets; move the pivot instead
            it.p["centerX"] = int(it.p.get("centerX", 0)) + dx
            it.p["centerY"] = int(it.p.get("centerY", 0)) + dy
        else:
            it.x += dx
            it.y += dy

    def nudge(self, dx: int, dy: int) -> None:
        if self.selected:
            self.push_undo()
            self._shift(self.selected, dx, dy)
            self.after_change()
            self.props.build(self.selected)

    def on_drag(self, it: Item, dx: int, dy: int) -> None:
        if not getattr(self, "_dragging", False):
            self.push_undo()
            self._dragging = True
        self._shift(it, dx, dy)
        bb = self.renderer.bboxes.get(id(it))
        if bb:  # move the selection frame immediately, re-render throttled
            self.renderer.bboxes[id(it)] = (bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy)
            self.canvas.show_selection(self.renderer.bboxes[id(it)])
        if not self.render_timer.isActive():
            self.render_timer.start(60)

    def after_drag(self) -> None:
        if getattr(self, "_dragging", False):
            self._dragging = False
            self.props.build(self.selected)
            self.rerender()

    def on_prop_changed(self, key: str) -> None:
        if key in ("name", "data_name", "text", "hidden"):
            self.refresh_list()
        if key == "name":
            self.update_title()
        self.after_change()

    def after_change(self, mark_dirty: bool = True) -> None:
        if mark_dirty:
            self.dirty = True
            self.update_title()
        if not self.render_timer.isActive():
            self.render_timer.start(40)

    # -- rendering -------------------------------------------------------------------
    def tick(self) -> None:
        self.sensors.update()
        self.rerender(update_values=True)

    def rerender(self, update_values: bool = False) -> None:
        r = self.renderer
        r.update_values = update_values or self.last_frame is None
        r.video_background = self.bg_still is not None
        t0 = time.monotonic()
        frame = r.render(include_hidden=False)
        r.update_values = True
        self.last_frame = frame
        self.canvas.hits = [(it, r.bboxes[id(it)]) for it in self.theme.items if id(it) in r.bboxes]
        self.canvas.set_frame(frame, self.bg_still)
        self.canvas.show_selection(r.bboxes.get(id(self.selected)) if self.selected else None)
        self.display.frame = frame
        self.display.rotation = 90 if self.theme.landscape else 0
        dt = (time.monotonic() - t0) * 1000
        if not self.display.running:
            self.statusBar().showMessage(f"{len(self.theme.items)} widgets, render {dt:.0f} ms")

    def use_as_display_theme(self) -> None:
        from . import service
        from .control import request

        if self.dirty or not self.theme.path:
            if not self.save():
                return
        path = os.path.abspath(self.theme.path)
        # the service needs the same fonts and videos as the editor
        st = load_settings()
        for key, dirs in (("font_dirs", self.font_dirs), ("video_dirs", self.video_dirs)):
            merged = list(st.get(key, []))
            for d in dirs:
                d = os.path.abspath(d)
                if os.path.isdir(d) and d not in merged:
                    merged.append(d)
            save_setting(key, merged)
        if self.display.running:  # give the display back to the service
            self.live_act.setChecked(False)
            self.display.stop()
        r = request("play", path=path, timeout=10)
        if r is None:
            save_setting("theme", path)
            ans = QMessageBox.question(
                self, "Background service",
                "The background service is not running.\n\nInstall and start it now? It shows this theme "
                "from login on, also when the editor is closed.")
            if ans != QMessageBox.Yes:
                return
            try:
                service.install()
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "Service", f"Could not install the service: {e}")
                return
            self.statusBar().showMessage(f"Service installed, showing {os.path.basename(path)}", 8000)
        elif not r.get("ok"):
            QMessageBox.warning(self, "Display", str(r.get("error")))
        else:
            self.statusBar().showMessage(f"Display now shows {os.path.basename(path)}", 8000)
        self.display_controls.refresh()

    def toggle_display(self, on: bool) -> None:
        if on:
            self.display.start(self.theme.find_video(self.video_dirs))
        else:
            self.display.stop()


def run(path: str | None = None, font_dirs: list[str] | None = None, video_dirs: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("TURZX Theme Editor")
    w = EditorWindow(path, font_dirs, video_dirs)
    w.show()
    return app.exec()
