#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏卡顿记录程序 Qt 版（v1.1.0）
================================
界面按用户 2026-10-25 晚确认的 Apple 评审版样品做：
  - 标题栏只写程序名，页面标题是任务“记录一次卡顿”，不重名；
  - 下拉用系统 chevron 箭头，不用字母 v 冒充；
  - 录制时只留一个主按钮“停止并分析”，“打开记录文件夹”降级为文字链接；
  - 小字说明用更深的灰色，保证对比度；
  - 卡片高度按内容收紧，不留大段空白。

深色模式（用户 2026-10-25 晚新增要求）：
  - 启动时先跟随系统（Qt 6.5+ 的 colorScheme，有就用，没有就默认浅色）；
  - 右上角“深色 / 浅色”按钮可以手动切换，手动选择会记住（QSettings）；
  - 深色不是简单反色：背景锌 900、卡片锌 800、边框锌 700、文字近白、
    小字用锌 300、主按钮在深色下是白底黑字，保证对比度；
  - 生成的报告 HTML 也会注入 prefers-color-scheme 深色样式，
    浏览器跟随系统深色时报告也是深色，不刺眼。

记录和分析逻辑一律复用 recorder.py，不复制、不改核心（除互查 P1-11 的 tasklist 编码一处，
见 recorder.py 里那段注释，改动只为中文 exe 名不被静默吞掉）：
  - PresentMon（ETW 外部采集，不注入游戏）逐帧；
  - 每 1 秒传感器采样（CPU 占用、内存、GPU 温度功耗）；
  - 结论永远是“最可能”，不是百分之百。
  - CPU 温度在 Windows 下不装驱动读不到，第一版不承诺。

互查（DeepSeek-V4.1-Flash，2026-10-26）修过的问题，写在这里提醒以后的人：
  - P0-1 深色报告：原注入 CSS 盖不住内联 style='color:#71717a'，已补 td[style] !important 和 th 颜色。
  - P0-2 GUI 线程阻塞：分析已移到 QThread + worker，界面线程只负责状态和打开报告。
  - P0-3 --windowed stdout：exe 没有控制台时 sys.stdout 可能是 None，--version/--analyze
    先尝试 AttachConsole，附不上就用弹窗显示结果，不直接 print 崩溃。
  - P1 单实例、关闭杀子进程、路径、权限、双击误停等，见各处注释。

打包注意（用户立的软件规矩）：
  - 正式发版前必须先让另一个 AI 独立互查，互查提示词先给用户过目批准，
    P0 修完并复跑测试通过才许打包；本文件在互查通过前只能算开发版。
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

VERSION = "1.1.0"
# 报告 HTML 里写的是 recorder.VERSION，这里改成 Qt 版的版本号，
# 这样报告里不会还写着 1.0.0-试用版。
# 注意：直接跑 python recorder.py --analyze 生成的报告仍是 1.0.0-试用版，
# 那是 Tkinter 版的版本号，不是 bug，两个版本共用一个核心文件。
recorder.VERSION = VERSION


def app_dir():
    """程序所在文件夹。

    注意 Tkinter 版的一个坑：PyInstaller --onefile 打包后，
    Path(__file__).parent 指向的是临时解压目录，不是 exe 所在目录，
    Records 和 PresentMon 都会找错地方。Qt 版这里修掉：
    打包后用 sys.executable 所在目录，没打包时用源码目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------- 深色 / 浅色主题

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
    """根据主题字典拼 QSS。Qt 的 QSS 不支持变量，所以每次切换主题就重新拼一份重设。

    下拉箭头说明：这里不自定义 QComboBox::down-arrow 图片，
    用 Fusion 系统箭头（系统 chevron，不是字母 v），避免 --add-data 和 QtSvg 插件依赖。
    样品图里的 ∨ 是示意图，实际以系统箭头为准，用户已确认的样品要求是“不用字母 v”，
    不是“必须画成 ∨ 形”。
    """
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


# P0-1 修：原注入 CSS 盖不住内联 style='color:#71717a'（th 和数据表左侧标签、原因说明），
# 深色下 #71717a 在 #27272a 上对比度只有约 3.1:1，低于 4.5:1，所以必须用 !important 覆盖内联样式，
# 并把 th、结论左侧竖线也改成深色下能看清的颜色。
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
    """报告 HTML 本来只有浅色样式，这里追加深色媒体查询，不改 recorder.py 核心。

    失败也不影响分析结果：报告已经生成了，深色只是锦上添花，静默跳过。
    """
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


# ---------------------------------------------------------------- 分析（P0-2：必须在后台线程跑，不碰任何 QWidget）

def _do_analysis(frames_path, sensors_path, session_dir, game, started_iso, sensor_ok=True):
    """纯函数：解析、分析、生成报告、注入深色样式，返回 (报告路径, 结论一句)。

    只接收路径和字符串，不碰界面，QThread worker 直接调它。
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
    main, _ = recorder.main_conclusion(analysis, has_timestamp=True)
    return report, main


