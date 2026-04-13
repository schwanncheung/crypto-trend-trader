#!/usr/bin/env python3
"""
交易时段判断模块

提供 is_trading_session() 判断当前是否处于交易时段内。
支持跨天时段（美洲 22:00-05:00）。
非交易时段：生产 scanner 跳过扫描，回测信号过滤跳过该 bar。
"""

import re
from datetime import datetime, timezone, timedelta
from typing import Optional

# 缓存配置引用，避免重复读文件
_cached_cfg: Optional[dict] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_tz_offset(tz: str) -> float:
    """
    将 'UTC+8' -> 8.0, 'UTC-5' -> -5.0
    """
    m = re.search(r'UTC([+-])(\d+)', tz)
    if not m:
        return 0.0
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2))


def _hour_in_session(current_hour: int, start: int, end: int) -> bool:
    """
    判断当前小时是否在给定时段内。
    end < start 表示跨天时段（如美洲 22-5）。
    """
    if end < start:
        return current_hour >= start or current_hour < end
    return start <= current_hour < end


def _load_session_cfg() -> dict:
    """从 config_loader 读取交易时段配置（带缓存）"""
    global _cached_cfg
    if _cached_cfg is not None:
        return _cached_cfg
    try:
        from scripts.config_loader import CFG
        _cached_cfg = CFG.get("trading_sessions", {})
    except ImportError:
        _cached_cfg = {}
    return _cached_cfg


def is_trading_session(cfg: Optional[dict] = None) -> bool:
    """
    返回当前 UTC 时间是否处于任一交易时段内。

    Args:
        cfg: 交易时段配置字典，传 None 则从 config_loader 自动读取。

    Returns:
        True  = 在交易时段内，允许交易/扫描
        False = 非交易时段，禁止交易/扫描
    """
    if cfg is None:
        cfg = _load_session_cfg()

    # 总开关关闭 = 全天候交易
    if not cfg.get("enabled", False):
        return True

    sessions = cfg.get("sessions", [])
    if not sessions:
        return True  # 未配置时段 = 全天候

    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    local = _utc_now() + timedelta(hours=tz_offset)
    current_hour = local.hour

    for s in sessions:
        if _hour_in_session(current_hour, s["start_hour"], s["end_hour"]):
            return True
    return False


def get_current_session_label(cfg: Optional[dict] = None) -> str:
    """
    返回当前所处时段的 label。
    如 '亚洲时段'，无匹配返回 '非交易时段'。
    """
    if cfg is None:
        cfg = _load_session_cfg()

    if not cfg.get("enabled", False):
        return "全天候"

    sessions = cfg.get("sessions", [])
    if not sessions:
        return "全天候"

    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    local = _utc_now() + timedelta(hours=tz_offset)
    current_hour = local.hour

    for s in sessions:
        if _hour_in_session(current_hour, s["start_hour"], s["end_hour"]):
            return s.get("label", s["name"])
    return "非交易时段"


def get_session_label_from_ts(ts_ms: int, cfg: Optional[dict] = None) -> str:
    """
    根据时间戳返回时段标签（回测/生产通用）。

    Args:
        ts_ms: bar 收盘时间戳（Unix ms，UTC）
        cfg:   交易时段配置，传 None 则自动读取

    Returns:
        时段 label 字符串，如 '亚洲时段'、'欧洲时段'、'美洲时段'、'全天候'、'非交易时段'
    """
    if cfg is None:
        cfg = _load_session_cfg()
    if not cfg.get("enabled", False):
        return "全天候"
    sessions = cfg.get("sessions", [])
    if not sessions:
        return "全天候"
    from datetime import datetime as dt
    utc_dt = dt.utcfromtimestamp(ts_ms / 1000)
    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    local_dt = utc_dt + timedelta(hours=tz_offset)
    current_hour = local_dt.hour
    for s in sessions:
        if _hour_in_session(current_hour, s["start_hour"], s["end_hour"]):
            return s.get("label", s["name"])
    return "非交易时段"


def is_trading_bar(ts_ms: int, cfg: Optional[dict] = None) -> bool:
    """
    判断指定 bar 时间戳（UTC 毫秒）是否处于交易时段。
    用于回测：逐 bar 循环中过滤非交易时段。

    Args:
        ts_ms: bar 收盘时间戳（Unix ms，UTC）
        cfg:   交易时段配置，传 None 则自动读取

    Returns:
        True  = 该 bar 在交易时段内，可产生信号
        False = 该 bar 在非交易时段，跳过信号生成
    """
    if cfg is None:
        cfg = _load_session_cfg()

    if not cfg.get("enabled", False):
        return True

    sessions = cfg.get("sessions", [])
    if not sessions:
        return True

    # 将 ts_ms 转换为本地小时
    from datetime import datetime as dt
    utc_dt = dt.utcfromtimestamp(ts_ms / 1000)
    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    local_dt = utc_dt + timedelta(hours=tz_offset)
    current_hour = local_dt.hour

    for s in sessions:
        if _hour_in_session(current_hour, s["start_hour"], s["end_hour"]):
            return True
    return False
