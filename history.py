#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3 同游戏历史对比（SQLite，本地文件，不上传）。

规则（用户定版）：
- 只对比同一个游戏（同一进程名，大小写不敏感），不同游戏不对比。
- 同游戏但画质/分辨率不同时，调用方应在报告里注明仅供参考
  （本模块不存画质字段，因为向导版不采集画质设置，不编造）。
- 无上次记录时不写对比句，不用空值凑数。
- history.db 放在 Records/ 下，随“保留记录”一起保留，随“全部删除”一起删。
只用标准库 sqlite3。
"""
import sqlite3
from pathlib import Path


def history_path(records_dir):
    return Path(records_dir) / "history.db"


def norm_game(game_name):
    """归一化游戏名：取进程文件名、转小写。互查 P1-7 修：
    手动输入完整路径时，历史对比不能因路径不同失效。"""
    if not game_name:
        return ""
    return Path(str(game_name).strip().replace("\\", "/")).name.lower()


def save_session(records_dir, game_name, analysis):
    """存一行摘要。analysis 来自 recorder.analyze_frames()。"""
    if not game_name:
        return
    records_dir = Path(records_dir)
    records_dir.mkdir(parents=True, exist_ok=True)
    db = history_path(records_dir)
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game TEXT NOT NULL,
                created TEXT DEFAULT (datetime('now')),
                frame_count REAL, avg_fps REAL, low1 REAL,
                stutter_count INTEGER, top_cause TEXT, top_share REAL,
                max_temp REAL, throttled INTEGER, vram_peak REAL
            )""")
        # 老库迁移：缺列时补列，已存在时忽略报错
        for _col in ("max_temp REAL", "throttled INTEGER", "vram_peak REAL"):
            try:
                con.execute(f"ALTER TABLE sessions ADD COLUMN {_col}")
            except Exception:
                pass
        _evs = [e for e in (analysis.get("events", []) or []) if isinstance(e, dict)]
        _temps = [e.get("evidence", {}).get("gpu_temp") for e in _evs
                  if isinstance(e.get("evidence", {}), dict)
                  and e["evidence"].get("gpu_temp") is not None
                  and not (isinstance(e["evidence"]["gpu_temp"], float)
                           and e["evidence"]["gpu_temp"] != e["evidence"]["gpu_temp"])]
        _vrams = [e.get("evidence", {}).get("vram_pct") for e in _evs
                  if isinstance(e.get("evidence", {}), dict)
                  and isinstance(e["evidence"].get("vram_pct"), (int, float))]
        _throttled = 1 if any(e.get("cause") == "温度过高，可能降频了" for e in _evs) else 0
        con.execute(
            "INSERT INTO sessions (game, frame_count, avg_fps, low1, stutter_count, top_cause, top_share, max_temp, throttled, vram_peak)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (norm_game(game_name), analysis.get("frame_count"),
             analysis.get("avg_fps"), analysis.get("low1"),
             len(analysis.get("events", []) or []), analysis.get("top_cause"),
             analysis.get("top_share"),
             max(_temps) if _temps else None, _throttled,
             max(_vrams) if _vrams else None))
        con.commit()
    finally:
        con.close()


def last_same_game(records_dir, game_name, exclude_latest=True):
    """返回上一次同游戏的摘要 dict，没有返回 None。

    save_session() 刚写完本局后调用时，exclude_latest=True 会跳过
    最新一行（也就是本局），返回上上一局；分析前调用时用 False。
    """
    if not game_name:
        return None
    db = history_path(records_dir)
    if not db.exists():
        return None
    con = sqlite3.connect(str(db))
    try:
        cur = con.execute(
            "SELECT game, frame_count, avg_fps, low1, stutter_count, top_cause, top_share"
            " FROM sessions WHERE game = ? ORDER BY id DESC LIMIT 2",
            (norm_game(game_name),))
        rows = cur.fetchall()
    finally:
        con.close()
    if not rows:
        return None
    row = rows[1] if (exclude_latest and len(rows) >= 2) else (rows[0] if not exclude_latest else None)
    if row is None:
        return None
    keys = ["game", "frame_count", "avg_fps", "low1", "stutter_count", "top_cause", "top_share"]
    return dict(zip(keys, row))


def comparison_sentence(current, previous):
    """同游戏对比句。current/previous 为 analysis 摘要（previous 可为 None）。
    返回 "" 表示不写对比句。"""
    if not previous:
        return ""
    cur_n = len(current.get("events", [])) if isinstance(current, dict) else 0
    prev_n = previous.get("stutter_count")
    if prev_n is None:
        return ""
    if cur_n < prev_n:
        return f"比上次同游戏记录卡顿少了 {prev_n - cur_n} 次。"
    if cur_n > prev_n:
        return f"比上次同游戏记录卡顿多了 {cur_n - prev_n} 次。"
    return "和上次同游戏记录卡顿次数一样。"
