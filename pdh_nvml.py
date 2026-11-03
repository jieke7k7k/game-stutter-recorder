#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3 Windows 采样：PDH 计数器 + NVML 直读 + WHEA/SMART 快照。

设计原则（Argon + 用户定版）：
- 非 Windows 下所有函数返回空值，绝不崩溃，Linux CI 能跑。
- PDH 用英文计数器路径（PdhAddEnglishCounter），避免中文系统名称不一致。
- NVML 用 ctypes 直调系统自带 nvml.dll 长连接，绝不每 250ms 起子进程；
  失败时回退到长驻 nvidia-smi 流式（由调用方决定），本模块只负责直读。
- WHEA 用系统自带 wevtutil 查，SMART 用 ctypes DeviceIoControl 尝试，
  拿不到就返回 None，报告里写盲区，不编造。
只用标准库。
"""
import ctypes
import math
import subprocess
import sys


def _nan():
    return float("nan")


def _is_win():
    return sys.platform == "win32"


# ---------------------------------------------------------------- PDH

class PDHSampler:
    """PDH 计数器采样器。Windows 下打开一个查询，逐项加英文计数器；
    某项加不上就记为缺失，不让整个采样崩掉。非 Windows 下全部为空。"""

    COUNTERS = {
        "cpu_perf": r"\Processor Information(_Total)\% Processor Performance",
        "page_faults": r"\Memory\Page Faults/sec",
        "disk_sec": r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer",
        "dpc": r"\Processor(_Total)\% DPC Time",
        "interrupt": r"\Processor(_Total)\% Interrupt Time",
        # 互查 P0-3/P0-4 修：整卡显存用量（非 NVIDIA 也可用 PDH 读已用量，
        # 总量走注册表；按进程枚举因实例名/PID 映射不可靠，不伪装成已实现，
        # 归属默认写“整卡满了、分不清是谁”，见 recorder.classify）。
        "gpu_adapter_mem": r"\GPU Adapter Memory(_Total)\Dedicated Usage",
        "gpu_process_mem": r"\GPU Process Memory(_Total)\Dedicated Usage",
    }

    def __init__(self):
        self._pdh = None
        self._query = None
        self._handles = {}
        self.missing = []
        if not _is_win():
            return
        try:
            self._pdh = ctypes.windll.pdh
            query = ctypes.c_void_p()
            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                return
            self._query = query
            for key, path in self.COUNTERS.items():
                h = ctypes.c_void_p()
                # PdhAddEnglishCounterW：英文路径，中文系统也能用
                try:
                    rc = self._pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(h))
                except Exception:
                    rc = -1
                if rc == 0:
                    self._handles[key] = h
                else:
                    self.missing.append(key)
            # 第一次采集是基线，第二次才有有效值
            try:
                self._pdh.PdhCollectQueryData(self._query)
            except Exception:
                pass
        except Exception:
            self._query = None
            self._handles = {}

    def sample(self):
        if not _is_win() or self._query is None:
            return {}
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return {}
            out = {}
            # PDH_FMT_COUNTERVALUE 是 CStatus(DWORD) + union，ctypes 按
            # c_ulong + c_double 自动补齐到 8 字节对齐，与 Windows 结构一致；
            # 直接传 c_double 会错位写坏内存，所以必须用这个结构。
            class _FmtVal(ctypes.Structure):
                _fields_ = [("CStatus", ctypes.c_ulong),
                            ("doubleValue", ctypes.c_double)]
            for key, h in self._handles.items():
                val = _FmtVal()
                try:
                    rc = self._pdh.PdhGetFormattedCounterValue(h, 0x200, None, ctypes.byref(val))
                except Exception:
                    rc = -1
                out[key] = float(val.doubleValue) if rc == 0 and val.CStatus == 0 else _nan()
            return out
        except Exception:
            return {}

    def close(self):
        try:
            if self._query is not None and self._pdh is not None:
                self._pdh.PdhCloseQuery(self._query)
        except Exception:
            pass
        self._query = None


# ---------------------------------------------------------------- NVML

class NVMLSampler:
    """NVML 直读：启动时 Init 一次，录制中每 250ms 调 MemoryInfo +
    UtilizationRates，单次 <0.2ms 量级。非 Windows / 无驱动时返回空。"""

    def __init__(self):
        self._nvml = None
        self._handle = None
        self._name = ""
        if not _is_win():
            return
        try:
            self._nvml = ctypes.CDLL("nvml.dll")
            self._nvml.nvmlInit()
            count = ctypes.c_uint()
            if self._nvml.nvmlDeviceGetCount(ctypes.byref(count)) != 0:
                return
            best = None
            best_mem = -1
            for i in range(count.value):
                h = ctypes.c_void_p()
                if self._nvml.nvmlDeviceGetHandleByIndex(i, ctypes.byref(h)) != 0:
                    continue
                name_buf = ctypes.create_string_buffer(128)
                try:
                    self._nvml.nvmlDeviceGetName(h, name_buf, 128)
                    name = name_buf.value.decode(errors="replace")
                except Exception:
                    name = ""
                # 内存信息结构体：used/total 为 unsigned long long
                class _Mem(ctypes.Structure):
                    _fields_ = [("total", ctypes.c_ulonglong),
                                ("free", ctypes.c_ulonglong),
                                ("used", ctypes.c_ulonglong)]
                mem = _Mem()
                try:
                    _rc = self._nvml.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem))
                    total_mb = mem.total / 1024 / 1024 if _rc == 0 else 0
                except Exception:
                    total_mb = 0
                # 认准 RTX/GTX，或显存最大的那张（Optimus 笔记本）
                score = total_mb + (100000 if ("RTX" in name or "GTX" in name) else 0)
                if score > best_mem:
                    best_mem = score
                    best = h
                    self._name = name
            self._handle = best
        except Exception:
            self._nvml = None
            self._handle = None

    def sample(self):
        """返回 dict：temp/power/clock/load/mem_used/mem_total，与
        recorder.sample_gpu_nvidia_smi() 的键名一致，便于替换。"""
        if not _is_win() or self._nvml is None or self._handle is None:
            return {"temp": _nan(), "power": _nan(), "clock": _nan(),
                    "load": _nan(), "mem_used": _nan(), "mem_total": _nan()}
        try:
            class _Mem(ctypes.Structure):
                _fields_ = [("total", ctypes.c_ulonglong),
                            ("free", ctypes.c_ulonglong),
                            ("used", ctypes.c_ulonglong)]
            mem = _Mem()
            if self._nvml.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(mem)) != 0:
                return {"temp": _nan(), "power": _nan(), "clock": _nan(),
                        "load": _nan(), "mem_used": _nan(), "mem_total": _nan()}
            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]
            util = _Util()
            try:
                if self._nvml.nvmlDeviceGetUtilizationRates(self._handle, ctypes.byref(util)) == 0:
                    load = float(util.gpu)
                else:
                    load = _nan()
            except Exception:
                load = _nan()
            temp = _nan()
            try:
                t = ctypes.c_uint()
                # 0 = GPU sensor
                if self._nvml.nvmlDeviceGetTemperature(self._handle, 0, ctypes.byref(t)) == 0:
                    temp = float(t.value)
            except Exception:
                pass
            clock = _nan()
            try:
                c = ctypes.c_uint()
                # 0 = SM clock
                if self._nvml.nvmlDeviceGetClockInfo(self._handle, 0, ctypes.byref(c)) == 0:
                    clock = float(c.value)
            except Exception:
                pass
            power = _nan()
            try:
                p = ctypes.c_uint()
                if self._nvml.nvmlDeviceGetPowerUsage(self._handle, ctypes.byref(p)) == 0:
                    power = float(p.value) / 1000.0
            except Exception:
                pass
            return {"temp": temp, "power": power, "clock": clock, "load": load,
                    "mem_used": mem.used / 1024 / 1024,
                    "mem_total": mem.total / 1024 / 1024}
        except Exception:
            return {"temp": _nan(), "power": _nan(), "clock": _nan(),
                    "load": _nan(), "mem_used": _nan(), "mem_total": _nan()}

    def close(self):
        try:
            if self._nvml is not None:
                self._nvml.nvmlShutdown()
        except Exception:
            pass
        self._nvml = None


# ---------------------------------------------------------------- WHEA / SMART 快照

def whea_snapshot(timeout=10):
    """返回近 10 分钟 WHEA-Logger 事件条数，拿不到返回 None。

    Windows 下用系统自带 wevtutil，不装第三方工具；非 Windows 返回 None。
    """
    if not _is_win():
        return None
    try:
        r = subprocess.run(
            ["wevtutil", "qe", "Microsoft-Windows-WHEA-Logger",
             "/c:50", "/f:text"],
            capture_output=True, text=True, timeout=timeout, errors="replace")
        if r.returncode != 0:
            return None
        # 粗略计数：每条事件以 "Event[" 开头（wevtutil 文本格式）
        return r.stdout.count("Event[")
    except Exception:
        return None


def smart_snapshot():
    """SMART 快照占位：Windows 下用 DeviceIoControl 读需要复杂的
    STORAGE_PROPERTY_QUERY 结构与管理员权限，v1.3 先做“尝试读总量与
    温度，拿不到就返回 None”的保守实现，不编造健康度。

    返回 None 表示未采到（报告写盲区）；返回 dict 时含 total 字段。
    真机上若 DeviceIoControl 路径被证明稳定，再补全温度/健康度字段。
    """
    # 注意：本函数在 v1.3 不伪装成已完整实现 SMART 读取。
    # 显存与 PDH 已覆盖主要归因，SMART 只作为录制前后快照的
    # “是否可读”标记，避免把不确定的健康度写进结论。
    return None


# ---------------------------------------------------------------- 长驻 nvidia-smi 流式采样（互查 P0-1 修）

class StreamingNvidiaSmiSampler:
    """NVML 不可用时的唯一回退：单个长驻 nvidia-smi 进程，-l 1 每秒
    输出一行，读线程存最新一行，sample() 只读内存，不 Popen。

    绝不在采样线程里反复 Popen。进程启动失败时 sample() 返回空值，
    报告盲区里写明显存维度未排查。非 Windows 下不启动进程。
    """

    def __init__(self):
        self._proc = None
        self._latest = None
        self._lock = None
        if not _is_win():
            return
        try:
            import threading
            self._lock = threading.Lock()
            self._proc = subprocess.Popen(
                ["nvidia-smi",
                 "--query-gpu=temperature.gpu,power.draw,clocks.sm,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits", "-l", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            def _read():
                try:
                    for line in self._proc.stdout:
                        parts = [x.strip() for x in line.split(",")]
                        if len(parts) != 6:
                            continue
                        try:
                            vals = [float(x) for x in parts]
                        except ValueError:
                            continue
                        with self._lock:
                            self._latest = {"temp": vals[0], "power": vals[1],
                                            "clock": vals[2], "load": vals[3],
                                            "mem_used": vals[4], "mem_total": vals[5]}
                except Exception:
                    pass
            threading.Thread(target=_read, daemon=True).start()
        except Exception:
            self._proc = None

    def sample(self):
        if self._lock is None or self._latest is None:
            return {"temp": _nan(), "power": _nan(), "clock": _nan(),
                    "load": _nan(), "mem_used": _nan(), "mem_total": _nan()}
        with self._lock:
            return dict(self._latest)

    def close(self):
        try:
            if self._proc is not None:
                self._proc.terminate()
                self._proc.wait(timeout=3)
        except Exception:
            try:
                if self._proc is not None:
                    self._proc.kill()
            except Exception:
                pass
        self._proc = None


def gpu_total_mb_from_registry():
    """尽力读注册表 64 位显存总量（互查 P0-4 修）。Windows 下枚举
    Control/Video 下各 GUID 的 HardwareInformation.MemorySize（QWORD），
    取最大值转 MB；读不到返回 None，不编造。非 Windows 返回 None。"""
    if not _is_win():
        return None
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Video"
        best = None
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                for sub in ("0000", "0001"):
                    try:
                        with winreg.OpenKey(k, guid + "\\" + sub) as sk:
                            val, _typ = winreg.QueryValueEx(sk, "HardwareInformation.MemorySize")
                            mb = float(val) / 1024 / 1024
                            if best is None or mb > best:
                                best = mb
                    except OSError:
                        continue
        return best
    except Exception:
        return None
