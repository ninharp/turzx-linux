"""Small control window: brightness, backlight on/off, reboot, theme selection."""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSlider, QVBoxLayout, QWidget)

from .control import request, run_command, saved_brightness

DATA = os.path.expanduser("~/.local/share/turzx")
THEME_DIRS = [os.path.join(DATA, "themes"), os.path.join(DATA, "theme")]
LOG_PATH = os.path.expanduser("~/.cache/turzx/player.log")


class _Bridge(QObject):
    done = Signal(str, object)  # command, reply


class DisplayControls(QWidget):
    """Brightness slider, backlight toggle and restart button. Works with
    whatever turzx process currently owns the display (or opens it directly)."""

    def __init__(self, parent: QWidget | None = None, compact: bool = False):
        super().__init__(parent)
        self.busy = False
        self.bridge = _Bridge()
        self.bridge.done.connect(self._on_done)
        self.state = QLabel("…")
        self.state.setTextFormat(Qt.RichText)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(saved_brightness())
        self.slider.setToolTip("Display brightness")
        self.value = QLabel(f"{self.slider.value()} %")
        self.value.setMinimumWidth(40)
        self.bright_timer = QTimer(self, singleShot=True, interval=150)
        self.bright_timer.timeout.connect(lambda: self.run("brightness", value=self.slider.value()))
        self.slider.valueChanged.connect(self._slider_moved)
        self.light = QPushButton("Backlight off")
        self.light.setCheckable(True)
        self.light.toggled.connect(lambda off: self.run("off" if off else "on"))
        self.reboot = QPushButton("Restart display")
        self.reboot.setToolTip("Reboots the display; a running theme continues afterwards")
        self.reboot.clicked.connect(lambda: self.run("reboot"))

        if compact:  # one row, e.g. for a toolbar
            lay = QHBoxLayout(self)
            lay.setContentsMargins(4, 0, 4, 0)
            lay.addWidget(QLabel("Brightness"))
            self.slider.setFixedWidth(140)
            lay.addWidget(self.slider)
            lay.addWidget(self.value)
            lay.addWidget(self.light)
            lay.addWidget(self.reboot)
            lay.addSpacing(8)
            lay.addWidget(self.state)
            lay.addStretch(1)  # keep the controls packed to the left
        else:
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            self.state.setWordWrap(True)
            lay.addWidget(self.state)
            box = QGroupBox("Display")
            bl = QVBoxLayout(box)
            row = QHBoxLayout()
            row.addWidget(QLabel("Brightness"))
            row.addWidget(self.slider, 1)
            row.addWidget(self.value)
            bl.addLayout(row)
            row = QHBoxLayout()
            row.addWidget(self.light)
            row.addWidget(self.reboot)
            bl.addLayout(row)
            lay.addWidget(box)
        self.compact = compact
        self.poll = QTimer(self, interval=2000)
        self.poll.timeout.connect(self.refresh)
        self.poll.start()
        self.status_changed = None  # optional callback(status dict | None)
        self.refresh()

    def _slider_moved(self, v: int) -> None:
        self.value.setText(f"{v} %")
        self.bright_timer.start()

    def run(self, cmd: str, **kw) -> None:
        if self.busy:
            return
        if cmd == "reboot":
            self.set_busy(True, "Restarting display…")
        threading.Thread(target=lambda: self.bridge.done.emit(cmd, run_command(cmd, **kw)), daemon=True).start()

    def _on_done(self, cmd: str, r: dict) -> None:
        if cmd == "reboot":
            self.set_busy(False)
        if not r.get("ok"):
            self.state.setText(f"<b>Error:</b> {r.get('error')}")
            return
        if cmd == "brightness" and self.light.isChecked():
            self._show_light(False)  # setting a brightness switches the backlight on
        if cmd in ("off", "on"):
            self._show_light(cmd == "off")
        self.refresh()

    def _show_light(self, off: bool) -> None:
        self.light.blockSignals(True)
        self.light.setChecked(off)
        self.light.setText("Backlight on" if off else "Backlight off")
        self.light.blockSignals(False)

    def set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        for w in (self.slider, self.light, self.reboot):
            w.setEnabled(not busy)
        if text:
            self.state.setText(text)

    def refresh(self) -> None:
        if self.busy:
            return
        r = request("status", timeout=1)
        if r and r.get("ok"):
            mode = r.get("mode") or "?"
            detail = r.get("detail") or ""
            if mode == "theme":
                detail = os.path.splitext(os.path.basename(detail))[0]
            text = f"<b>Running:</b> {mode} {detail}"
            if not self.compact:
                text += f"<br>{r.get('model')}, firmware {r.get('firmware')}"
            self.state.setText(text)
            if not self.slider.isSliderDown() and not self.bright_timer.isActive():
                self.slider.blockSignals(True)
                self.slider.setValue(int(r.get("brightness", self.slider.value())))
                self.value.setText(f"{self.slider.value()} %")
                self.slider.blockSignals(False)
            self._show_light(bool(r.get("off")))
        else:
            r = None
            self.state.setText("<i>display idle</i>" if self.compact else
                               "<b>Nothing running</b> - commands open the display directly.")
        if self.status_changed:
            self.status_changed(r)


