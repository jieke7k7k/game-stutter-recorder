#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏卡顿记录程序（简化版第一版，Windows 用）
================================================
这不是只有分析旧日志的工具，而是一个能在你玩游戏时记录的程序：

  1. 你先打开游戏，再在这个程序里选游戏 exe、点“开始记录”。
  2. 程序在后台做两件事：
     - 调用 PresentMon（Intel 官方开源工具，ETW 外部采集，不注入游戏）
       逐帧记录帧时间、GPU Busy，带时间戳；
     - 每 1 秒采一次系统数据：CPU 占用、内存占用、
       有 nvidia-smi 时再采 GPU 温度、功耗、频率、占用、显存。
  3. 你打完点“停止并分析”，程序自动把两份数据对齐，
     给出一句中文结论（例如“这次卡顿最像内存快满了”）并附上证据，
     自动在浏览器里打开报告。

限制要先说清楚（不藏着）：
  - Windows 本身不直接提供 CPU 温度，不装传感器驱动就读不到，
    所以第一版不承诺 CPU 温度，只用 CPU 占用、内存、GPU 温度功耗来判断。
  - PresentMon 需要你自己从官方下载，放在这个程序旁边，
    程序不会替你从不明网站下载 exe。
  - 结论永远是“最可能”，不是百分之百，因为卡顿可能几个原因叠在一起。

运行要求：Windows + Python 3.10 以上（安装时勾选 Add python to PATH）
           + PresentMon.exe 放在本程序同一文件夹。
用法：
  双击 start.bat，或者：python recorder.py
  分析旧文件（不录制）：python recorder.py --analyze 旧文件.csv
只用标准库，不用 pip 装任何东西。
"""

import argparse
import csv
import datetime
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

VERSION = "1.0.0-试用版"
PRESENTMON_URL = "https://github.com/GameTechDev/PresentMon"

# ---------------------------------------------------------------- CSV 工具

def _norm_header(h):
    return (h or "").lstrip("\ufeff").strip().lower().replace(" ", "")


def _num(v):
    try:
        if v is None:
            return float("nan")
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return float("nan")


def _is_nan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def _median(values):
    vals = sorted(v for v in values if not _is_nan(v))
    if not vals:
        return float("nan")
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _mean(values):
    vals = [v for v in values if not _is_nan(v)]
    return sum(vals) / len(vals) if vals else float("nan")


def _pct_low(fpss, p):
    vals = sorted(v for v in fpss if not _is_nan(v))
    if not vals:
        return float("nan")
    n = max(1, int(len(vals) * p))
    return _mean(vals[:n])


def _find_col(headers, tests):
    for test in tests:
        for i, h in enumerate(headers):
            if test(h):
                return i
    return -1


def parse_csv_file(path):
    """返回 (headers_normalized, rows)。rows 是原始字符串列表的列表。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if "".join(r).strip()]
    if not rows:
        raise ValueError("文件是空的")
    headers = [_norm_header(h) for h in rows[0]]
    return headers, rows[1:]


# ---------------------------------------------------------------- 帧数据解析

