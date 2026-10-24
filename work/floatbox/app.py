"""悬浮框窗口层（PySide6）：托盘常驻 + 全局热键呼出 + Ctrl+Enter 存入。

关键决策：
- Qt 没有系统级全局热键，这里用 Windows RegisterHotKey(ctypes)，通过 QAbstractNativeEventFilter 接收 WM_HOTKEY。
- 提交逻辑在 submit.py（可单测）；本文件只负责界面，失败时绝不清空文本。
- 热键注册失败（被占用）时在托盘提示，并允许通过环境变量 KB_HOTKEY 修改，如 "ctrl+alt+j"。
- Windows 不允许后台进程随意抢前台焦点：显示后先用 AttachThreadInput 技巧再 SetForegroundWindow。
  这是 V0.1 的 PoC 风险点，需人工验收。
"""
import ctypes
import os
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, Qt, QTimer, QSettings
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QPlainTextEdit, QSystemTrayIcon, QMenu, QVBoxLayout, QWidget, QLineEdit, QFormLayout, QMessageBox)

try:  # 以 `python -m floatbox.app` 运行（README 推荐）
    from .submit import submit_text
except ImportError:  # 以 `python floatbox/app.py` 直接运行
    from submit import submit_text

WM_HOTKEY = 0x0312
MOD = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
HOTKEY_ID = 0x4B42
user32 = ctypes.windll.user32


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+k' -> (modifiers, vk)。仅支持单个字母/数字键。"""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods = 0
    for p in parts[:-1]:
        mods |= MOD[p]
    return mods | 0x4000, ord(parts[-1].upper())  # 0x4000 = MOD_NOREPEAT


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.callback()
                return True, 0
        return False, 0


