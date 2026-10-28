#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向导版 v1.2.0 测试。

跑法：python tests/test_wizard.py
不依赖 PySide6（app_wizard 顶层不 import Qt，只有 GUI 路径才 import），
所以在没有显示环境的机器上也能跑纯函数和分析一轮的断言。
不破坏已有的 tests/test_regression.py（它仍断言 app_qt 1.1.0）。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import recorder
import app_wizard


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} 失败：{detail}")
    print(f"OK {name}")


def main():
    # 1. 版本
    check("app_wizard 版本是 1.2.0", app_wizard.VERSION == "1.2.0", app_wizard.VERSION)

    # 2. 自动选中游戏：过滤系统进程，不默认选第一项
    check("自动选中过滤系统进程后选游戏",
          app_wizard.choose_default_game(
              ["System", "svchost.exe", "explorer.exe", "MyGame.exe"]) == "MyGame.exe")
    check("全是系统进程时返回空",
          app_wizard.choose_default_game(["svchost.exe", "explorer.exe"]) == "")
    check("空列表时返回空", app_wizard.choose_default_game([]) == "")
    check("大小写不敏感过滤",
          app_wizard.choose_default_game(["SVCHOST.EXE", "Game.exe"]) == "Game.exe")
    check("记录程序自身不被选中",
          app_wizard.choose_default_game(["GameStutterRecorder.exe", "Other.exe"]) == "Other.exe")
    # 互查 v1.2 补：优先猜前台窗口的游戏（只比进程名，不读窗口标题）
    check("前台游戏优先于列表第一个",
          app_wizard.choose_default_game(
              ["Other.exe", "MyGame.exe"], foreground="MyGame.exe") == "MyGame.exe")
    check("前台是系统进程时忽略前台",
          app_wizard.choose_default_game(
              ["MyGame.exe"], foreground="explorer.exe") == "MyGame.exe")
    check("前台不在列表里时回落到原来逻辑",
          app_wizard.choose_default_game(
              ["MyGame.exe"], foreground="NotRunning.exe") == "MyGame.exe")
    check("完整路径也按文件部分过滤",
          app_wizard.choose_default_game(
              [r"C:\Windows\System32\svchost.exe", "Game.exe"]) == "Game.exe")
    check("前台传完整路径也能匹配",
          app_wizard.choose_default_game(
              ["Game.exe"], foreground=r"D:\Games\Game.exe") == "Game.exe")
    check("非 Windows 上前台进程名为空",
          app_wizard.foreground_process_name() == "" if sys.platform != "win32" else True)

    # 3. PresentMon 查找顺序：旁边 > 内置 > 设置 > 没有
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "base"
        bundled = Path(td) / "bundled"
        settings_dir = Path(td) / "settings"
        base.mkdir()
        bundled.mkdir()
        settings_dir.mkdir()
        (bundled / "PresentMon.exe").write_bytes(b"MZ")
        check("旁边没有、内置有时用内置",
              app_wizard.resolve_presentmon_path(base, bundled, None) == bundled / "PresentMon.exe")
        (base / "PresentMon.exe").write_bytes(b"MZ")
        check("旁边有时优先旁边",
              app_wizard.resolve_presentmon_path(base, bundled, None) == base / "PresentMon.exe")
        (settings_dir / "CustomPM.exe").write_bytes(b"MZ")
        check("旁边和内置都没有、设置有时用设置",
              app_wizard.resolve_presentmon_path(
                  Path(td) / "empty", None, str(settings_dir / "CustomPM.exe"))
              == settings_dir / "CustomPM.exe")
        check("三个都没有时返回空",
              app_wizard.resolve_presentmon_path(Path(td) / "empty", None, "") is None)
        check("设置空字符串不被当成当前目录",
              app_wizard.resolve_presentmon_path(Path(td) / "empty", None, "") is None)

    # 4. 深色报告注入（v1.1 的 P0-1 修复不要回退）
    css = app_wizard.DARK_CSS_FOR_REPORT
    check("深色 CSS 覆盖 th", "th { color: #a1a1aa" in css)
    check("深色 CSS 覆盖内联 td[style]", "td[style] { color: #d4d4d8 !important" in css)
    check("深色 CSS 覆盖 .muted", ".muted { color: #a1a1aa" in css)
    check("深色 CSS 覆盖结论竖线", ".conclusion { border-left-color: #fafafa" in css)

    # 5. 两套主题 QSS 都能拼出来
    check("浅色 QSS 有白卡片", "#ffffff" in app_wizard.build_qss(app_wizard.LIGHT))
    check("深色 QSS 有锌 800 卡片", "#27272a" in app_wizard.build_qss(app_wizard.DARK))

    # 6. 合成样本走一轮向导分析：报告生成、结论和摘要非空
    sample = ROOT / "tests" / "sample_presentmon.csv"
    with tempfile.TemporaryDirectory() as td:
        session = Path(td) / "session"
        session.mkdir()
        report, main, summary = app_wizard._do_analysis(
            sample, None, session, "SampleGame", "2026-01-01T00:00:00")
        check("向导分析生成报告", Path(report).exists(), report)
        check("向导分析结论非空", isinstance(main, str) and len(main) > 0, main)
        check("向导分析摘要含帧数", "帧数" in summary, summary)
        check("报告已注入深色样式",
              "prefers-color-scheme: dark" in Path(report).read_text(encoding="utf-8"))

    # 7. 互查 v1.2 补：带传感器数据的分析（传感器路径不断言 GUI，只断言分析层）
    with tempfile.TemporaryDirectory() as td:
        session = Path(td) / "session"
        session.mkdir()
        sensors = Path(td) / "sensors.csv"
        sensors.write_text(
            "Timestamp,ElapsedSec,CPU_Load_Pct,Memory_Load_Pct,GPU_Temp_C,"
            "GPU_Clock_MHz,GPU_Load_Pct,GPU_Memory_Used_MB\n"
            "2026-01-01T00:00:00,0,30,50,60,1800,70,4000\n"
            "2026-01-01T00:00:01,1,35,52,61,1800,72,4100\n",
            encoding="utf-8")
        report, main, summary = app_wizard._do_analysis(
            sample, sensors, session, "SampleGame", "2026-01-01T00:00:00")
        check("带传感器也能生成报告", Path(report).exists(), report)
        check("带传感器结论非空", isinstance(main, str) and len(main) > 0, main)
        check("传感器未停干净时当没有传感器数据",
              app_wizard._do_analysis(
                  sample, sensors, session, "SampleGame", "2026-01-01T00:00:00",
                  sensor_ok=False)[0] is not None)

    print("向导版测试全部通过")


if __name__ == "__main__":
    main()
