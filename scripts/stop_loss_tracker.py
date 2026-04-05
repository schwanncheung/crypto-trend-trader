#!/usr/bin/env python3
"""
止损追踪模块
记录止损触发时间，用于冷却机制
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

from file_lock import atomic_read_json, atomic_write_json, atomic_update_json

logger = logging.getLogger(__name__)

COOLDOWN_FILE = Path("logs/stop_loss_cooldown.json")
POSITION_SNAPSHOT_FILE = Path("logs/position_snapshot.json")

# 延迟导入，避免循环依赖
def _get_generate_close_report():
    from trade_report import generate_close_report
    return generate_close_report


def _save_close_trade_log(symbol: str, side: str, position_data: dict, pnl: float, close_reason: str):
    """
    生成平仓日志文件（用于日报统计）

    参数：
    - symbol: 合约符号
    - side: 方向（long/short）
    - position_data: 持仓快照数据
    - pnl: 已实现盈亏
    - close_reason: 平仓原因（stop_loss/take_profit_or_manual）
    """
    try:
        from config_loader import now_cst_str

        log_dir = Path("logs/trades")
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = now_cst_str()
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        log_path = log_dir / f"{safe_symbol}_close_{timestamp}.json"

        close_log = {
            "type": "close",
            "status": "success",
            "symbol": symbol,
            "side": side,
            "close_reason": close_reason,
            "contracts": position_data.get("contracts", 0),
            "entry_price": position_data.get("entry_price", 0),
            "orders": [{
                "realized_pnl": pnl,
                "pnl": pnl,
            }],
            "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        }

        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(close_log, f, ensure_ascii=False, indent=2)

        logger.info(f"平仓日志已保存：{log_path}")

    except Exception as e:
        logger.error(f"保存平仓日志失败：{e}")


def _call_generate_report(symbol: str, pnl: float, position_data: dict, reason: str):
    """
    调用交易报告生成函数
    """
    try:
        generate_close_report = _get_generate_close_report()
        entry_price = position_data.get("entry_price", 0)
        contracts = position_data.get("contracts", 0)

        # 计算 pnl_pct
        if entry_price and contracts:
            pnl_pct = pnl / (entry_price * contracts) * 100
        else:
            pnl_pct = 0

        generate_close_report(symbol, reason, pnl, pnl_pct)
    except Exception as e:
        logger.error(f"生成交易报告失败：{e}")


def save_position_snapshot(positions: list):
    """
    保存当前持仓快照（用于下次对比检测止损）
    positions: get_open_positions() 返回的持仓列表
    """
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "positions": {
            f"{p['symbol']}_{p['side']}": {
                "contracts": p["contracts"],
                "entry_price": p["entry_price"],
                "unrealized_pnl": p["unrealized_pnl"],
            }
            for p in positions
        }
    }
    atomic_write_json(POSITION_SNAPSHOT_FILE, snapshot)


def detect_and_record_stop_loss(current_positions: list):
    """
    检测持仓消失事件（推断为止损触发），记录冷却时间

    逻辑：
    1. 读取上次持仓快照
    2. 对比当前持仓，找出消失的持仓
    3. 判断消失原因：
       - 如果是亏损状态消失 → 推断为止损触发，记录冷却
       - 如果是盈利状态消失 → 可能是止盈/手动平仓，不记录冷却
    """
    try:
        # 读取上次快照（atomic_read_json 内部已处理文件不存在的情况）
        snapshot_data = atomic_read_json(POSITION_SNAPSHOT_FILE, default={"positions": {}})
        last_positions = snapshot_data.get("positions", {})

        # 当前持仓
        current_keys = {f"{p['symbol']}_{p['side']}" for p in current_positions}

        # 找出消失的持仓
        disappeared = set(last_positions.keys()) - current_keys

        if not disappeared:
            return

        # 读取冷却记录
        cooldown_data = atomic_read_json(COOLDOWN_FILE, default={})

        for key in disappeared:
            last_pos = last_positions[key]
            symbol = key.rsplit("_", 1)[0]
            side = key.rsplit("_", 1)[1]
            pnl = last_pos.get("unrealized_pnl", 0)

            # 清理状态文件（无论止盈还是止损）
            _clear_position_states(symbol, side)

            pnl_threshold = -0.01
            if pnl < pnl_threshold:
                # 止损触发：记录长冷却（stop_loss_cooldown_hours）
                cooldown_data[symbol] = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "type": "stop_loss",
                }
                logger.warning(
                    f"检测到止损触发：{symbol} 持仓消失（浮亏 {pnl:.2f} USDT），"
                    f"记录止损冷却"
                )
                _save_close_trade_log(symbol, side, last_pos, pnl, "stop_loss")
                _call_generate_report(symbol, pnl, last_pos, "止损触发")
            else:
                # 止盈/手动平仓：记录短冷却（take_profit_cooldown_minutes），防止立即重新开仓
                cooldown_data[symbol] = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "type": "take_profit",
                }
                logger.info(
                    f"{symbol} 持仓消失（浮盈/微亏 {pnl:.2f} USDT），"
                    f"推断为止盈/手动平仓，记录短冷却"
                )
                _save_close_trade_log(symbol, side, last_pos, pnl, "take_profit_or_manual")
                _call_generate_report(symbol, pnl, last_pos, "止盈/手动平仓")

        # 保存冷却记录
        atomic_write_json(COOLDOWN_FILE, cooldown_data)

    except Exception as e:
        logger.error(f"检测止损触发异常：{e}")


def record_stop_loss_manual(symbol: str, reason: str):
    """
    手动记录止损触发（用于 trade_manager 主动平仓）

    调用时机：
    - trade_manager 强制平仓（浮亏超限）
    - trade_manager 结构���仓（支撑/阻力突破）
    - market_scanner 紧急风控
    """
    try:
        def _add_cooldown(data: dict) -> dict:
            data[symbol] = {
                "time": datetime.now(timezone.utc).isoformat(),
                "type": "stop_loss",
            }
            return data
        atomic_update_json(COOLDOWN_FILE, _add_cooldown, default={})
        logger.warning(f"记录止损冷却：{symbol} | 原因：{reason}")
    except Exception as e:
        logger.error(f"记录止损冷却异常：{e}")


def check_cooldown(symbol: str, cooldown_hours: int = 4) -> tuple[bool, str]:
    """
    检查合约是否在冷却期内

    返回：(是否通过, 原因)
    - (True, "无冷却记录") - 可以开仓
    - (False, "冷却中，剩余X小时/分钟") - 禁止开仓
    """
    try:
        cooldown_data = atomic_read_json(COOLDOWN_FILE, default={})
        entry = cooldown_data.get(symbol)

        if not entry:
            return True, "无冷却记录"

        last_time = datetime.fromisoformat(entry["time"])
        cooldown_type = entry.get("type", "stop_loss")

        now = datetime.now(timezone.utc)

        if cooldown_type == "take_profit":
            from config_loader import RISK_CFG as _RISK_CFG
            tp_minutes = _RISK_CFG.get("take_profit_cooldown_minutes", 30)
            cooldown_until = last_time + timedelta(minutes=tp_minutes)
            if now < cooldown_until:
                remaining_min = (cooldown_until - now).total_seconds() / 60
                return False, f"止盈冷却中，剩余 {remaining_min:.0f} 分钟"
        else:
            cooldown_until = last_time + timedelta(hours=cooldown_hours)
            if now < cooldown_until:
                remaining_hr = (cooldown_until - now).total_seconds() / 3600
                return False, f"止损冷却中，剩余 {remaining_hr:.1f} 小时"

        # 冷却期已过，清理记录
        def _remove_cooldown(data: dict) -> dict:
            data.pop(symbol, None)
            return data
        atomic_update_json(COOLDOWN_FILE, _remove_cooldown, default={})
        return True, "冷却期已过"

    except Exception as e:
        logger.warning(f"冷却检查异常：{e}")
        return True, "冷却检查异常，保守放行"


def _clear_position_states(symbol: str, side: str):
    """
    清理持仓相关的状态文件
    在持仓消失时调用（无论止盈还是止损）
    """
    key = f"{symbol}_{side}"

    def _remove_key(data: dict) -> dict:
        data.pop(key, None)
        return data

    try:
        breakeven_file = Path("logs/breakeven_state.json")
        if breakeven_file.exists():
            atomic_update_json(breakeven_file, _remove_key, default={})
            logger.info(f"已清理 {symbol} 的保本状态")
    except Exception as e:
        logger.error(f"清理保本状态异常：{e}")

    try:
        partial_profit_file = Path("logs/partial_profit_state.json")
        if partial_profit_file.exists():
            atomic_update_json(partial_profit_file, _remove_key, default={})
            logger.info(f"已清理 {symbol} 的部分止盈状态")
    except Exception as e:
        logger.error(f"清理部分止盈状态异常：{e}")

    try:
        trailing_stop_file = Path("logs/trailing_stop_state.json")
        if trailing_stop_file.exists():
            atomic_update_json(trailing_stop_file, _remove_key, default={})
            logger.info(f"已清理 {symbol} 的跟踪止损状态")
    except Exception as e:
        logger.error(f"清理跟踪止损状态异常：{e}")


def clear_cooldown(symbol: str):
    """清除冷却记录（用于手动干预）"""
    try:
        def _remove(data: dict) -> dict:
            data.pop(symbol, None)
            return data
        atomic_update_json(COOLDOWN_FILE, _remove, default={})
        logger.info(f"已清除 {symbol} 的冷却记录")
    except Exception as e:
        logger.error(f"清除冷却记录异常：{e}")