def parse_frames(headers, rows):
    """支持 PresentMon CSV 和旧式 FPS/FrameTime CSV，返回帧列表。"""
    frame_col = _find_col(headers, [
        lambda h: "msbetweendisplaychange" in h,
        lambda h: "msbetweenpresents" in h,
        lambda h: h == "frametime",
        lambda h: "frametime" in h,
    ])
    fps_col = _find_col(headers, [lambda h: h == "fps"])
    time_col = _find_col(headers, [lambda h: h == "timeinseconds"])
    gpu_busy_col = _find_col(headers, [
        lambda h: "msgpubusy" in h,
        lambda h: "msgpuactive" in h,
    ])
    gpu_load_col = _find_col(headers, [lambda h: "gpucoreload" in h])
    cpu_temp_col = _find_col(headers, [lambda h: "cputemp" in h])
    gpu_temp_col = _find_col(headers, [lambda h: "gputemp" in h])
    # 注意：系统内存列不能误认成显存列，GPU Memory Load 在表头顺序上在前，
    # 所以内存列必须排除含 gpu 的表头，否则会把 60% 左右的显存占用当成内存。
    mem_col = _find_col(headers, [lambda h: "memoryload" in h and "gpu" not in h])
    gpu_mem_col = _find_col(headers, [lambda h: "gpumemoryload" in h])
    gpu_clock_col = _find_col(headers, [lambda h: "gpucoreclock" in h])
    app_col = _find_col(headers, [lambda h: h == "application"])

    if frame_col < 0 and fps_col < 0:
        raise ValueError("没找到帧时间或 FPS 列：请用 PresentMon 导出的 CSV，或旧式带 FrameTime/FPS 的 CSV")

    frames = []
    for idx, r in enumerate(rows):
        def get(col):
            return _num(r[col]) if 0 <= col < len(r) else float("nan")
        ft = get(frame_col) if frame_col >= 0 else float("nan")
        fps = get(fps_col) if fps_col >= 0 else float("nan")
        if _is_nan(ft) and not _is_nan(fps) and fps > 0:
            ft = 1000.0 / fps
        if _is_nan(fps) and not _is_nan(ft) and ft > 0:
            fps = 1000.0 / ft
        if _is_nan(ft) or ft <= 0 or ft > 1000:
            continue
        frames.append({
            "idx": idx, "ft": ft, "fps": fps,
            "t": get(time_col) if time_col >= 0 else float("nan"),
            "gpu_busy": get(gpu_busy_col) if gpu_busy_col >= 0 else float("nan"),
            "gpu_load": get(gpu_load_col) if gpu_load_col >= 0 else float("nan"),
            "cpu_temp": get(cpu_temp_col) if cpu_temp_col >= 0 else float("nan"),
            "gpu_temp": get(gpu_temp_col) if gpu_temp_col >= 0 else float("nan"),
            "mem": get(mem_col) if mem_col >= 0 else float("nan"),
            "gpu_mem": get(gpu_mem_col) if gpu_mem_col >= 0 else float("nan"),
            "gpu_clock": get(gpu_clock_col) if gpu_clock_col >= 0 else float("nan"),
            "app": r[app_col].strip() if 0 <= app_col < len(r) else "",
        })
    if len(frames) < 10:
        raise ValueError("能用的帧数据太少（不到 10 行），请确认文件没有被截断")
    return frames


def parse_sensors(headers, rows):
    """解析本程序自己写的 sensors.csv，返回按 elapsed 秒排序的列表。"""
    elapsed_col = _find_col(headers, [lambda h: "elapsedsec" in h])
    mem_col = _find_col(headers, [lambda h: "memory_load_pct" in h or "memoryload" in h])
    gpu_temp_col = _find_col(headers, [lambda h: "gpu_temp_c" in h or "gputemp" in h])
    gpu_clock_col = _find_col(headers, [lambda h: "gpu_clock_mhz" in h or "gpucoreclock" in h])
    gpu_load_col = _find_col(headers, [lambda h: "gpu_load_pct" in h or "gpucoreload" in h])
    gpu_mem_col = _find_col(headers, [lambda h: "gpu_memory_used_mb" in h])
    out = []
    for r in rows:
        def get(col):
            return _num(r[col]) if 0 <= col < len(r) else float("nan")
        out.append({
            "elapsed": get(elapsed_col) if elapsed_col >= 0 else float("nan"),
            "mem": get(mem_col) if mem_col >= 0 else float("nan"),
            "gpu_temp": get(gpu_temp_col) if gpu_temp_col >= 0 else float("nan"),
            "gpu_clock": get(gpu_clock_col) if gpu_clock_col >= 0 else float("nan"),
            "gpu_load": get(gpu_load_col) if gpu_load_col >= 0 else float("nan"),
            "gpu_mem_used": get(gpu_mem_col) if gpu_mem_col >= 0 else float("nan"),
        })
    return [s for s in out if not _is_nan(s["elapsed"])]


# ---------------------------------------------------------------- 诊断逻辑

