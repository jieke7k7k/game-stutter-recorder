#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3 测试：显存接线、复合判定、优先级、同游戏历史、卸载清单。

跑法：python tests/test_v13.py（不依赖 PySide6，不依赖 Windows，
PDH/NVML 在 Linux 下返回空值，测试只断言纯逻辑与降级行为）。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import recorder
import history
import uninstall
import pdh_nvml


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} 失败：{detail}")
    print(f"OK {name}")


def main():
    # 1. parse_sensors 算显存百分比（v1.2 缺口：只读 Used 不读 Total）
    headers = ["timestamp", "elapsedsec", "cpu_load_pct", "memory_load_pct",
               "gpu_temp_c", "gpu_power_w", "gpu_clock_mhz", "gpu_load_pct",
               "gpu_memory_used_mb", "gpu_memory_total_mb"]
    rows = [["2026-01-01", "1.00", "10", "80", "70", "100", "2000", "90", "7600", "8188"],
            ["2026-01-01", "2.00", "10", "80", "70", "100", "2000", "90", "7650", "8188"],
            ["2026-01-01", "2.50", "10", "80", "70", "100", "2000", "40", "7700", "8188"]]
    sensors = recorder.parse_sensors(headers, rows)
    check("显存百分比算出约 92.8%", abs(sensors[0]["vram_pct"] - 7600 / 8188 * 100) < 0.01, sensors[0]["vram_pct"])
    check("显存总量读到了", sensors[0]["gpu_mem_total"] == 8188)

    # 2. 窗口最大值对齐：窗口内无样本返回 None，不拿旧数据硬拼
    check("窗口无样本返回 None", recorder._window_stats(sensors, 100.0) is None)
    win = recorder._window_stats(sensors, 2.4)
    check("窗口最大值是 7700 对应的百分比", win is not None and abs(win["vram_pct"] - 7700 / 8188 * 100) < 0.01, win)
    check("GPU 负载下跌信号算出", win["gpu_drop"] is True, win)

    # 3. 复合判定：高显存 + GPU 下跌 = 显存溢出；高显存 alone + GPU 满载 = GPU 瓶颈
    ev = {"mem": 80, "gpu_temp": 70, "cpu_temp": 70, "gpu_busy_ratio": 0.95,
          "gpu_load": 99, "gpu_clock": 2000, "med_clock": 2000,
          "gpu_mem": float("nan"), "vram_pct": 95, "gpu_drop": True, "vram_climb": False}
    cause, _ = recorder.classify(ev, allow_vram_strong=True)
    check("高显存+下跌判显存溢出", "显存溢出" in cause, cause)
    ev2 = dict(ev, gpu_drop=False, vram_climb=False, mem=80)
    cause2, _ = recorder.classify(ev2, allow_vram_strong=True)
    check("高显存 alone + GPU 满载判 GPU 瓶颈", "GPU 瓶颈" in cause2, cause2)
    # 4. 双满：显存 ≥92 且内存 ≥88 = 双满主因，排在纯显存之前
    ev3 = dict(ev, gpu_drop=False, mem=91)
    cause3, _ = recorder.classify(ev3, allow_vram_strong=True)
    check("双满判同时见底", "同时见底" in cause3, cause3)
    # 5. 强判定门：本局卡顿 <3 次时不硬判显存
    cause4, _ = recorder.classify(ev, allow_vram_strong=False)
    check("卡顿不足 3 次不硬判显存", "显存" not in cause4, cause4)
    # 6. 温度降频仍排第一
    ev5 = dict(ev, gpu_temp=88)
    cause5, _ = recorder.classify(ev5, allow_vram_strong=True)
    check("温度降频排第一", "温度过高" in cause5, cause5)

    # 7. 同游戏历史：只对比同游戏，不同游戏不对比
    with tempfile.TemporaryDirectory() as td:
        rec = Path(td) / "Records"
        analysis_a = {"frame_count": 100, "avg_fps": 60, "low1": 30,
                      "events": [1, 2, 3], "top_cause": "GPU 瓶颈", "top_share": 100}
        history.save_session(rec, "Game.exe", analysis_a)
        prev = history.last_same_game(rec, "game.exe", exclude_latest=False)
        check("同游戏（大小写不敏感）能找回上次", prev is not None and prev["stutter_count"] == 3, prev)
        check("不同游戏找回为空", history.last_same_game(rec, "Other.exe", exclude_latest=False) is None)
        cur = {"events": [1]}
        check("对比句：卡顿少了", "少了 2 次" in history.comparison_sentence(cur, prev),
              history.comparison_sentence(cur, prev))

    # 8. 卸载清单：默认保留记录，全部删除才删记录；驱动标记决定是否清驱动
    with tempfile.TemporaryDirectory() as td:
        app_dir = Path(td) / "app"
        rec_dir = app_dir / "Records"
        app_dir.mkdir(parents=True)
        delete, keep, _checks = uninstall.uninstall_plan(app_dir, rec_dir, delete_records=False)
        check("默认保留记录", str(rec_dir) in keep and str(rec_dir) not in delete)
        delete2, _k2, _c2 = uninstall.uninstall_plan(app_dir, rec_dir, delete_records=True)
        check("全部删除才删记录", str(rec_dir) in delete2)
        check("无标记不拆驱动", uninstall.driver_installed_by_us(app_dir) is False)
        (app_dir / "lhm_driver_installed.txt").write_text("x")
        check("有标记才拆驱动", uninstall.driver_installed_by_us(app_dir) is True)
        batch = Path(td) / "un.bat"
        uninstall.write_uninstall_batch(app_dir, rec_dir, False, batch)
        text = batch.read_text(encoding="utf-8")
        check("保留记录时批处理不删程序文件夹", 'rmdir /s /q "' + str(app_dir) + '"' not in text, text)
        check("批处理清注册表", "reg delete" in text)

    # 9. PDH/NVML 降级不崩溃（平台相关，互查后修 CI 失败用）
    # Linux 下返回空；Windows CI 跑在 windows-latest 上，PDH 会返回
    # 真实数据、NVML/WHEA 也可能有值，所以 Windows 下只断言“字典、
    # 不崩溃”，不硬断言空值。以前硬断言 Linux 空值导致 CI #28 失败。
    import math as _math
    if sys.platform != "win32":
        check("PDH Linux 返回空", pdh_nvml.PDHSampler().sample() == {})
        check("NVML Linux 返回空显存", _math.isnan(pdh_nvml.NVMLSampler().sample()["mem_total"]))
        check("WHEA Linux 返回 None", pdh_nvml.whea_snapshot() is None)
    else:
        _pdh_s = pdh_nvml.PDHSampler().sample()
        check("PDH Windows 返回字典不崩溃", isinstance(_pdh_s, dict), _pdh_s)
        _nvml_s = pdh_nvml.NVMLSampler().sample()
        check("NVML Windows 返回字典含显存键",
              isinstance(_nvml_s, dict) and "mem_total" in _nvml_s, _nvml_s)
        _whea = pdh_nvml.whea_snapshot()
        check("WHEA Windows 返回 None 或计数值",
              _whea is None or (isinstance(_whea, int) and _whea >= 0), _whea)
    check("SMART 占位返回 None（不编造健康度）", pdh_nvml.smart_snapshot() is None)

    # 10. SensorRecorder 250ms 间隔参数可传，不改默认行为之外的接口
    import io, csv as _csv
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "sensors.csv"
        rec_thread = recorder.SensorRecorder(out, __import__("time").monotonic(), interval=0.05)
        rec_thread.start()
        __import__("time").sleep(0.22)
        rec_thread.stop_event.set()
        rec_thread.join(timeout=3)
        check("250ms 采样器能跑且写出新列",
              "VRAM_Pct" in out.read_text(encoding="utf-8").splitlines()[0])

    # 11. LHM 快照进摘要：录前/录后文件存在时摘要里出现温度，缺失时不编
    import app_wizard, json as _json
    sample = ROOT / "tests" / "sample_presentmon.csv"
    with tempfile.TemporaryDirectory() as td:
        session = Path(td) / "session"
        session.mkdir()
        (session / "lhm_before.json").write_text(_json.dumps({"temp_c": 72.0}), encoding="utf-8")
        (session / "lhm_after.json").write_text(_json.dumps({"temp_c": 85.0}), encoding="utf-8")
        _r, _m, _s = app_wizard._do_analysis(sample, None, session, "SampleGame", "2026-01-01T00:00:00")
        check("LHM 摘要含录前录后温度", "录前 72℃" in _s and "录后 85℃" in _s, _s)
    check("LHM 非 Windows 快照返回 None", app_wizard.lhm_snapshot(None) is None)
    check("LHM 路径不存在返回 None", app_wizard.resolve_lhm_path("/nonexistent-dir-xyz") is None)

    # 12. 互查 P0-2：GPU 下跌是窗口占比 ≥40%，不是单点最低值
    _s = [
        {"elapsed": 1.0, "gpu_load": 90.0, "vram_pct": 50.0, "mem": 50.0},
        {"elapsed": 2.0, "gpu_load": 90.0, "vram_pct": 50.0, "mem": 50.0},
        {"elapsed": 2.5, "gpu_load": 90.0, "vram_pct": 50.0, "mem": 50.0},
        {"elapsed": 2.6, "gpu_load": 40.0, "vram_pct": 50.0, "mem": 50.0},
    ]
    # 窗口中心 2.5：窗口 [1.5, 3.0] 含 90,90,40 三样本，下跌占比 1/3 <40%
    _w = recorder._window_stats(_s, 2.5)
    check("下跌占比不足 40% 不算下跌", _w["gpu_drop"] is False, _w)
    _s2 = [
        {"elapsed": 1.0, "gpu_load": 90.0, "vram_pct": 50.0, "mem": 50.0},
        {"elapsed": 2.0, "gpu_load": 40.0, "vram_pct": 50.0, "mem": 50.0},
        {"elapsed": 2.5, "gpu_load": 40.0, "vram_pct": 50.0, "mem": 50.0},
    ]
    _w2 = recorder._window_stats(_s2, 2.4)
    check("下跌占比 ≥40% 算下跌", _w2["gpu_drop"] is True, _w2)

    # 13. 互查 P0-2 豁免：显存平稳高 + GPU 满载 = GPU 瓶颈，哪怕内存 ≥85%
    _ev_ex = {"mem": 86, "gpu_temp": 70, "cpu_temp": 70, "gpu_busy_ratio": 0.95,
              "gpu_load": 99, "gpu_load_mean": 99, "gpu_clock": 2000, "med_clock": 2000,
              "gpu_mem": float("nan"), "vram_pct": 95.5, "vram_span": 0.5,
              "gpu_drop": False, "vram_climb": False}
    _c_ex, _ = recorder.classify(_ev_ex, allow_vram_strong=True)
    check("平稳高显存 + GPU 满载豁免为 GPU 瓶颈", "GPU 瓶颈" in _c_ex, _c_ex)
    _ev_dual = dict(_ev_ex, mem=91)
    _c_dual, _ = recorder.classify(_ev_dual, allow_vram_strong=True)
    check("双满仍优先于豁免", "同时见底" in _c_dual, _c_dual)

    # 14. 互查 P1-3：无效行跳过后 frame_index 仍连续，approx_elapsed 不错位
    _headers = ["frametime", "fps"]
    _rows = [["8.3", "120"], ["", ""], ["50.0", "20"], ["8.3", "120"]] * 4
    _frames = recorder.parse_frames(_headers, _rows)
    check("frame_index 连续", [f["frame_index"] for f in _frames] == list(range(len(_frames))),
          [f["frame_index"] for f in _frames])

    # 15. 互查 P0-1：无 gpu_sampler 时绝不调用 nvidia-smi 采样函数
    _called = {"v": False}
    _orig = recorder.sample_gpu_nvidia_smi
    def _spy():
        _called["v"] = True
        return _orig()
    recorder.sample_gpu_nvidia_smi = _spy
    try:
        with tempfile.TemporaryDirectory() as td:
            _out = Path(td) / "s.csv"
            _th = recorder.SensorRecorder(_out, __import__("time").monotonic(), interval=0.05)
            _th.start()
            __import__("time").sleep(0.15)
            _th.stop_event.set()
            _th.join(timeout=3)
    finally:
        recorder.sample_gpu_nvidia_smi = _orig
    check("无采样器时不调用 nvidia-smi", _called["v"] is False)

    # 16. 互查 P1-7：完整路径与裸文件名归一化为同一游戏
    check("路径归一化", history.norm_game("C:\\Games\\Game.exe") == "game.exe",
          history.norm_game("C:\\Games\\Game.exe"))

    print("v1.3 测试全部通过")


if __name__ == "__main__":
    main()