# ---------------------------------------------------------------- Qt 主界面

class _MainWindow:
    """QMainWindow 子类：关闭事件必须用子类重写 closeEvent 才生效。

    直接给窗口实例赋值 window.closeEvent = ... 在 PySide6 里不会被 C++ 事件分发调用，
    关窗口时就不会停 PresentMon，会留孤儿进程——这是常见 Bug 预查里“关闭≠退出”那一条，
    所以这里必须用真正的子类。
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
            QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
            QLineEdit, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
        )
        from PySide6.QtCore import QLockFile, QSettings, Qt, QTimer
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        self._widgets = (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                         QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
                         QVBoxLayout, QWidget, QLockFile, QSettings, Qt, QTimer,
                         QDesktopServices, QUrl)
        (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
         QLineEdit, QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
         QLockFile, QSettings, Qt, QTimer, QDesktopServices, QUrl) = self._widgets

        self.app = app
        self.base_dir = app_dir()
        self.settings = QSettings("XiaoMa", "GameStutterRecorder")
        self.theme_mode = self.settings.value("theme", "system")  # system / light / dark
        self.dark = self._resolve_dark()

        self.presentmon_proc = None
        self.sensor_thread = None
        self.sensor_stop_timed_out = False
        self.session_dir = None
        self.start_monotonic = None
        self.started_iso = ""
        self.recording = False
        self.analysis_thread = None
        self.analysis_worker = None
        self.close_after_analysis = False

        self.window = _MainWindow(self._on_close)
        self.window.setWindowTitle(f"游戏卡顿记录 {VERSION}")
        self.window.setFixedSize(640, 620)

        central = QWidget()
        self.window.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 20, 28, 20)
        root.setSpacing(16)

        # 标题行：任务标题 + 主题切换（录制时也随时可切，不影响记录）
        title_row = QHBoxLayout()
        self.heading = QLabel("记录一次卡顿")
        self.heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        title_row.addWidget(self.heading)
        title_row.addStretch()
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("toggle")
        self.theme_btn.clicked.connect(self._toggle_theme)
        title_row.addWidget(self.theme_btn)
        root.addLayout(title_row)

        subtitle = QLabel("先打开游戏，再选游戏、点开始记录。打完点停止，自动出结论。")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # 状态条：录制中 / 未开始，只显示当前状态，不堆两个状态
        self.status_label = QLabel()
        self.status_label.setObjectName("pill")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # 卡片：游戏 + PresentMon
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 20, 24, 20)
        card_layout.setSpacing(10)

        game_label = QLabel("游戏")
        game_label.setStyleSheet("font-weight: 700;")
        card_layout.addWidget(game_label)

        self.game_combo = QComboBox()
        self.game_combo.setEditable(True)
        self.game_combo.setPlaceholderText("请选择或输入游戏 exe")
        self.game_combo.setMinimumHeight(36)
        card_layout.addWidget(self.game_combo)

        self.refresh_btn = QPushButton("刷新进程列表")
        self.refresh_btn.setObjectName("link")
        self.refresh_btn.clicked.connect(self.refresh_processes)
        card_layout.addWidget(self.refresh_btn)

        hint = QLabel("例如：游戏进程名，一般以 .exe 结尾。不知道就点刷新，在下拉里找。")
        hint.setObjectName("small")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)

        pm_label = QLabel("PresentMon")
        pm_label.setStyleSheet("font-weight: 700;")
        card_layout.addWidget(pm_label)

        pm_row = QHBoxLayout()
        self.pm_edit = QLineEdit(str(self.base_dir / "PresentMon.exe"))
        pm_row.addWidget(self.pm_edit, 1)
        choose_btn = QPushButton("选择")
        choose_btn.setObjectName("secondary")
        choose_btn.clicked.connect(self._choose_pm)
        pm_row.addWidget(choose_btn)
        card_layout.addLayout(pm_row)

        pm_link = QPushButton("去 PresentMon 官方下载页")
        pm_link.setObjectName("link")
        pm_link.clicked.connect(lambda: webbrowser.open(recorder.PRESENTMON_URL))
        card_layout.addWidget(pm_link)
        root.addWidget(card)

        # 按钮行：录制时只留主按钮，文件夹操作降级为文字链接（Apple 评审版定下的）
        btn_row = QHBoxLayout()
        self.main_btn = QPushButton()
        self.main_btn.setObjectName("primary")
        self.main_btn.setMinimumWidth(180)
        self.main_btn.clicked.connect(self._main_button_clicked)
        btn_row.addWidget(self.main_btn)
        folder_link = QPushButton("打开记录文件夹")
        folder_link.setObjectName("link")
        folder_link.clicked.connect(self.open_records)
        btn_row.addWidget(folder_link)
        btn_row.addStretch()
        root.addLayout(btn_row)

        note = QLabel("记录时程序在后台运行，不注入游戏。CPU 温度在 Windows 下不装驱动读不到，"
                      "第一版用 CPU 占用、内存、GPU 温度功耗来判断；结论是“最可能”，不是百分之百。")
        note.setObjectName("small")
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch()

        self.timer = QTimer()
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)

        # P1-1 修：进程列表不在 show 之前同步跑 tasklist（最多 10 秒白屏），
        # 等事件循环启动、窗口先显示出来再刷新。
        QTimer.singleShot(0, self.refresh_processes)
        self._apply_theme()
        self._refresh_ui_state()

    # -- 主题 ---------------------------------------------------------------
    def _resolve_dark(self):
        if self.theme_mode == "dark":
            return True
        if self.theme_mode == "light":
            return False
        return detect_system_dark()

    def _apply_theme(self):
        # P1-8 修：QSS 设到 QApplication 上，不只设主窗口，
        # 这样非原生文件对话框、弹窗也跟随手动切换的深色，不会系统浅色、程序深色两张皮。
        t = DARK if self.dark else LIGHT
        self.app.setStyleSheet(build_qss(t))
        # 按钮文字永远是“切到另一边”，不写当前状态，避免用户看不懂
        self.theme_btn.setText("浅色" if self.dark else "深色")

    def _toggle_theme(self):
        self.dark = not self.dark
        self.theme_mode = "dark" if self.dark else "light"
        self.settings.setValue("theme", self.theme_mode)
        self._apply_theme()

    # -- 界面状态 -------------------------------------------------------------
    def _refresh_ui_state(self):
        if self.recording:
            game = self.game_combo.currentText().strip()
            self.status_label.setText(f"● 正在记录　{game}　·　已记录 0 秒")
            self.main_btn.setText("停止并分析")
            self.game_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
        else:
            self.status_label.setText("● 未开始记录　先打开游戏，再点开始记录。")
            self.main_btn.setText("开始记录")
            self.main_btn.setEnabled(True)
            self.game_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)

    def _tick(self):
        if self.recording and self.start_monotonic is not None:
            sec = int(time.monotonic() - self.start_monotonic)
            game = self.game_combo.currentText().strip()
            self.status_label.setText(f"● 正在记录　{game}　·　已记录 {sec} 秒")

    def refresh_processes(self):
        # P1-1 修：不再默认选中列表第一个进程（以前会默认选到 ApplicationFrameHost.exe 之类，
        # “游戏名为空”的校验永远触发不了，用户可能录错进程）。
        # 现在留空 + 占位提示，只有用户明确选了或输入了才允许开始。
        procs = recorder.list_processes()
        current = self.game_combo.currentText()
        self.game_combo.clear()
        self.game_combo.addItems(procs)
        if current:
            self.game_combo.setCurrentText(current)
        if not procs and not self.recording:
            self.status_label.setText("● 进程列表读取失败，可手动输入游戏 exe 名。")

    def _choose_pm(self):
        # P1-8 修：用非原生文件对话框，这样手动深色下对话框也深色，
        # 原生对话框只跟随系统主题，不跟随手动切换。
        from PySide6.QtWidgets import QFileDialog
        dialog = QFileDialog(self.window, "选择 PresentMon.exe")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setNameFilter("exe (*.exe);;全部 (*.*)")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        if dialog.exec():
            files = dialog.selectedFiles()
            if files:
                self.pm_edit.setText(files[0])

    def open_records(self):
        from PySide6.QtWidgets import QMessageBox
        folder = self.base_dir / "Records"
        try:
            folder.mkdir(exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self.window, "无法打开记录文件夹",
                                 f"无法写入：{folder}\n{e}\n\n请把程序移到你有写权限的文件夹，例如桌面或 D 盘。")
            return
        webbrowser.open(str(folder))

    def _main_button_clicked(self):
        # P1-12 修：双击“开始记录”会马上变成“停止并分析”，第二次点击会直接停掉，
        # 录不到一秒就停。开始后 1 秒内忽略停止点击，防双击误停。
        if self.recording and self.start_monotonic is not None:
            if time.monotonic() - self.start_monotonic < 1.0:
                return
        if self.analysis_thread is not None:
            return
        if self.recording:
            self.stop_and_analyze()
        else:
            self.start_recording()

    # -- 记录 ---------------------------------------------------------------
    def start_recording(self):
        from PySide6.QtWidgets import QMessageBox
        game = self.game_combo.currentText().strip()
        pm = Path(self.pm_edit.text().strip())
        if not game:
            QMessageBox.warning(self.window, "缺游戏名",
                                "请先填游戏 exe 名，或者点“刷新进程列表”从列表里选一个。")
            return
        # P1-2 修：以前只判断 exists()，输入框清空时 Path("") 就是当前目录，exists() 为 True，
        # 后面 Popen 才报奇怪的启动失败。现在必须是一个 .exe 文件才往下走。
        if not pm.is_file():
            QMessageBox.warning(self.window, "找不到 PresentMon",
                                "请选择 PresentMon.exe 文件，不是文件夹。\n\n"
                                "请先去官方下载页下载 PresentMon，把里面的 PresentMon.exe "
                                "解压到这个程序同一文件夹，再点开始记录。\n"
                                f"官方地址：{recorder.PRESENTMON_URL}")
            return
        if pm.suffix.lower() != ".exe":
            QMessageBox.warning(self.window, "找不到 PresentMon",
                                "请选择 .exe 文件。\n\n"
                                f"官方地址：{recorder.PRESENTMON_URL}")
            return
        now = datetime.now()
        safe_game = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(game).stem)
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
        # P1-4 修：传感器线程启动失败时，以前 PresentMon 已经跑起来了，但 recording 还是 False，
        # 再点开始会再起一个 PresentMon。现在传感器没起来就先杀掉 PresentMon 再报错返回。
        # 计时起点先定好再传给传感器线程，和界面计时用同一个值，对齐才准。
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
        self.recording = True
        self._refresh_ui_state()
        self.timer.start()

    def _stop_processes(self):
        """停传感器线程和 PresentMon，带超时，超时就杀掉，不留孤儿进程。

        P1-3 修：以前 join(timeout=3) 的返回值被忽略，线程没停也被置 None，
        随后立刻读 sensors.csv 可能读到半截文件。现在：
        - 只对已启动的线程 join，join 后还活着就标记传感器超时；
        - PresentMon kill 后再 wait 一次，确认真的退出了。
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

    # -- 分析（P0-2：后台线程） ------------------------------------------------
    def stop_and_analyze(self):
        if not self.recording:
            return
        self.recording = False
        self._stop_processes()
        self._refresh_ui_state()
        # 停下来先显示“正在分析”，主按钮禁用，分析完再恢复，避免重复触发和界面假死。
        self.status_label.setText("● 正在分析…，请稍等，分析完会自动打开报告。")
        self.main_btn.setText("分析中…")
        self.main_btn.setEnabled(False)
        self.game_combo.setEnabled(False)
        self._start_analysis_thread()

    def _start_analysis_thread(self):
        from PySide6.QtCore import QObject, QThread, Signal

        class AnalysisWorker(QObject):
            finished = Signal(str, str)  # 报告路径, 结论一句
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
                    report, main = _do_analysis(
                        self.frames_path, self.sensors_path, self.session_dir,
                        self.game, self.started_iso, sensor_ok=self.sensor_ok)
                    self.finished.emit(str(report), main)
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

    def _on_analysis_finished(self, report_path, main):
        from PySide6.QtWidgets import QMessageBox
        sensor_note = ""
        if self.sensor_stop_timed_out:
            sensor_note = "（传感器未完全停止，传感器数据可能不完整）"
        self.analysis_thread = None
        self.analysis_worker = None
        self._refresh_ui_state()
        self.status_label.setText(f"● 分析完了：{main}{sensor_note}")
        webbrowser.open(report_path)
        if self.close_after_analysis:
            self.close_after_analysis = False
            self.window.close()

    def _on_analysis_failed(self, message):
        from PySide6.QtWidgets import QMessageBox
        self.analysis_thread = None
        self.analysis_worker = None
        self._refresh_ui_state()
        self.status_label.setText(f"● 分析失败：{message}")
        QMessageBox.critical(self.window, "分析失败",
                             f"分析失败：{message}\n\n原始数据还在：{self.session_dir}，"
                             "可以把 frames.csv 发给我看看。")
        if self.close_after_analysis:
            self.close_after_analysis = False
            self.window.close()

    def _on_close(self, event):
        from PySide6.QtWidgets import QMessageBox
        # 分析正在跑时关窗口：不直接杀线程（worker 不碰界面，安全），
        # 先记下“分析完就关”，分析完的回调里会关窗口，避免关到一半崩溃。
        if self.analysis_thread is not None:
            self.close_after_analysis = True
            event.ignore()
            return
        if self.recording:
            # P1-6 修：以前文案只说“先停止记录再退出”，实际还会分析并打开浏览器，
            # 文案和行为不一致。现在文案写清楚会分析，行为和文案一致。
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
        self._stop_processes()
        event.accept()