def classify(evidence):
    """evidence: dict，含 mem/gpu_temp/cpu_temp/gpu_busy_ratio/gpu_load/gpu_clock/med_clock/gpu_mem。
    返回 (原因, 证据一句话)。顺序是刻意排过的：内存和温度先于频率小波动，
    因为 GPU 频率在正常 boost 范围内本来就会上下浮动，不能一掉就判降频。"""
    mem = evidence.get("mem", float("nan"))
    gpu_temp = evidence.get("gpu_temp", float("nan"))
    cpu_temp = evidence.get("cpu_temp", float("nan"))
    busy = evidence.get("gpu_busy_ratio", float("nan"))
    gpu_load = evidence.get("gpu_load", float("nan"))
    gpu_clock = evidence.get("gpu_clock", float("nan"))
    med_clock = evidence.get("med_clock", float("nan"))
    gpu_mem = evidence.get("gpu_mem", float("nan"))

    if not _is_nan(mem) and mem >= 95:
        return "内存快满了", "卡顿那一刻内存占用在 95% 以上"
    if (not _is_nan(gpu_temp) and gpu_temp >= 85) or (not _is_nan(cpu_temp) and cpu_temp >= 90):
        return "温度过高，可能降频了", "卡顿那一刻温度已经很高"
    if not _is_nan(busy) and busy < 0.5:
        return "CPU 瓶颈，GPU 没吃饱", "卡顿那一帧 GPU 真正干活的时间不到帧时间的一半"
    if (not _is_nan(busy) and busy >= 0.9) or (not _is_nan(gpu_load) and gpu_load >= 95):
        return "GPU 瓶颈", "卡顿那一刻 GPU 已经接近满载"
    if not _is_nan(mem) and mem >= 90:
        return "内存偏高", "卡顿那一刻内存占用在 90% 以上"
    if (not _is_nan(gpu_temp) and gpu_temp >= 80) or (not _is_nan(cpu_temp) and cpu_temp >= 85):
        return "温度偏高", "卡顿那一刻温度已经偏高"
    if not _is_nan(gpu_mem) and gpu_mem >= 95:
        return "显存快满了", "卡顿那一刻显存占用在 95% 以上"
    if not _is_nan(gpu_clock) and not _is_nan(med_clock) and med_clock > 0 and gpu_clock < 0.6 * med_clock:
        return "GPU 频率掉了下来", "卡顿那一刻 GPU 频率只有平时的六成以下，不是正常小波动"
    return ("这次数据里看不出明确原因",
            "温度、内存、GPU 占用在卡顿那一刻都不算异常，可能是游戏加载、后台程序或网络，需要再录一局")


def analyze_frames(frames, sensors=None):
    """frames: parse_frames 的结果；sensors: parse_sensors 的结果或 None。
    返回分析 dict。传感器对齐是近似的（±几秒），报告里会明说。"""
    fts = [f["ft"] for f in frames]
    fpss = [f["fps"] for f in frames]
    med_ft = _median(fts)
    med_clock = _median([f["gpu_clock"] for f in frames])

    # 传感器时钟中位数（PresentMon 帧数据本身没有 GPU 频率时用它）
    sensor_clocks = [s["gpu_clock"] for s in (sensors or []) if not _is_nan(s["gpu_clock"])]
    if _is_nan(med_clock) and sensor_clocks:
        med_clock = _median(sensor_clocks)

    # 分组卡顿：连续的卡顿行算一次
    events, cur = [], None
    for f in frames:
        is_stutter = f["ft"] >= 33.3 or (f["ft"] >= 25 and f["ft"] >= 2 * med_ft)
        if is_stutter:
            if cur is None:
                cur = {"rows": [], "max_ft": 0, "peak": None}
            cur["rows"].append(f)
            if f["ft"] > cur["max_ft"]:
                cur["max_ft"] = f["ft"]
                cur["peak"] = f
        elif cur is not None:
            events.append(cur)
            cur = None
    if cur is not None:
        events.append(cur)

    # 每一次卡顿大约发生在开局后多少秒：用前面所有帧时间累加
    cum = []
    total = 0.0
    for f in frames:
        cum.append(total)
        total += f["ft"] / 1000.0

    for ev in events:
        peak = ev["peak"]
        approx_elapsed = cum[peak["idx"]] if peak["idx"] < len(cum) else 0.0
        ev["approx_elapsed"] = approx_elapsed
        # 证据：先用帧行自带的数据（旧式 CSV），再用最近的传感器样本补
        evidence = {
            "mem": peak["mem"], "gpu_temp": peak["gpu_temp"], "cpu_temp": peak["cpu_temp"],
            "gpu_busy_ratio": (peak["gpu_busy"] / peak["ft"]) if not _is_nan(peak["gpu_busy"]) else float("nan"),
            "gpu_load": peak["gpu_load"], "gpu_clock": peak["gpu_clock"],
            "med_clock": med_clock, "gpu_mem": peak["gpu_mem"],
        }
        if sensors:
            nearest = min(sensors, key=lambda s: abs(s["elapsed"] - approx_elapsed))
            if abs(nearest["elapsed"] - approx_elapsed) <= 3.0:
                if _is_nan(evidence["mem"]):
                    evidence["mem"] = nearest["mem"]
                if _is_nan(evidence["gpu_temp"]):
                    evidence["gpu_temp"] = nearest["gpu_temp"]
                if _is_nan(evidence["gpu_clock"]):
                    evidence["gpu_clock"] = nearest["gpu_clock"]
                if _is_nan(evidence["gpu_load"]):
                    evidence["gpu_load"] = nearest["gpu_load"]
                ev["sensor_diff_sec"] = abs(nearest["elapsed"] - approx_elapsed)
        cause, why = classify(evidence)
        ev["cause"] = cause
        ev["why"] = why
        ev["evidence"] = evidence

    from collections import Counter
    counts = Counter(ev["cause"] for ev in events)
    whys = {}
    for ev in events:
        whys.setdefault(ev["cause"], ev["why"])
    sorted_causes = sorted(counts.items(), key=lambda kv: -kv[1])
    top_cause, top_share = (sorted_causes[0][0], 0) if sorted_causes else ("没有检测到明显卡顿", 0)
    if events:
        top_share = round(counts[top_cause] / len(events) * 100)

    return {
        "frame_count": len(frames),
        "avg_fps": _mean(fpss),
        "low1": _pct_low(fpss, 0.01),
        "low01": _pct_low(fpss, 0.001),
        "max_ft": max(fts) if fts else float("nan"),
        "med_ft": med_ft,
        "mem_mean": _mean([f["mem"] for f in frames] + [s["mem"] for s in (sensors or [])]),
        "events": events,
        "cause_counts": dict(counts),
        "cause_whys": whys,
        "top_cause": top_cause,
        "top_share": top_share,
        "has_sensors": bool(sensors),
    }


