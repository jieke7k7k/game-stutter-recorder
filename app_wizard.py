#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏卡顿记录程序 向导版（v1.2.0）
================================
用户 2026-10-28 确认的向导单文件版，界面样品：
  workspace/your_files/界面样品-向导版-步骤1.png（浅色/深色）
  workspace/your_files/界面样品-向导版-步骤2记录中.png
第 3 步（停止看报告）沿用 v1.1.0 的报告，结论放最前面，不重画。

向导只有三步，顶上是步骤指示，当前步实心、走完步打勾：
  1 打开游戏：程序自动找正在运行的游戏，默认选中，用户可改；
  2 开始记录：只留一个大按钮，F9 也能开始/停止，不用切出来；
  3 停止看报告：结论摘要先显示，报告自动打开，也能再打开、再录一次。

单文件：PyInstaller --onefile 打包时把官方 PresentMon 2.6.0 x64
控制台版一起打进去（工作流构建时从官方下载并校验），
用户只下一个 exe 就能用，不用再单独下载、放一起。
PresentMon 查找顺序：exe 旁边的 > 内置的 > 设置里手动选的，
三处都没有才弹一句话说明，不贴报错代码。

记录和分析逻辑一律复用 recorder.py，不复制、不改核心：
  - PresentMon（ETW 外部采集，不注入游戏）逐帧；
  - 每 1 秒传感器采样；
  - 结论永远是“最可能”，不是百分之百；
  - CPU 温度在 Windows 下不装驱动读不到，不承诺。

v1.1 修过的问题，这里原样保留，不要回退：
  - 深色报告 CSS 盖不住内联样式（P0-1）；
  - 分析在后台线程，不阻塞界面（P0-2）；
  - --windowed 下 stdout 为 None 时用弹窗代替打印（P0-3）；
  - 单实例、关闭杀子进程、冻结路径、双击误停、进程不默认选中等。