class ControlWindow(QWidget):
    def __init__(self, font_dirs: list[str] | None = None, video_dirs: list[str] | None = None,
                 theme_dirs: list[str] | None = None):
        super().__init__()
        self.setWindowTitle("TURZX Display")
        self.font_dirs = font_dirs or []
        self.video_dirs = video_dirs or []
        self.theme_dirs = (theme_dirs or []) + THEME_DIRS
        self.bridge = _Bridge()
        self.bridge.done.connect(self._on_done)

        lay = QVBoxLayout(self)
        self.controls = DisplayControls(self)
        lay.addWidget(self.controls)

        box = QGroupBox("Theme")
        tl = QVBoxLayout(box)
        self.themes = QListWidget()
        self.themes.itemDoubleClicked.connect(lambda _: self.play())
        tl.addWidget(self.themes, 1)
        row = QHBoxLayout()
        add = QPushButton("Add…")
        add.clicked.connect(self.add_theme)
        self.play_b = QPushButton("Play")
        self.play_b.clicked.connect(self.play)
        self.stop_b = QPushButton("Stop")
        self.stop_b.clicked.connect(lambda: self.controls.run("stop"))
        row.addWidget(add)
        row.addStretch(1)
        row.addWidget(self.play_b)
        row.addWidget(self.stop_b)
        tl.addLayout(row)
        lay.addWidget(box, 1)
        self.controls.status_changed = lambda r: self.stop_b.setEnabled(
            bool(r) and r.get("mode") != "editor" and not self.controls.busy)

        self.resize(460, 520)
        self.load_themes()
        self.controls.refresh()

    # -- themes ------------------------------------------------------------
    def load_themes(self, select: str | None = None) -> None:
        self.themes.clear()
        seen = set()
        for d in self.theme_dirs:
            files = glob.glob(os.path.join(d, "**", "*.turzx"), recursive=True)
            files += glob.glob(os.path.join(d, "**", "*.turtheme"), recursive=True)
            for f in sorted(files, key=lambda p: os.path.basename(p).lower()):
                f = os.path.abspath(f)
                if f in seen:
                    continue
                seen.add(f)
                name, ext = os.path.splitext(os.path.basename(f))
                it = QListWidgetItem(f"{name}   ({ext[1:]})")
                it.setData(Qt.UserRole, f)
                it.setToolTip(f)
                self.themes.addItem(it)
                if f == select:
                    self.themes.setCurrentItem(it)

    def add_theme(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Add theme", os.path.expanduser("~"),
                                           "Themes (*.turzx *.turtheme)")
        if f:
            d = os.path.dirname(os.path.abspath(f))
            if d not in self.theme_dirs:
                self.theme_dirs.insert(0, d)
            self.load_themes(select=os.path.abspath(f))

    def play(self) -> None:
        it = self.themes.currentItem()
        if not it or self.controls.busy:
            return
        path = it.data(Qt.UserRole)
        self.controls.set_busy(True, f"Starting {os.path.basename(path)}…")
        self.play_b.setEnabled(False)

        def work():
            st = request("status", timeout=2)
            if st and st.get("daemon"):  # background service: just switch its theme
                self.bridge.done.emit("play", request("play", path=path, timeout=10) or {"ok": False})
                return
            if request("stop", timeout=5) is not None:
                for _ in range(50):  # wait until the old player released the display
                    if request("status", timeout=1) is None:
                        break
                    time.sleep(0.1)
                time.sleep(0.5)
            cmd = [sys.executable, "-m", "turzx", "theme", path]
            for d in self.font_dirs:
                cmd += ["--font-dir", d]
            for d in self.video_dirs:
                cmd += ["--video-dir", d]
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "ab") as log:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True,
                                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            for _ in range(100):
                r = request("status", timeout=1)
                if r and r.get("ok"):
                    self.bridge.done.emit("play", {"ok": True})
                    return
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            self.bridge.done.emit("play", {"ok": False, "error": f"player did not start, see {LOG_PATH}"})

        threading.Thread(target=work, daemon=True).start()

    def _on_done(self, cmd: str, r: dict) -> None:
        self.controls.set_busy(False)
        self.play_b.setEnabled(True)
        if not r.get("ok"):
            self.controls.state.setText(f"<b>Error:</b> {r.get('error')}")
        self.controls.refresh()


def run(font_dirs: list[str] | None = None, video_dirs: list[str] | None = None,
        theme_dirs: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("TURZX Display")
    w = ControlWindow(font_dirs, video_dirs, theme_dirs)
    w.show()
    return app.exec()