def fmt(x, digits=1):
    return "没有这项数据" if _is_nan(x) else f"{x:.{digits}f}"


def main_conclusion(analysis, has_timestamp):
    events = analysis["events"]
    if not events:
        return ("这局记录里没有检测到明显卡顿，帧时间基本平稳。",
                "如果游戏里你明明感觉卡了，可能是记录没对上卡顿那段，或者卡顿来自网络、输入延迟这类帧时间看不出来的地方，下次卡的时候马上停下来保存那一段再测。")
    top = analysis["top_cause"]
    if top == "这次数据里看不出明确原因":
        return ("这次数据里看不出明确原因，卡顿确实存在，但温度、内存、GPU 占用在卡顿那一刻都不算异常。",
                "这不代表没问题，只代表这份数据不够。建议重新录一局，卡顿前后都录进去，再测一次。")
    text = f"这次卡顿最像：{top}。"
    note = (f"一共检测到 {len(events)} 次卡顿，其中 {analysis['top_share']}% 是这个原因。"
            f"{analysis['cause_whys'].get(top, '')}。下面有每次卡顿当时的数字，你可以对照着看。")
    if not has_timestamp:
        note += "另外提醒：这份文件里没有时间戳，只能按顺序看，不能精确还原卡顿发生在几点几分。"
    return text, note


# ---------------------------------------------------------------- 报告 HTML