def _ensure_stdout():
    """P0-3 修：--windowed 打包后 exe 没有控制台，sys.stdout 可能是 None。

    直接 print 会报 AttributeError。先尝试附加父控制台（CI 里跑 --version 时就是这种情况），
    附不上就返回 False，调用方改用弹窗显示结果，不直接 print。
    GUI 正常启动时不调这个函数，避免双击 exe 时莫名其妙附到别的控制台。
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
    parser = argparse.ArgumentParser(description="游戏卡顿记录程序 Qt 版")
    parser.add_argument("--analyze", metavar="CSV", help="只分析一个已有的 CSV 文件，不启动界面")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()
    if args.version:
        if _ensure_stdout():
            print(VERSION)
        else:
            # 没有控制台时用弹窗显示版本，不崩溃
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication(sys.argv)
            QMessageBox.information(None, "游戏卡顿记录", f"版本：{VERSION}")
        return
    if args.analyze:
        has_stdout = _ensure_stdout()
        analysis, report = recorder.analyze_file(args.analyze)
        # P1-9 修：--analyze 路径以前不注入深色 CSS，生成的报告只有浅色。
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
    app.setStyle("Fusion")  # Fusion 在 Windows 和深色下都稳定，不用系统原生样式忽明忽暗
    # 单实例：第二个实例不抢 PresentMon（PresentMon 自带 -stop_existing_session，
    # 第二个实例一启动就会把第一个的采集停掉，所以必须挡住）。
    lock_path = os.path.join(tempfile.gettempdir(), "GameStutterRecorder.lock")
    lock = QLockFile(lock_path)
    if not lock.tryLock():
        QMessageBox.warning(None, "已经在运行", "游戏卡顿记录已经在运行了，不用再开一个。")
        return
    win = RecorderWindow(app)
    win.window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
