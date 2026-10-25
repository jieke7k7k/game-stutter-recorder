#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qt 版回归测试（P1-10 修：CI 不能只验证“没崩”，要断言结论不变）。

跑法：python tests/test_regression.py
不依赖 PySide6（app_qt 顶层不 import Qt，只有 GUI 路径才 import），
所以在没有显示环境的 CI Linux 上也能跑核心断言；Windows 打包后另有 --version 冒烟。

断言来源：
- tests/sample_presentmon.csv：合成 300 帧，正常 8ms，100-109 帧 50ms 且 GPU Busy 低，
  应检出 1 次卡顿，最可能原因是 CPU 瓶颈（GPU 没吃饱）。
- 内存列防误认：表头里 GPU Memory Load 在系统 Memory Load 之前出现时，
  系统内存必须取系统那一列，不能取显存那一列（v1.0 踩过的 bug）。
- 旧日志实测值（7290 帧、121.8、27.3、55 次、内存偏高）是用户私人日志的本地实测，
  不随测试文件进仓库，CI 里不直接断言它；本地复跑时用 /tmp 副本另行核对，见 README-Qt。
- 深色报告：注入后 th、td[style]、.muted 在深色下必须有覆盖，否则报告深色看不清（P0-1）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import recorder
import app_qt


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} 失败：{detail}")
    print(f"OK {name}")


def main():
    # 1. 版本一致
    check("app_qt 版本是 1.1.0", app_qt.VERSION == "1.1.0", app_qt.VERSION)

    # 2. 合成 PresentMon 样本
    sample = ROOT / "tests" / "sample_presentmon.csv"
    if not sample.exists():
        # 兼容仓库里 tests 在根目录的情况（GitHub 仓 tests/sample_presentmon.csv）
        sample = ROOT.parent / "tests" / "sample_presentmon.csv"
    headers, rows = recorder.parse_csv_file(sample)
    frames = recorder.parse_frames(headers, rows)
    analysis = recorder.analyze_frames(frames, None)
    check("样本卡顿次数是 1", len(analysis["events"]) == 1, len(analysis["events"]))
    check("样本最可能原因是 CPU 瓶颈", "CPU" in analysis["top_cause"], analysis["top_cause"])

    # 3. 内存列防误认
    headers = [h.lower().replace(" ", "") for h in
               ["FPS", "FrameTime", "GPU Memory Load", "Memory Load"]]
    # parse_frames 要求至少 10 帧才不报错，所以用 12 行重复数据，只看内存列取值
    rows = [["120", "8.3", "57", "95"], ["30", "50.0", "64", "96"]] * 6
    frames = recorder.parse_frames(headers, rows)
    check("系统内存取的是 95/96，不是显存 57/64",
          abs(frames[0]["mem"] - 95) < 0.01 and abs(frames[1]["mem"] - 96) < 0.01,
          [f["mem"] for f in frames])
    check("显存仍取 GPU 那一列",
          abs(frames[0]["gpu_mem"] - 57) < 0.01, frames[0]["gpu_mem"])

    # 4. 深色报告注入（P0-1）
    css = app_qt.DARK_CSS_FOR_REPORT
    check("深色 CSS 覆盖 th", "th { color: #a1a1aa" in css)
    check("深色 CSS 覆盖内联 td[style]", "td[style] { color: #d4d4d8 !important" in css)
    check("深色 CSS 覆盖 .muted", ".muted { color: #a1a1aa" in css)
    check("深色 CSS 覆盖结论竖线", ".conclusion { border-left-color: #fafafa" in css)

    # 5. 两套主题 QSS 都能拼出来，且深色卡片不是浅色
    light_qss = app_qt.build_qss(app_qt.LIGHT)
    dark_qss = app_qt.build_qss(app_qt.DARK)
    check("浅色 QSS 有白卡片", "#ffffff" in light_qss)
    check("深色 QSS 有锌 800 卡片", "#27272a" in dark_qss)
    check("深色 QSS 有下拉弹层样式", "QAbstractItemView" in dark_qss)
    check("深色 QSS 有禁用态样式", ":disabled" in dark_qss)

    print("回归测试全部通过")


if __name__ == "__main__":
    main()