def generate_report_html(session_dir, game_name, started_iso, analysis, has_timestamp):
    main, note = main_conclusion(analysis, has_timestamp)
    cause_rows = "".join(
        f"<tr><td>{html.escape(c)}</td><td>{n} 次</td><td style='color:#71717a'>{html.escape(analysis['cause_whys'].get(c, ''))}</td></tr>"
        for c, n in sorted(analysis["cause_counts"].items(), key=lambda kv: -kv[1])
    ) or "<tr><td colspan='3'>没有检测到卡顿。</td></tr>"
    top_events = sorted(analysis["events"], key=lambda e: -e["max_ft"])[:20]
    event_rows = "".join(
        f"<tr><td>第 {e['peak']['idx'] + 1} 行</td><td>{fmt(e['peak']['ft'])} 毫秒</td>"
        f"<td>{fmt(e['peak']['fps'])}</td><td>{html.escape(e['cause'])}</td></tr>"
        for e in top_events
    ) or "<tr><td colspan='4'>没有检测到卡顿。</td></tr>"
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>游戏卡顿诊断报告</title>
<style>
body{{margin:0;background:#fafafa;color:#18181b;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}}
.wrap{{max-width:880px;margin:0 auto;padding:32px 20px 48px}}
h1{{font-size:26px;margin:0 0 8px}} h2{{font-size:18px;margin:32px 0 12px}}
.box{{background:#fff;border:1px solid #e4e4e7;border-radius:12px;padding:24px}}
.conclusion{{background:#fff;border:1px solid #e4e4e7;border-left:4px solid #18181b;border-radius:12px;padding:24px}}
.conclusion p{{font-size:19px;font-weight:600;margin:0 0 8px}} .muted{{color:#71717a;font-size:14px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;color:#71717a;font-weight:500;padding:8px 8px 8px 0;border-bottom:1px solid #e4e4e7}}
td{{padding:10px 8px 10px 0;border-bottom:1px solid #f4f4f5;vertical-align:top}}
</style></head><body><div class="wrap">
<h1>游戏卡顿诊断报告</h1>
<p class="muted">游戏：{html.escape(game_name)}　开始时间：{html.escape(started_iso)}　记录程序版本：{VERSION}</p>
<h2>结论：只看这一句</h2>
<div class="conclusion"><p>{html.escape(main)}</p><p class="muted">{html.escape(note)}</p></div>
<h2>数据一览</h2><div class="box"><table>
<tr><td style="color:#71717a">总帧数</td><td>{analysis['frame_count']}</td></tr>
<tr><td style="color:#71717a">平均 FPS</td><td>{fmt(analysis['avg_fps'])}</td></tr>
<tr><td style="color:#71717a">1% Low FPS</td><td>{fmt(analysis['low1'])}</td></tr>
<tr><td style="color:#71717a">0.1% Low FPS</td><td>{fmt(analysis['low01'])}</td></tr>
<tr><td style="color:#71717a">最差一帧</td><td>{fmt(analysis['max_ft'])} 毫秒</td></tr>
<tr><td style="color:#71717a">卡顿次数</td><td>{len(analysis['events'])}</td></tr>
<tr><td style="color:#71717a">内存占用平均</td><td>{fmt(analysis['mem_mean'])}%</td></tr>
</table></div>
<h2>各种可能原因各占多少</h2><div class="box"><table>
<tr><th>可能原因</th><th>卡顿次数</th><th>说明</th></tr>{cause_rows}</table>
<p class="muted">一次卡顿只算一个最可能的原因，排第一的不等于唯一原因，几个原因叠在一起也很常见。传感器和帧数据的对齐是近似的（±几秒），因为它们是两个独立采集。</p></div>
<h2>最严重的卡顿</h2><div class="box"><table>
<tr><th>位置</th><th>帧时间</th><th>当时 FPS</th><th>最可能原因</th></tr>{event_rows}</table></div>
<p class="muted">本报告由游戏卡顿记录程序自动生成，数据只存在你的电脑上，没有上传。</p>
</div></body></html>
"""
    out = Path(session_dir) / "报告.html"
    out.write_text(page, encoding="utf-8")
    return out


# ---------------------------------------------------------------- 传感器采集（Windows）

class CpuMemSampler:
    """Windows 下用 ctypes 读 CPU 占用和内存占用；非 Windows 返回空值，不崩溃。"""
    def __init__(self):
        self._prev_idle = None
        self._prev_total = None
        self._kernel32 = None
        if sys.platform == "win32":
            try:
                import ctypes
                self._ctypes = ctypes
                self._kernel32 = ctypes.windll.kernel32
            except Exception:
                self._kernel32 = None

    def _system_times(self):
        import ctypes
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not self._kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        def to_int(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return to_int(idle), to_int(kernel) + to_int(user)

    def sample(self):
        cpu_pct = float("nan")
        mem_pct = float("nan")
        if sys.platform == "win32" and self._kernel32 is not None:
            try:
                times = self._system_times()
                if times is not None:
                    idle, total = times
                    if self._prev_idle is not None and total > self._prev_total:
                        cpu_pct = max(0.0, min(100.0, 100.0 * (1.0 - (idle - self._prev_idle) / (total - self._prev_total))))
                    self._prev_idle, self._prev_total = idle, total
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if self._kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    mem_pct = float(stat.dwMemoryLoad)
            except Exception:
                pass
        else:
            # 非 Windows 或取不到时，尝试 psutil（如果装了的话），否则留空
            try:
                import psutil  # type: ignore
                cpu_pct = float(psutil.cpu_percent(interval=None))
                mem_pct = float(psutil.virtual_memory().percent)
            except Exception:
                pass
        return cpu_pct, mem_pct


def sample_gpu_nvidia_smi():
    """有 nvidia-smi 时返回 GPU 数据，否则全空。只读数据，不改任何设置。"""
    empty = {"temp": float("nan"), "power": float("nan"), "clock": float("nan"),
             "load": float("nan"), "mem_used": float("nan"), "mem_total": float("nan")}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,clocks.sm,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
        if r.returncode != 0 or not r.stdout.strip():
            return empty
        parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
        if len(parts) < 6:
            return empty
        return {"temp": _num(parts[0]), "power": _num(parts[1]), "clock": _num(parts[2]),
                "load": _num(parts[3]), "mem_used": _num(parts[4]), "mem_total": _num(parts[5])}
    except Exception:
        return empty


class SensorRecorder(threading.Thread):
    def __init__(self, out_path, start_monotonic):
        super().__init__(daemon=True)
        self.out_path = Path(out_path)
        self.start_monotonic = start_monotonic
        self.stop_event = threading.Event()
        self.sampler = CpuMemSampler()
        self.count = 0

    def run(self):
        with open(self.out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Timestamp", "ElapsedSec", "CPU_Load_Pct", "Memory_Load_Pct",
                        "GPU_Temp_C", "GPU_Power_W", "GPU_Clock_MHz", "GPU_Load_Pct",
                        "GPU_Memory_Used_MB", "GPU_Memory_Total_MB"])
            while not self.stop_event.wait(1.0):
                cpu_pct, mem_pct = self.sampler.sample()
                gpu = sample_gpu_nvidia_smi()
                elapsed = time.monotonic() - self.start_monotonic
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                def v(x):
                    return "" if _is_nan(x) else round(float(x), 1)
                w.writerow([ts, round(elapsed, 1), v(cpu_pct), v(mem_pct),
                            v(gpu["temp"]), v(gpu["power"]), v(gpu["clock"]), v(gpu["load"]),
                            v(gpu["mem_used"]), v(gpu["mem_total"])])
                f.flush()
                self.count += 1


# ---------------------------------------------------------------- 进程列表（Windows tasklist）

def list_processes():
    """返回 exe 名列表。Windows 用 tasklist，非 Windows 返回空列表。"""
    if sys.platform != "win32":
        return []
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10)
        names = []
        for line in r.stdout.splitlines():
            m = re.match(r'"([^"]+\.exe)"', line.strip(), re.IGNORECASE)
            if m and m.group(1) not in names:
                names.append(m.group(1))
        return sorted(names, key=str.lower)
    except Exception:
        return []


def build_presentmon_command(presentmon_exe, game_exe, output_file):
    return [str(presentmon_exe), "-process_name", game_exe,
            "-output_file", str(output_file), "-no_top", "-stop_existing_session"]


# ---------------------------------------------------------------- 命令行：只分析旧文件

def analyze_file(input_path, output_dir=None):
    headers, rows = parse_csv_file(input_path)
    frames = parse_frames(headers, rows)
    sensors = None
    # 如果同目录下有同名 sensors.csv，顺带一起用
    cand = Path(input_path).with_name("sensors.csv")
    if cand.exists():
        try:
            sh, sr = parse_csv_file(cand)
            sensors = parse_sensors(sh, sr)
        except Exception:
            sensors = None
    analysis = analyze_frames(frames, sensors)
    out_dir = Path(output_dir) if output_dir else Path(input_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    report = generate_report_html(out_dir, Path(input_path).stem,
                                  datetime.datetime.now().isoformat(timespec="seconds"),
                                  analysis, has_timestamp=False)
    main, _ = main_conclusion(analysis, has_timestamp=False)
    print(main)
    print(f"帧数：{analysis['frame_count']}，平均 FPS：{fmt(analysis['avg_fps'])}，"
          f"1% Low：{fmt(analysis['low1'])}，卡顿：{len(analysis['events'])} 次")
    print(f"报告已生成：{report}")
    return analysis, report


# ---------------------------------------------------------------- GUI（Windows 主界面）

class RecorderApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.filedialog, self.messagebox, self.ttk = tk, filedialog, messagebox, ttk
        self.root = root
        self.root.title(f"游戏卡顿记录 {VERSION}")
        self.root.geometry("580x520")
        self.root.resizable(False, False)

        self.base_dir = Path(__file__).resolve().parent
        self.presentmon_proc = None
        self.sensor_thread = None
        self.session_dir = None
        self.start_monotonic = None
        self.started_iso = ""
        self.recording = False
        self.elapsed_after_id = None

        tk.Label(root, text="游戏卡顿记录", font=("Microsoft YaHei", 18, "bold")).pack(pady=(16, 2), anchor="w", padx=20)
        tk.Label(root, text="先打开游戏，再选游戏、点开始记录。打完点停止，自动出结论。",
                 fg="#71717a", wraplength=540, justify="left").pack(anchor="w", padx=20)

        frm = tk.Frame(root)
        frm.pack(fill="x", padx=20, pady=12)
        tk.Label(frm, text="游戏 exe：").grid(row=0, column=0, sticky="w")
        self.game_var = tk.StringVar()
        self.game_entry = tk.Entry(frm, textvariable=self.game_var, width=38)
        self.game_entry.grid(row=0, column=1, padx=6)
        tk.Button(frm, text="刷新进程", command=self.refresh_processes).grid(row=0, column=2)
        tk.Label(frm, text="例如：游戏进程名，一般以 .exe 结尾。不知道名字就点刷新，在下拉里找。").grid(row=1, column=1, columnspan=2, sticky="w")
        self.proc_box = ttk.Combobox(frm, width=36, state="readonly")
        self.proc_box.grid(row=2, column=1, padx=6, pady=4, sticky="w")
        self.proc_box.bind("<<ComboboxSelected>>", lambda e: self.game_var.set(self.proc_box.get()))

        tk.Label(frm, text="PresentMon：").grid(row=3, column=0, sticky="w")
        self.pm_var = tk.StringVar(value=str(self.base_dir / "PresentMon.exe"))
        tk.Entry(frm, textvariable=self.pm_var, width=38).grid(row=3, column=1, padx=6)
        tk.Button(frm, text="选择", command=self.choose_pm).grid(row=3, column=2)
        tk.Button(frm, text="去官方下载页", command=lambda: webbrowser.open(PRESENTMON_URL)).grid(row=4, column=1, sticky="w", padx=6, pady=2)

        self.status_var = tk.StringVar(value="还没开始。先打开游戏，再点开始记录。")
        tk.Label(root, textvariable=self.status_var, wraplength=540, justify="left", fg="#18181b").pack(anchor="w", padx=20, pady=6)
        self.time_var = tk.StringVar(value="已记录：0 秒")
        tk.Label(root, textvariable=self.time_var, fg="#71717a").pack(anchor="w", padx=20)

        btn_frm = tk.Frame(root)
        btn_frm.pack(pady=14)
        self.start_btn = tk.Button(btn_frm, text="开始记录", width=16, bg="#18181b", fg="white", command=self.start_recording)
        self.start_btn.pack(side="left", padx=6)
        self.stop_btn = tk.Button(btn_frm, text="停止并分析", width=16, state="disabled", command=self.stop_and_analyze)
        self.stop_btn.pack(side="left", padx=6)
        tk.Button(btn_frm, text="打开记录文件夹", width=16, command=self.open_records).pack(side="left", padx=6)

        tk.Label(root, text="说明：记录时程序在后台运行，不注入游戏。CPU 温度在 Windows 下不装驱动读不到，"
                 "第一版用 CPU 占用、内存、GPU 温度功耗来判断；结论是“最可能”，不是百分之百。",
                 wraplength=540, justify="left", fg="#71717a").pack(padx=20, pady=6)
        self.refresh_processes()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def refresh_processes(self):
        procs = list_processes()
        self.proc_box["values"] = procs
        if procs and not self.game_var.get():
            self.proc_box.set(procs[0])
            self.game_var.set(procs[0])

    def choose_pm(self):
        p = self.filedialog.askopenfilename(title="选择 PresentMon.exe", filetypes=[("exe", "*.exe"), ("全部", "*.*")])
        if p:
            self.pm_var.set(p)

    def open_records(self):
        folder = self.base_dir / "Records"
        folder.mkdir(exist_ok=True)
        webbrowser.open(str(folder))

    def _tick(self):
        if self.recording and self.start_monotonic is not None:
            sec = int(time.monotonic() - self.start_monotonic)
            self.time_var.set(f"已记录：{sec} 秒")
            self.elapsed_after_id = self.root.after(500, self._tick)

    def start_recording(self):
        game = self.game_var.get().strip()
        pm = Path(self.pm_var.get().strip())
        if not game:
            self.messagebox.showerror("缺游戏名", "请先填游戏 exe 名，或者点“刷新进程”从列表里选一个。")
            return
        if not pm.exists():
            self.messagebox.showerror("找不到 PresentMon",
                "在程序旁边没找到 PresentMon.exe。\n\n请先去官方下载页下载 PresentMon，"
                "把里面的 PresentMon.exe 解压到这个程序同一文件夹，再点开始记录。\n"
                f"官方地址：{PRESENTMON_URL}")
            return
        now = datetime.datetime.now()
        safe_game = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(game).stem)
        self.session_dir = self.base_dir / "Records" / f"{now.strftime('%Y%m%d-%H%M%S')}-{safe_game}"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        frames_path = self.session_dir / "frames.csv"
        sensors_path = self.session_dir / "sensors.csv"
        cmd = build_presentmon_command(pm, game, frames_path)
        try:
            kwargs = {}
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.presentmon_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        except Exception as e:
            self.messagebox.showerror("启动失败", f"启动 PresentMon 失败：{e}\n\n命令：{' '.join(cmd)}")
            return
        self.start_monotonic = time.monotonic()
        self.started_iso = now.isoformat(timespec="seconds")
        self.sensor_thread = SensorRecorder(sensors_path, self.start_monotonic)
        self.sensor_thread.start()
        self.recording = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set(f"正在记录：{game}。去玩游戏吧，打完回到这个窗口点“停止并分析”。\n记录保存在：{self.session_dir}")
        self._tick()

    def stop_and_analyze(self):
        if not self.recording:
            return
        self.recording = False
        if self.elapsed_after_id:
            try:
                self.root.after_cancel(self.elapsed_after_id)
            except Exception:
                pass
        if self.sensor_thread is not None:
            self.sensor_thread.stop_event.set()
            self.sensor_thread.join(timeout=3)
        if self.presentmon_proc is not None:
            try:
                self.presentmon_proc.terminate()
                self.presentmon_proc.wait(timeout=5)
            except Exception:
                try:
                    self.presentmon_proc.kill()
                except Exception:
                    pass
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        frames_path = self.session_dir / "frames.csv"
        sensors_path = self.session_dir / "sensors.csv"
        if not frames_path.exists() or frames_path.stat().st_size == 0:
            self.status_var.set("PresentMon 没有写出帧数据。可能原因：游戏进程名写错了，或者记录时间太短，"
                                "或者 PresentMon 没有权限。先确认游戏当时确实在运行，再重新录一次。")
            self.messagebox.showerror("没有帧数据", "frames.csv 是空的，分析不了。\n请确认游戏 exe 名没写错，游戏在记录时确实在运行。")
            return
        try:
            headers, rows = parse_csv_file(frames_path)
            frames = parse_frames(headers, rows)
            sensors = None
            if sensors_path.exists():
                try:
                    sh, sr = parse_csv_file(sensors_path)
                    sensors = parse_sensors(sh, sr)
                except Exception:
                    sensors = None
            analysis = analyze_frames(frames, sensors)
            report = generate_report_html(self.session_dir, self.game_var.get().strip(),
                                          self.started_iso, analysis, has_timestamp=True)
            main, _ = main_conclusion(analysis, has_timestamp=True)
            self.status_var.set(f"分析完了：{main}\n报告已打开，文件在：{report}")
            webbrowser.open(str(report))
        except Exception as e:
            self.status_var.set(f"分析失败：{e}")
            self.messagebox.showerror("分析失败", f"分析失败：{e}\n\n原始数据还在：{self.session_dir}，可以把 frames.csv 发给我看看。")

    def on_close(self):
        if self.recording:
            if not self.messagebox.askyesno("还在记录", "现在还在记录，关掉窗口会先停止记录再退出，确定吗？"):
                return
            try:
                self.stop_and_analyze()
            except Exception:
                pass
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="游戏卡顿记录程序")
    parser.add_argument("--analyze", metavar="CSV", help="只分析一个已有的 CSV 文件，不启动界面")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return
    if args.analyze:
        analyze_file(args.analyze)
        return
    import tkinter as tk
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
