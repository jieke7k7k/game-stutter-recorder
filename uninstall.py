#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3 干净卸载：算出要删的东西 + 自查清单，不动用户没同意的东西。

原则（用户定版）：
- 默认保留记录（Records/），另有“全部删除”一键选项才删记录。
- 只清本程序 Wrapper 装的 WinRing0 驱动：以程序文件夹下的
  lhm_driver_installed.txt 标记为准；无标记时不拆驱动，清单里明写。
- 本程序不建计划任务，自查里该项应为空；若发现同名任务，列出来不自动删。
- 卸载脚本本身在程序退出后由批处理执行（exe 运行时删不掉自己），
  本模块只负责算清单和写批处理，不直接删运行中的 exe。
只用标准库；非 Windows 下路径计算仍可测试，实际删除只在 Windows 执行。
"""
import os
from pathlib import Path

REGISTRY_KEY = r"HKCU\Software\XiaoMa\GameStutterRecorder"
DRIVER_MARKER = "lhm_driver_installed.txt"


def uninstall_plan(app_dir, records_dir, delete_records=False):
    """返回 (delete_paths, keep_paths, checks) 三个列表，供界面展示确认用。"""
    app_dir = Path(app_dir)
    records_dir = Path(records_dir)
    # 互查 P1-8/P2-7 修：history.db 在 Records/ 下，不是程序根目录；
    # 驱动标记文件不属于用户记录，保留记录时也要删，避免重装误判。
    delete = [
        str(app_dir / "GameStutterRecorder.exe"),
        str(app_dir / "PresentMon.exe"),
        str(app_dir / "LhmWrapper.exe"),
        str(app_dir / DRIVER_MARKER),
        "桌面/开始菜单快捷方式（若存在）",
    ]
    keep = []
    if delete_records:
        delete.append(str(records_dir))
        delete.append(str(records_dir / "history.db"))
    else:
        keep.append(str(records_dir))
    checks = [
        ("程序文件（不含 Records）", str(app_dir)),
        ("设置注册表项", REGISTRY_KEY),
        ("温度驱动服务 WinRing0", "仅当存在 lhm_driver_installed.txt 标记时清除，否则保留并明示"),
        ("计划任务", "本程序不建计划任务，应为空"),
    ]
    return delete, keep, checks


def driver_installed_by_us(app_dir):
    return (Path(app_dir) / DRIVER_MARKER).exists()


def write_uninstall_batch(app_dir, records_dir, delete_records, batch_path):
    """写一个 Windows 批处理：等程序退出后删文件、清注册表、删驱动（仅标记时）。

    互查 P0-5/P0-6 修：
    - 批处理用 Windows mbcs 编码 + CRLF 写，中文/空格路径不乱码；
    - 保留记录时删程序文件夹下除 Records/ 外的全部文件与子目录
      （含依赖 dll、标记文件），不只删三个 exe；快捷方式若存在也删；
    - 全部删除时整个程序文件夹连同 Records 一起删。
    调用方应在程序退出前启动它（带延迟），批处理用 ping 延迟 2 秒后执行，
    避免删到还没退出的 exe。
    """
    app_dir = Path(app_dir)
    records_dir = Path(records_dir)
    lines = [
        "@echo off",
        "ping -n 3 127.0.0.1 >nul",
        f'reg delete "{REGISTRY_KEY}" /f',
    ]
    if driver_installed_by_us(app_dir):
        lines += [
            "sc stop WinRing0 >nul 2>&1",
            "sc delete WinRing0 >nul 2>&1",
        ]
    if delete_records:
        lines.append(f'rmdir /s /q "{app_dir}"')
    else:
        # 删除 Records 外的全部文件与子目录（for 循环变量在批处理里用 %%）
        lines.append(f'for %%F in ("{app_dir}\\*") do if /I not "%%~nxF"=="Records" del /f /q "%%F"')
        lines.append(f'for /D %%D in ("{app_dir}\\*") do if /I not "%%~nxD"=="Records" rmdir /s /q "%%D"')
        lines.append(f'del /f /q "{app_dir}\\{DRIVER_MARKER}"')
    # 快捷方式（若用户手动建过且在默认位置，删；不存在时 del 报错被忽略）
    lines.append('del /f /q "%USERPROFILE%\\Desktop\游戏卡顿记录.lnk" >nul 2>&1')
    lines.append('del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\游戏卡顿记录.lnk" >nul 2>&1')
    lines.append("del %0")
    text = "\r\n".join(lines) + "\r\n"
    try:
        data = text.encode("mbcs")
    except Exception:
        data = text.encode("utf-8")
    Path(batch_path).write_bytes(data)
    return str(batch_path)