class FloatBox(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("知识库 · 快速收集")
        self.setMinimumSize(320, 240)
        self.resize(400, 300)
        self.settings = QSettings("PersonalKnowledgeBase", "QuickCapture")
        saved = self.settings.value("geometry")
        if saved:
            self.restoreGeometry(saved)
        else:
            area = QApplication.primaryScreen().availableGeometry()
            self.move(area.right()-self.width()-24, area.bottom()-self.height()-48)
        if not self.settings.value("compact_default_v1", False, type=bool):
            self.resize(400, 300)
            self.settings.setValue("compact_default_v1", True)
        self.edit = QPlainTextEdit(placeholderText="粘贴或输入文本，Ctrl+Enter 存入，Esc 隐藏")
        self.title_input = QLineEdit(placeholderText="选填，留空自动取标题")
        self.category_input = QLineEdit(placeholderText="选填，如 研究/信息检索；留空自动分类")
        self.count = QLabel("0 字")
        self.status = QLabel("")
        self.status_timer = QTimer(self)
        self.status_timer.setSingleShot(True)
        self.status_timer.timeout.connect(self.status.clear)
        save = QPushButton("存入")
        save.setToolTip("存入知识库 (Ctrl+Enter)")
        clear = QPushButton("清空")
        close = QPushButton("收起")
        pin = QPushButton("置顶")
        pin.setCheckable(True)
        pin.setChecked(True)
        pin.toggled.connect(self.set_pinned)
        row = QHBoxLayout()
        for w in (self.count,):
            row.addWidget(w)
        row.addStretch()
        for b in (pin, clear, save, close):
            b.setMinimumWidth(0)
            b.setMaximumWidth(60)
            row.addWidget(b)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("标题",self.title_input)
        form.addRow("分类",self.category_input)
        lay.addLayout(form)
        lay.addWidget(QLabel("正文"))
        lay.addWidget(self.edit)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addLayout(row)
        self.edit.textChanged.connect(lambda: self.count.setText(f"{len(self.edit.toPlainText())} 字"))
        save.clicked.connect(self.save)
        clear.clicked.connect(self.edit.clear)
        close.clicked.connect(self.hide)
        QShortcut(QKeySequence("Ctrl+Return"), self, self.save)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.save)
        QShortcut(QKeySequence("Esc"), self, self.hide)

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.popup()

    def popup(self):
        if not any(s.availableGeometry().intersects(self.frameGeometry()) for s in QApplication.screens()):
            area = QApplication.primaryScreen().availableGeometry()
            self.move(area.topLeft())
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._force_foreground(int(self.winId()))
        self.edit.setFocus()

    def set_pinned(self, enabled):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.show()

    def hideEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        super().hideEvent(event)

    def closeEvent(self, event):
        # Closing the editor keeps its draft and tray entry alive.
        event.ignore()
        self.hide()

    def moveEvent(self, event):
        if hasattr(self, "settings") and not self.isMaximized():
            self.settings.setValue("geometry", self.saveGeometry())
        super().moveEvent(event)

    def resizeEvent(self, event):
        if hasattr(self, "settings") and not self.isMaximized():
            self.settings.setValue("geometry", self.saveGeometry())
        super().resizeEvent(event)

    @staticmethod
    def _force_foreground(hwnd: int):
        """附加到前台窗口线程再 SetForegroundWindow，绕过 Windows 的焦点限制。"""
        fg = user32.GetForegroundWindow()
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        if fg_tid and fg_tid != cur:
            user32.AttachThreadInput(fg_tid, cur, True)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(fg_tid, cur, False)
        else:
            user32.SetForegroundWindow(hwnd)

    def save(self):
        self.status_timer.stop()
        res = submit_text(self.edit.toPlainText(), title=self.title_input.text(), category_path=self.category_input.text())
        if res.duplicate:
            choice=QMessageBox.question(self,"相似资料已存在",res.message+"\n是否仍然另存一份？选择否将保留当前输入。",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
            if choice == QMessageBox.Yes:
                res=submit_text(self.edit.toPlainText(),title=self.title_input.text(),category_path=self.category_input.text(),allow_duplicate=True)
        if res.ok:
            self.edit.clear()
            self.status.setStyleSheet("color: green")
            self.status.setText(res.message)
            self.title_input.clear()
            self.edit.setFocus()  # Keep category and window for continuous capture.
            self.status_timer.start(5000)
        else:
            self.status.setStyleSheet("color: red")  # 失败：红色提示，保留文本
            self.status.setText(res.message)


def main():
    import hashlib
    from pathlib import Path
    identity=hashlib.sha256(str(Path(__file__).resolve().parent.parent).casefold().encode()).hexdigest()[:16]
    kernel=ctypes.windll.kernel32
    kernel.CreateMutexW.restype=ctypes.c_void_p
    instance=kernel.CreateMutexW(None,False,"Local\\ShizhiFloatbox-"+identity)
    if kernel.GetLastError()==183:return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    box = FloatBox()
    tray = QSystemTrayIcon(app.style().standardIcon(app.style().StandardPixmap.SP_FileIcon), app)
    menu = QMenu()
    act_show = QAction("打开悬浮框")
    act_show.triggered.connect(box.popup)
    act_quit = QAction("退出")
    act_quit.triggered.connect(app.quit)
    menu.addActions([act_show, act_quit])
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: box.popup() if r == QSystemTrayIcon.Trigger else None)
    tray.show()

    spec = os.environ.get("KB_HOTKEY", "ctrl+alt+k")
    mods, vk = parse_hotkey(spec)
    if user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
        flt = HotkeyFilter(box.toggle)
        app.installNativeEventFilter(flt)
        tray.showMessage("知识库悬浮框", f"已运行，按 {spec} 呼出")
    else:
        tray.showMessage("知识库悬浮框", f"热键 {spec} 注册失败（可能被占用），可点击托盘图标呼出；"
                                       "可设置环境变量 KB_HOTKEY 更换")
    code = app.exec()
    user32.UnregisterHotKey(None, HOTKEY_ID)
    sys.exit(code)


if __name__ == "__main__":
    main()