本文件在另一 AI 互查通过前只能算开发版，正式发版前必须先互查，
互查提示词先给用户过目批准，P0 修完并复跑测试通过才许打包。
"""

import argparse
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# recorder.py 就在同一文件夹，PyInstaller 打包时会跟随 import 一起打进去。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import recorder

VERSION = "1.2.0"
# 报告 HTML 里写的是 recorder.VERSION，这里改成向导版的版本号。
# 直接跑 python recorder.py --analyze 生成的报告仍是 1.0.0-试用版，
# 那是 Tkinter 版的版本号，不是 bug，三个版本共用一个核心文件。
# 互查 v1.2 注：这是模块级副作用，不要在同一进程同时 import app_qt
# 和 app_wizard，后导入的会改掉前者的报告版本；三个入口都是分开跑的。
recorder.VERSION = VERSION


def app_dir():
    """程序所在文件夹。PyInstaller --onefile 下用 sys.executable 所在目录，
    不要用 __file__ 所在的临时解包目录（v1.1 修过的坑）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_dir():
    """PyInstaller --add-data 解包目录（只有打包后才有），开发时返回 None。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


# ---------------------------------------------------------------- 纯函数（不依赖 Qt，可单独测试）

# 明显不是游戏的系统进程（小写比对）。自动选中只做“最佳猜测”，
# 用户随时可改，不要写成“一定能认出游戏”。
SYSTEM_PROCESSES = {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "taskhostw.exe", "sihost.exe",
    "ctfmon.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "gamestutterrecorder.exe",
}


def _base_name(name):
    """取进程名的文件部分，兼容完整路径（Windows 反斜杠在 Linux 下不是分隔符）。"""
    return (name or "").replace("\\", "/").split("/")[-1].strip()


def foreground_process_name():
    """取当前前台窗口的进程 exe 名（Windows 专用，只取进程名，不读窗口标题、不注入）。

    任何一步失败都返回空字符串，调用方回落到原来的进程列表逻辑，绝不抛异常。
    非 Windows 直接返回空。
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        proc = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        if not proc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                proc, 0, buf, ctypes.byref(size))
            if not ok:
                return ""
            return _base_name(buf.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(proc)
    except Exception:
        return ""


def choose_default_game(processes, foreground=None):
    """从进程名列表里选一个默认游戏，选不到返回空字符串。

    不默认选列表第一项（v1.1 踩过的坑：第一项可能是系统进程），
    全是系统进程或列表为空时返回空，界面保持未选中状态。
    互查 v1.2 补：优先猜前台窗口的游戏（foreground 是前台进程名时，
    它在列表里且不是系统进程就先选它）；选不中再按原来逻辑。
    比对用文件部分，列表里某天出现完整路径时过滤也不失效。
    """
    fg = _base_name(foreground)
    if fg and fg.lower() not in SYSTEM_PROCESSES:
        for name in processes or []:
            if name and _base_name(name).lower() == fg.lower():
                return name
    for name in processes or []:
        if not name:
            continue
        if _base_name(name).lower() in SYSTEM_PROCESSES:
            continue
        return name
    return ""


def resolve_presentmon_path(base_dir, bundled=None, settings_path=None):
    """按顺序找 PresentMon.exe，返回第一个存在的文件，没有返回 None。

    顺序：exe 旁边的 > 内置的（打包解包目录）> 设置里手动选的。
    settings_path 为空字符串时当作没设置，不要把空路径当成当前目录。
    """
    candidates = [Path(base_dir) / "PresentMon.exe"]
    if bundled is not None:
        candidates.append(Path(bundled) / "PresentMon.exe")
    if settings_path:
        candidates.append(Path(settings_path))
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 深色 / 浅色主题（沿用 v1.1 的配色）

LIGHT = {
    "window": "#fafafa", "card": "#ffffff", "border": "#e4e4e7",
    "text": "#18181b", "muted": "#71717a", "small": "#52525b",
    "pill": "#f4f4f5", "primary_bg": "#18181b", "primary_fg": "#ffffff",
    "input_bg": "#ffffff",
}
DARK = {
    "window": "#18181b", "card": "#27272a", "border": "#3f3f46",
    "text": "#fafafa", "muted": "#a1a1aa", "small": "#d4d4d8",
    "pill": "#27272a", "primary_bg": "#fafafa", "primary_fg": "#18181b",
    "input_bg": "#27272a",
}


def build_qss(t):
    """根据主题字典拼 QSS，下拉仍用 Fusion 系统箭头，不用字母 v 冒充。"""
    return f"""
QWidget {{ background: {t['window']}; color: {t['text']};
  font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 12px; }}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {t['muted']}; }}
QLabel#small {{ color: {t['small']}; }}
QLabel#pill {{ background: {t['pill']}; border-radius: 16px; padding: 8px 16px; }}
QLineEdit, QComboBox {{ background: {t['input_bg']}; color: {t['text']};
  border: 1px solid {t['border']}; border-radius: 8px; padding: 8px 10px; min-height: 28px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QLineEdit {{ border: none; background: transparent; }}
QComboBox QAbstractItemView {{ background: {t['input_bg']}; color: {t['text']};
  border: 1px solid {t['border']}; selection-background-color: {t['primary_bg']};
  selection-color: {t['primary_fg']}; outline: 0; }}
QComboBox:disabled {{ color: {t['muted']}; background: {t['pill']}; }}
QPushButton#primary {{ background: {t['primary_bg']}; color: {t['primary_fg']};
  border: none; border-radius: 10px; padding: 12px 20px; font-weight: 600; }}
QPushButton#primary:disabled {{ background: {t['pill']}; color: {t['muted']}; }}
QPushButton#secondary {{ background: {t['card']}; color: {t['text']};
  border: 1px solid {t['border']}; border-radius: 8px; padding: 8px 14px; }}
QPushButton#link {{ background: transparent; border: none; color: {t['text']};
  text-decoration: underline; padding: 0px; text-align: left; }}
QPushButton#link:disabled {{ color: {t['muted']}; text-decoration: none; }}
QPushButton#toggle {{ background: transparent; color: {t['small']};
  border: 1px solid {t['border']}; border-radius: 16px; padding: 6px 14px; }}
"""


def detect_system_dark():
    """跟随系统。Qt 6.5+ 有 colorScheme，老版本没有就默认浅色，不崩溃。"""
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        scheme = QApplication.styleHints().colorScheme()
        return scheme == Qt.ColorScheme.Dark
    except Exception:
        return False


# v1.1 的 P0-1 修复原样保留：原注入 CSS 盖不住内联样式，
# 深色下 #71717a 在 #27272a 上对比度只有约 3.1:1，必须用 !important 覆盖。
DARK_CSS_FOR_REPORT = """<style>
@media (prefers-color-scheme: dark) {
  body { background: #18181b; color: #fafafa; }
  .box, .conclusion { background: #27272a; border-color: #3f3f46; }
  th { color: #a1a1aa; border-color: #3f3f46; }
  td { border-color: #3f3f46; }
  td[style] { color: #d4d4d8 !important; }
  .muted { color: #a1a1aa; }
  .conclusion { border-left-color: #fafafa; }
}
</style>
"""


def inject_report_dark_css(report_path):
    """报告 HTML 追加深色媒体查询，失败静默跳过，不影响分析结果。"""
    try:
        p = Path(report_path)
        text = p.read_text(encoding="utf-8")
        if "prefers-color-scheme: dark" in text:
            return
        if "</head>" in text:
            text = text.replace("</head>", DARK_CSS_FOR_REPORT + "</head>", 1)
            p.write_text(text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 分析（后台线程跑，不碰任何 QWidget）

def _do_analysis(frames_path, sensors_path, session_dir, game, started_iso, sensor_ok=True):
    """纯函数：解析、分析、生成报告、注入深色样式。

    返回 (报告路径, 结论一句, 摘要一句)。只接收路径和字符串，不碰界面。
    传感器如果没停干净（sensor_ok=False），就当没有传感器数据，不读半截文件。
    """
    frames_path = Path(frames_path)
    if not frames_path.exists() or frames_path.stat().st_size == 0:
        raise ValueError("PresentMon 没有写出帧数据，请确认游戏 exe 名没写错，游戏在记录时确实在运行。")
    headers, rows = recorder.parse_csv_file(frames_path)
    try:
        frames = recorder.parse_frames(headers, rows)
    except ValueError as e:
        raise ValueError(f"没有有效帧数据：{e}，请确认游戏 exe 名和记录时间。")
    if not frames:
        raise ValueError("frames.csv 里没有有效帧，请确认游戏 exe 名没写错，游戏在记录时确实在运行。")
    sensors = None
    if sensor_ok and sensors_path is not None and Path(sensors_path).exists():
        try:
            sh, sr = recorder.parse_csv_file(sensors_path)
            sensors = recorder.parse_sensors(sh, sr)
        except Exception:
            sensors = None
    analysis = recorder.analyze_frames(frames, sensors)
    report = recorder.generate_report_html(session_dir, game, started_iso, analysis, has_timestamp=True)
    inject_report_dark_css(report)
    main, note = recorder.main_conclusion(analysis, has_timestamp=True)
    summary = (f"帧数 {analysis['frame_count']} · 平均 FPS {recorder.fmt(analysis['avg_fps'])} · "
               f"1% Low {recorder.fmt(analysis['low1'])} · 卡顿 {len(analysis['events'])} 次")
    if analysis["events"]:
        summary += f" · 其中 {analysis['top_share']}% 是{analysis['top_cause']}"
    return report, main, summary


# ---------------------------------------------------------------- 全局热键 F9（仅 Windows）

def _install_hotkey(window, app, callback):
    """用 Windows RegisterHotKey 注册 F9，返回 (是否成功, 注销函数)。

    只注册 F9 一个键，不轮询键盘、不记录别的按键。失败时返回 False，
    调用方只改提示文案，不弹窗，按钮流程照常可用。
    非 Windows 平台直接返回 False，不做任何事。
    注销函数可重复调用，不会重复注销报错。
    """
    if sys.platform != "win32":
        return False, (lambda: None)
    try:
        import ctypes
        from ctypes import wintypes
        from PySide6.QtCore import QAbstractNativeEventFilter

        HOTKEY_ID = 9001
        VK_F9 = 0x78
        WM_HOTKEY = 0x0312

        class _Msg(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

        class _Filter(QAbstractNativeEventFilter):
            def __init__(self, cb):
                super().__init__()
                self.cb = cb

            def nativeEventFilter(self, eventType, message):
                # 互查 v1.2 修：只对 Windows MSG 消息做 cast，别把别的原生
                # 消息按 MSG 结构解析；PySide6 官方写法就是拿 eventType
                # 跟 b"windows_generic_MSG" / b"windows_dispatcher_MSG" 比对。
                if eventType not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                    return False, 0
                try:
                    msg = ctypes.cast(message, ctypes.POINTER(_Msg)).contents
                    if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        self.cb()
                        return True, 0
                except Exception:
                    pass
                return False, 0

        user32 = ctypes.windll.user32
        hwnd = int(window.winId())
        if not user32.RegisterHotKey(hwnd, HOTKEY_ID, 0, VK_F9):
            return False, (lambda: None)
        filt = _Filter(callback)
        app.installNativeEventFilter(filt)
        state = {"done": False}

        def uninstall():
            if state["done"]:
                return
            state["done"] = True
            try:
                app.removeNativeEventFilter(filt)
            except Exception:
                pass
            try:
                user32.UnregisterHotKey(hwnd, HOTKEY_ID)
            except Exception:
                pass

        return True, uninstall
    except Exception:
        return False, (lambda: None)


# ---------------------------------------------------------------- Qt 主界面（向导）

class _MainWindow:
    """QMainWindow 子类：关闭事件必须用子类重写 closeEvent 才生效。

    直接给窗口实例赋值 window.closeEvent = ... 在 PySide6 里不会被 C++
    事件分发调用，关窗口时就不会停 PresentMon，会留孤儿进程，
    所以这里必须用真正的子类（v1.1 踩过的坑）。
    """
    def __new__(cls, on_close):
        from PySide6.QtWidgets import QMainWindow
        class _W(QMainWindow):
            def closeEvent(self, event):
                on_close(event)
        return _W()


class RecorderWindow:
    def __init__(self, app):
        from PySide6.QtWidgets import (
            QApplication, QComboBox, QFrame, QHBoxLayout, QLabel,
            QPushButton, QStackedWidget, QVBoxLayout, QWidget,
        )
        from PySide6.QtCore import QLockFile, QSettings, Qt, QTimer

        self.app = app
        self.base_dir = app_dir()
        self.settings = QSettings("XiaoMa", "GameStutterRecorder")
        self.theme_mode = self.settings.value("theme", "system")
        self.dark = self._resolve_dark()
        self.pm_settings_path = self.settings.value("presentmon_path", "") or ""

        self.step = 1
        self.recording = False
        self.analyzing = False
        self.presentmon_proc = None
        self.sensor_thread = None
        self.sensor_stop_timed_out = False
        self.session_dir = None
        self.start_monotonic = None
        self.started_iso = ""
        self.report_path = None
        self.analysis_thread = None
        self.analysis_worker = None
        self.close_after_analysis = False
        self._pm_exit_noted = False
        self.hotkey_available = False
        self._hotkey_uninstall = lambda: None

        self.window = _MainWindow(self._on_close)
        self.window.setWindowTitle(f"游戏卡顿记录 {VERSION}")
        self.window.setFixedSize(700, 720)

        central = QWidget()
        central.setStyleSheet("background: transparent;")
        self.window.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 20, 28, 20)
        root.setSpacing(16)

        # 标题行：任务标题 + 主题切换
        title_row = QHBoxLayout()
        heading = QLabel("记录一次卡顿")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        title_row.addWidget(heading)
        title_row.addStretch()
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("toggle")
        self.theme_btn.clicked.connect(self._toggle_theme)
        title_row.addWidget(self.theme_btn)
        root.addLayout(title_row)

        subtitle = QLabel("跟着三步走，不用看说明书。")
        subtitle.setObjectName("muted")
        root.addWidget(subtitle)

        # 步骤指示：圆圈 + 文字 + 连接线，状态在 _refresh_steps 里刷
        self.step_circles = []
        self.step_labels = []
        self.step_lines = []
        steps_row = QHBoxLayout()
        steps_row.setSpacing(8)
        step_names = ["打开游戏", "开始记录", "停止看报告"]
        for i, name in enumerate(step_names):
            circle = QLabel(str(i + 1))
            circle.setFixedSize(36, 36)
            circle.setAlignment(Qt.AlignCenter)
            circle.setStyleSheet("font-size: 16px; font-weight: 700;")
            self.step_circles.append(circle)
            steps_row.addWidget(circle)
            lab = QLabel(name)
            lab.setStyleSheet("font-size: 15px;")
            self.step_labels.append(lab)
            steps_row.addWidget(lab)
            if i < 2:
                line = QFrame()
                line.setFrameShape(QFrame.HLine)
                line.setFixedHeight(2)
                self.step_lines.append(line)
                steps_row.addWidget(line, 1)
        steps_row.addStretch()
        root.addLayout(steps_row)

        # 卡片：里面是三页堆叠，步骤切换只换页，不换窗口
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 20, 24, 20)
        card_layout.setSpacing(10)
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        card_layout.addWidget(self.stack)

        # ---- 第 1 页：打开游戏 ----
        page1 = QWidget()
        page1.setStyleSheet("background: transparent;")
        p1 = QVBoxLayout(page1)
        p1.setContentsMargins(0, 0, 0, 0)
        p1.setSpacing(10)
        t1 = QLabel("先打开你的游戏")
        t1.setStyleSheet("font-size: 19px; font-weight: 700;")
        p1.addWidget(t1)
        s1 = QLabel("程序会自动找到正在运行的游戏，选好就能开始。")
        s1.setObjectName("muted")
        s1.setWordWrap(True)
        p1.addWidget(s1)
        self.game_combo = QComboBox()
        self.game_combo.setEditable(True)
        self.game_combo.setPlaceholderText("请选择或输入游戏 exe")
        # 互查 v1.2 注：QComboBox 自 5.15 就有 setPlaceholderText（Qt 官方文档），
        # 上一行不是崩溃原因；但可编辑框的提示要写在 lineEdit 上才显示，
        # 所以这里再补一行，两处都写，互不冲突。
        if self.game_combo.lineEdit() is not None:
            self.game_combo.lineEdit().setPlaceholderText("请选择或输入游戏 exe")
        self.game_combo.setMinimumHeight(36)
        p1.addWidget(self.game_combo)
        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.setObjectName("link")
        self.refresh_btn.clicked.connect(self.refresh_processes)
        p1.addWidget(self.refresh_btn)
        h1 = QLabel("没看到你的游戏？先把游戏打开，再点刷新。")
        h1.setObjectName("small")
        h1.setWordWrap(True)
        p1.addWidget(h1)
        self.start_btn = QPushButton("开始记录")
        self.start_btn.setObjectName("primary")
        self.start_btn.setMinimumHeight(48)
        self.start_btn.clicked.connect(self._primary_clicked)
        p1.addWidget(self.start_btn)
        self.stack.addWidget(page1)

        # ---- 第 2 页：正在记录 ----
        page2 = QWidget()
        page2.setStyleSheet("background: transparent;")
        p2 = QVBoxLayout(page2)
        p2.setContentsMargins(0, 0, 0, 0)
        p2.setSpacing(10)
        self.status_pill = QLabel("● 正在记录")
        self.status_pill.setObjectName("pill")
        p2.addWidget(self.status_pill)
        self.elapsed_label = QLabel("已记录 00:00")
        self.elapsed_label.setStyleSheet("font-size: 28px; font-weight: 700;")
        p2.addWidget(self.elapsed_label)
        self.recording_game_label = QLabel("")
        self.recording_game_label.setObjectName("muted")
        p2.addWidget(self.recording_game_label)
        h2 = QLabel("去玩吧，卡顿不用停，继续玩一会儿再回来。")
        h2.setObjectName("small")
        h2.setWordWrap(True)
        p2.addWidget(h2)
        self.stop_btn = QPushButton("停止并分析")
        self.stop_btn.setObjectName("primary")
        self.stop_btn.setMinimumHeight(48)
        self.stop_btn.clicked.connect(self._primary_clicked)
        p2.addWidget(self.stop_btn)
        self.stack.addWidget(page2)

        # ---- 第 3 页：停止看报告 ----
        page3 = QWidget()
        page3.setStyleSheet("background: transparent;")
        p3 = QVBoxLayout(page3)
        p3.setContentsMargins(0, 0, 0, 0)
        p3.setSpacing(10)
        t3 = QLabel("分析完了")
        t3.setStyleSheet("font-size: 19px; font-weight: 700;")
        p3.addWidget(t3)
        self.conclusion_label = QLabel("")
        self.conclusion_label.setStyleSheet("font-size: 17px; font-weight: 700;")
        self.conclusion_label.setWordWrap(True)
        p3.addWidget(self.conclusion_label)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("small")
        self.summary_label.setWordWrap(True)
        p3.addWidget(self.summary_label)
        self.open_report_btn = QPushButton("打开报告")
        self.open_report_btn.setObjectName("primary")
        self.open_report_btn.setMinimumHeight(48)
        self.open_report_btn.clicked.connect(self._open_report)
        p3.addWidget(self.open_report_btn)
        self.again_link = QPushButton("再录一次")
        self.again_link.setObjectName("link")
        self.again_link.clicked.connect(self._again)
        p3.addWidget(self.again_link)
        self.stack.addWidget(page3)

        root.addWidget(card)

        # 卡片下方：说明 + 设置链接（PresentMon 路径平时只在设置里）
        bottom_row = QHBoxLayout()
        self.note_label = QLabel("")
        self.note_label.setObjectName("muted")
        self.note_label.setWordWrap(True)
        bottom_row.addWidget(self.note_label, 1)
        settings_link = QPushButton("设置")
        settings_link.setObjectName("link")
        settings_link.clicked.connect(self._open_settings)
        bottom_row.addWidget(settings_link)
        root.addLayout(bottom_row)
        root.addStretch()

        self.timer = QTimer()
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

        # 进程列表等窗口先显示出来再刷新，不要在 show 之前同步跑 tasklist 白屏（v1.1 的坑）。
        QTimer.singleShot(0, self.refresh_processes)
        self._apply_theme()
        self._refresh_all()

    # -- 主题 ---------------------------------------------------------------
    def _resolve_dark(self):
        if self.theme_mode == "dark":
            return True
        if self.theme_mode == "light":
            return False
        return detect_system_dark()

    def _apply_theme(self):
        t = DARK if self.dark else LIGHT
        self.app.setStyleSheet(build_qss(t))
        self.theme_btn.setText("浅色" if self.dark else "深色")
        self._refresh_steps()

    def _toggle_theme(self):
        self.dark = not self.dark
        self.theme_mode = "dark" if self.dark else "light"
        self.settings.setValue("theme", self.theme_mode)
        self._apply_theme()

    # -- 步骤指示与界面状态 ---------------------------------------------------
    def _refresh_steps(self):
        t = DARK if self.dark else LIGHT
        for i in range(3):
            circle = self.step_circles[i]
            lab = self.step_labels[i]
            if i < self.step - 1:
                # 已完成：实心圆 + 对勾
                circle.setText("✓")
                circle.setStyleSheet(
                    f"background: {t['primary_bg']}; color: {t['primary_fg']}; "
                    "border-radius: 18px; font-size: 16px; font-weight: 700;")
                lab.setStyleSheet(f"color: {t['small']}; font-size: 15px;")
            elif i == self.step - 1:
                circle.setText(str(i + 1))
                circle.setStyleSheet(
                    f"background: {t['primary_bg']}; color: {t['primary_fg']}; "
                    "border-radius: 18px; font-size: 16px; font-weight: 700;")
                lab.setStyleSheet(f"color: {t['text']}; font-size: 15px;")
            else:
                circle.setText(str(i + 1))
                circle.setStyleSheet(
                    f"background: transparent; color: {t['muted']}; "
                    f"border: 1px solid {t['border']}; border-radius: 18px; "
                    "font-size: 16px;")
                lab.setStyleSheet(f"color: {t['muted']}; font-size: 15px;")
        for line in self.step_lines:
            line.setStyleSheet(f"background: {t['border']}; border: none;")

    def _f9_note(self, verb):
        """热键提示：注册失败时不写 F9，改写按钮操作，避免误导。"""
        if self.hotkey_available:
            return f"按 F9 {verb}，不用切出来"
        return "热键 F9 不可用（被占用），请切回程序点按钮"

    def _refresh_all(self):
        self.stack.setCurrentIndex(self.step - 1)
        self._refresh_steps()
        if self.step == 1:
            self.note_label.setText(
                "PresentMon 已一起装好，不用单独下载 · " + self._f9_note("开始或停止"))
            self.game_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
            self.start_btn.setEnabled(True)
            self.start_btn.setText("开始记录")
        elif self.step == 2:
            if self.analyzing:
                self.status_pill.setText("● 正在分析…")
                self.stop_btn.setText("分析中…")
                self.stop_btn.setEnabled(False)
                self.note_label.setText("正在分析，请稍等，分析完会自动打开报告。")
            else:
                self.status_pill.setText("● 正在记录")
                self.stop_btn.setText("停止并分析")
                self.stop_btn.setEnabled(True)
                self.note_label.setText(
                    self._f9_note("也能停止") + " · 录完自动打开报告，结论放最前面")
            self.game_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
        else:
            self.note_label.setText("报告已自动打开，也可以在这里再打开一次。")
            self.game_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)

    def _tick(self):
        if self.recording and self.start_monotonic is not None:
            sec = int(time.monotonic() - self.start_monotonic)
            self.elapsed_label.setText(f"已记录 {sec // 60:02d}:{sec % 60:02d}")
            # 互查 v1.2 补：PresentMon 因权限、参数或游戏退出提前没了时，
            # 别让界面一直假装在记录；只排一次停止分析，分析失败会如实弹窗。
            if (not self._pm_exit_noted and self.presentmon_proc is not None
                    and self.presentmon_proc.poll() is not None):
                self._pm_exit_noted = True
                self.note_label.setText("记录进程已退出，正在分析已录到的数据…")
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._primary_clicked)

    def install_hotkey(self):
        """窗口显示后再注册热键（需要窗口句柄），失败只改提示，不弹窗。"""
        ok, uninstall = _install_hotkey(self.window, self.app, self._hotkey_pressed)
        self.hotkey_available = ok
        self._hotkey_uninstall = uninstall
        self._refresh_all()

    def _hotkey_pressed(self):
        """F9 回调：只在步骤 1（已选游戏）和步骤 2（记录中）起作用，
        步骤 3 和分析中忽略，避免误触。
        互查 v1.2 修：不在原生事件过滤器的调用栈里直接做事（可能弹
        模态框重入事件循环），只做状态判断，动作排到主循环再跑。"""
        if self.analyzing:
            return
        from PySide6.QtCore import QTimer
        if self.step == 1:
            if self.game_combo.currentText().strip():
                QTimer.singleShot(0, self.start_recording)
        elif self.step == 2 and self.recording:
            QTimer.singleShot(0, self._primary_clicked)

    def refresh_processes(self):
        """刷新进程列表：有已选游戏时保留用户选择，否则自动选中一个。

        不默认选列表第一项（v1.1 的坑），自动选中走 choose_default_game，
        选不到就保持未选中 + 占位提示。
        互查 v1.2 修：用户手动输入的一律保留（以前输入不在列表里时
        会被自动选中覆盖掉）；自动选中时优先猜前台窗口的游戏。
        list_processes 本身已有 10 秒超时 + 异常返回空列表，
        这里再包一层，任何异常都只提示、不让界面崩掉。
        """
        try:
            procs = recorder.list_processes()
        except Exception:
            procs = []
        current = self.game_combo.currentText().strip()
        self.game_combo.clear()
        self.game_combo.addItems(procs)
        if current:
            self.game_combo.setCurrentText(current)
        elif self.step == 1 and not self.recording:
            default = choose_default_game(procs, foreground_process_name())
            if default:
                self.game_combo.setCurrentText(default)
            else:
                self.game_combo.setCurrentText("")
        if not procs and self.step == 1 and not self.recording:
            self.note_label.setText("进程列表读取失败，可手动输入游戏 exe 名。")

    def _primary_clicked(self):
        # 双击误停防护（v1.1 的坑）：开始后 1 秒内忽略停止点击。
        if self.recording and self.start_monotonic is not None:
            if time.monotonic() - self.start_monotonic < 1.0:
                return
        if self.analyzing:
            return
        if self.step == 2 and self.recording:
            self.stop_and_analyze()
        elif self.step == 1:
            self.start_recording()

    def _open_report(self):
        if self.report_path:
            webbrowser.open(str(self.report_path))

    def _again(self):
        self.report_path = None
        self.step = 1
        self._refresh_all()
        self.refresh_processes()

    # -- 记录 ---------------------------------------------------------------
    def _resolve_pm_or_warn(self):
        """按顺序找 PresentMon，找不到时弹一句话 + 两个去处，不贴报错代码。"""
        from PySide6.QtWidgets import QMessageBox
        pm = resolve_presentmon_path(self.base_dir, bundled_dir(), self.pm_settings_path)
        if pm is not None and pm.suffix.lower() == ".exe":
            return pm
        msg = QMessageBox(self.window)
        msg.setWindowTitle("找不到 PresentMon")
        msg.setText(
            "程序里没有找到 PresentMon，内置的也没有，设置里也没选。\n\n"
            "请去官方下载页下载 PresentMon，把里面的 PresentMon.exe "
            "放到这个程序同一文件夹，或者在设置里选一个。\n"
            f"官方地址：{recorder.PRESENTMON_URL}")
        btn_official = msg.addButton("去官方下载页", QMessageBox.AcceptRole)
        btn_repo = msg.addButton("官方仓库", QMessageBox.ActionRole)
        btn_settings = msg.addButton("打开设置", QMessageBox.ActionRole)
        msg.addButton("取消", QMessageBox.RejectRole)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == btn_official:
            webbrowser.open(recorder.PRESENTMON_URL)
        elif clicked == btn_repo:
            webbrowser.open("https://github.com/GameTechDev/PresentMon")
        elif clicked == btn_settings:
            self._open_settings()
        return None

    def start_recording(self):
        from PySide6.QtWidgets import QMessageBox
        game = self.game_combo.currentText().strip()
        if not game:
            QMessageBox.warning(self.window, "缺游戏名",
                                "请先选一个游戏，或者点“刷新列表”从列表里选一个，"
                                "也可以手动输入游戏 exe 名。")
            return
        pm = self._resolve_pm_or_warn()
        if pm is None:
            return
        now = datetime.now()
        # 互查 v1.2 修：\w 含中文，中文游戏名不再被压成一串下划线。
        safe_game = re.sub(r"[^\w.-]+", "_", Path(game).stem)
        self.session_dir = self.base_dir / "Records" / f"{now.strftime('%Y%m%d-%H%M%S')}-{safe_game}"
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self.window, "无法创建记录目录",
                                 f"无法写入：{self.session_dir}\n{e}\n\n"
                                 "请把程序移到你有写权限的文件夹，例如桌面或 D 盘。")
            return
        frames_path = self.session_dir / "frames.csv"
        sensors_path = self.session_dir / "sensors.csv"
        cmd = recorder.build_presentmon_command(pm, game, frames_path)
        try:
            kwargs = {}
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.presentmon_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        except Exception as e:
            QMessageBox.critical(self.window, "启动失败",
                                 f"启动 PresentMon 失败：{e}\n\n命令：{' '.join(cmd)}")
            return
        # 计时起点先定好再传给传感器线程，和界面计时用同一个值，对齐才准。
        # 传感器启动失败时先杀掉已启动的 PresentMon 再报错，不留孤儿进程（v1.1 的坑）。
        self.start_monotonic = time.monotonic()
        try:
            self.sensor_thread = recorder.SensorRecorder(sensors_path, self.start_monotonic)
            self.sensor_thread.start()
        except Exception as e:
            self._stop_processes()
            QMessageBox.critical(self.window, "启动失败", f"传感器线程启动失败：{e}")
            return
        self.started_iso = now.isoformat(timespec="seconds")
        self.sensor_stop_timed_out = False
        self._pm_exit_noted = False
        self.recording = True
        self.step = 2
        self.elapsed_label.setText("已记录 00:00")
        self.recording_game_label.setText(f"{game} · 正在记录中")
        self._refresh_all()
        self.timer.start()

    def _stop_processes(self):
        """停传感器线程和 PresentMon，带超时，超时就杀掉，不留孤儿进程。

        只对已启动的线程 join，join 后还活着就标记传感器超时；
        PresentMon kill 后再 wait 一次，确认真的退出了（v1.1 的坑）。
        """
        self.timer.stop()
        if self.sensor_thread is not None:
            try:
                self.sensor_thread.stop_event.set()
            except Exception:
                pass
            try:
                if self.sensor_thread.is_alive():
                    self.sensor_thread.join(timeout=3)
                if self.sensor_thread.is_alive():
                    self.sensor_stop_timed_out = True
            except Exception:
                pass
            self.sensor_thread = None
        if self.presentmon_proc is not None:
            try:
                self.presentmon_proc.terminate()
                self.presentmon_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self.presentmon_proc.kill()
                    self.presentmon_proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                try:
                    self.presentmon_proc.kill()
                    self.presentmon_proc.wait(timeout=2)
                except Exception:
                    pass
            self.presentmon_proc = None

    # -- 分析（后台线程，不阻塞界面） ------------------------------------------
    def stop_and_analyze(self):
        if not self.recording:
            return
        self.recording = False
        self._stop_processes()
        self.analyzing = True
        self._refresh_all()
        self._start_analysis_thread()

    def _start_analysis_thread(self):
        from PySide6.QtCore import QObject, QThread, Signal

        class AnalysisWorker(QObject):
            finished = Signal(str, str, str)  # 报告路径, 结论一句, 摘要一句
            failed = Signal(str)

            def __init__(self, frames_path, sensors_path, session_dir, game, started_iso, sensor_ok):
                super().__init__()
                self.frames_path = frames_path
                self.sensors_path = sensors_path
                self.session_dir = session_dir
                self.game = game
                self.started_iso = started_iso
                self.sensor_ok = sensor_ok

            def run(self):
                try:
                    report, main, summary = _do_analysis(
                        self.frames_path, self.sensors_path, self.session_dir,
                        self.game, self.started_iso, sensor_ok=self.sensor_ok)
                    self.finished.emit(str(report), main, summary)
                except Exception as e:
                    self.failed.emit(str(e))

        self.analysis_thread = QThread()
        self.analysis_worker = AnalysisWorker(
            self.session_dir / "frames.csv", self.session_dir / "sensors.csv",
            self.session_dir, self.game_combo.currentText().strip(),
            self.started_iso, sensor_ok=not self.sensor_stop_timed_out)
        self.analysis_worker.moveToThread(self.analysis_thread)
        self.analysis_thread.started.connect(self.analysis_worker.run)
        self.analysis_worker.finished.connect(self._on_analysis_finished)
        self.analysis_worker.failed.connect(self._on_analysis_failed)
        self.analysis_worker.finished.connect(self.analysis_thread.quit)
        self.analysis_worker.failed.connect(self.analysis_thread.quit)
        self.analysis_worker.finished.connect(self.analysis_worker.deleteLater)
        self.analysis_worker.failed.connect(self.analysis_worker.deleteLater)
        self.analysis_thread.finished.connect(self.analysis_thread.deleteLater)
        self.analysis_thread.start()

    def _on_analysis_finished(self, report_path, main, summary):
        sensor_note = ""
        if self.sensor_stop_timed_out:
            sensor_note = "（传感器未完全停止，传感器数据可能不完整）"
        self.analysis_thread = None
        self.analysis_worker = None
        self.analyzing = False
        self.report_path = Path(report_path)
        self.conclusion_label.setText(main)
        self.summary_label.setText(summary + sensor_note)
        self.step = 3
        self._refresh_all()
        # 向导第 3 步就是“停止看报告”：录完自动打开报告，不用用户再找文件。
        webbrowser.open(report_path)
        if self.close_after_analysis:
            self.close_after_analysis = False
            self.window.close()

    def _on_analysis_failed(self, message):
        from PySide6.QtWidgets import QMessageBox
        self.analysis_thread = None
        self.analysis_worker = None
        self.analyzing = False
        self.step = 1
        self._refresh_all()
        # 互查 v1.2 修：session_dir 极端情况下可能是 None，别拼出“还在：None”。
        if self.session_dir:
            detail = (f"分析失败：{message}\n\n原始数据还在：{self.session_dir}，"
                      "可以把 frames.csv 发给我看看。")
        else:
            detail = f"分析失败：{message}"
        QMessageBox.critical(self.window, "分析失败", detail)
        if self.close_after_analysis:
            self.close_after_analysis = False
            self.window.close()

    def _on_close(self, event):
        from PySide6.QtWidgets import QMessageBox
        # 分析正在跑时关窗口：不直接杀线程，先记下“分析完就关”。
        if self.analysis_thread is not None:
            self.close_after_analysis = True
            event.ignore()
            return
        if self.recording:
            ans = QMessageBox.question(
                self.window, "还在记录",
                "现在还在记录，关掉窗口会先停止记录并分析，然后退出，确定吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                event.ignore()
                return
            self.close_after_analysis = True
            try:
                self.stop_and_analyze()
            except Exception:
                self._stop_processes()
                self.window.close()
            event.ignore()
            return
        self._hotkey_uninstall()
        self._stop_processes()
        event.accept()

    # -- 设置（PresentMon 路径平时只在这里，不摆主界面） -----------------------
    def _open_settings(self):
        from PySide6.QtWidgets import (
            QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
            QPushButton, QVBoxLayout,
        )
        dialog = QDialog(self.window)
        dialog.setWindowTitle("设置")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        label = QLabel("PresentMon")
        label.setStyleSheet("font-weight: 700;")
        layout.addWidget(label)
        note = QLabel("平时不用改这里。程序会先用 exe 旁边的 PresentMon，"
                      "再用内置的，最后用这里选的。PresentMon 为 Intel 官方开源工具，"
                      "许可证以官方仓库为准。")
        note.setObjectName("small")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        resolved = resolve_presentmon_path(self.base_dir, bundled_dir(), self.pm_settings_path)
        pm_edit = QLineEdit(str(resolved) if resolved else str(self.base_dir / "PresentMon.exe"))
        row.addWidget(pm_edit, 1)
        choose_btn = QPushButton("选择")
        choose_btn.setObjectName("secondary")

        def choose():
            dlg = QFileDialog(dialog, "选择 PresentMon.exe")
            dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dlg.setNameFilter("exe (*.exe);;全部 (*.*)")
            dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
            if dlg.exec():
                files = dlg.selectedFiles()
                if files:
                    pm_edit.setText(files[0])

        choose_btn.clicked.connect(choose)
        row.addWidget(choose_btn)
        layout.addLayout(row)

        official_link = QPushButton("去 PresentMon 官方下载页")
        official_link.setObjectName("link")
        official_link.clicked.connect(lambda: webbrowser.open(recorder.PRESENTMON_URL))
        layout.addWidget(official_link)

        repo_link = QPushButton("PresentMon 官方仓库（GameTechDev/PresentMon）")
        repo_link.setObjectName("link")
        repo_link.clicked.connect(
            lambda: webbrowser.open("https://github.com/GameTechDev/PresentMon"))
        layout.addWidget(repo_link)

        btn_row = QHBoxLayout()
        restore_btn = QPushButton("恢复默认")
        restore_btn.setObjectName("link")
        restore_btn.clicked.connect(lambda: pm_edit.setText(str(self.base_dir / "PresentMon.exe")))
        btn_row.addWidget(restore_btn)
        btn_row.addStretch()
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primary")
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("secondary")
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        def save():
            from PySide6.QtWidgets import QMessageBox
            text = pm_edit.text().strip()
            default_text = str(self.base_dir / "PresentMon.exe")
            if text == "" or text == default_text:
                # 默认就是“exe 旁边优先、其次内置”，设置里不存路径
                self.pm_settings_path = ""
                self.settings.setValue("presentmon_path", "")
                dialog.accept()
                return
            p = Path(text)
            if not p.is_file() or p.suffix.lower() != ".exe":
                QMessageBox.warning(dialog, "请选择 .exe 文件",
                                    "请选择一个确实存在的 .exe 文件，或者点“恢复默认”。")
                return
            self.pm_settings_path = text
            self.settings.setValue("presentmon_path", text)
            dialog.accept()

        save_btn.clicked.connect(save)
        cancel_btn.clicked.connect(dialog.reject)
        dialog.exec()


def _ensure_stdout():
    """--windowed 打包后 exe 没有控制台，sys.stdout 可能是 None。

    直接 print 会报 AttributeError。先尝试附加父控制台，附不上就返回 False，
    调用方改用弹窗显示结果，不直接 print 崩溃（v1.1 的 P0-3 修复）。
    GUI 正常启动时不调这个函数，避免双击 exe 时附到别的控制台。
    """
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return True
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                return True
        except Exception:
            pass
    return False


def main():
    parser = argparse.ArgumentParser(description="游戏卡顿记录程序 向导版")
    parser.add_argument("--analyze", metavar="CSV", help="只分析一个已有的 CSV 文件，不启动界面")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()
    if args.version:
        if _ensure_stdout():
            print(VERSION)
        else:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication(sys.argv)
            QMessageBox.information(None, "游戏卡顿记录", f"版本：{VERSION}")
        return
    if args.analyze:
        has_stdout = _ensure_stdout()
        if not has_stdout and sys.stdout is None:
            # v1.1 遗留的小坑：--windowed 下 stdout 为 None 时，
            # recorder.analyze_file 里的 print 会直接崩溃，根本走不到后面的弹窗。
            # 先把 stdout 指到空设备，让分析能跑完，完了再用弹窗给报告路径。
            import os
            sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
            sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
        analysis, report = recorder.analyze_file(args.analyze)
        inject_report_dark_css(report)
        if not has_stdout:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication(sys.argv)
            webbrowser.open(str(report))
            QMessageBox.information(None, "分析完了", f"报告已生成：{report}")
        return

    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QLockFile
    import tempfile, os

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 单实例：第二个实例不抢 PresentMon（PresentMon 自带 -stop_existing_session，
    # 第二个实例一启动就会把第一个的采集停掉，所以必须挡住）。
    lock_path = os.path.join(tempfile.gettempdir(), "GameStutterRecorder.lock")
    lock = QLockFile(lock_path)
    if not lock.tryLock():
        QMessageBox.warning(None, "已经在运行", "游戏卡顿记录已经在运行了，不用再开一个。")
        return
    win = RecorderWindow(app)
    win.window.show()
    # 窗口显示后再注册热键（需要窗口句柄），失败只改提示，不弹窗。
    win.install_hotkey()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
