#!/usr/bin/env python3
"""
止损追踪模块
记录止损触发时间，用于冷却机制
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

COOLDOWN_FILE = Path("logs/stop_loss_cooldown.json")
POSITION_SNAPSHOT_FILE = Path("logs/position_snapshot.json")


def _ensure_file(file_path: Path):
    """确保文件存在"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if not file_path.exists():
        file_path.write_text("{}")


def save_position_snapshot(positions: list):
    """
    保存当前持仓快照（用于下次对比检测止损）
    positions: get_open_positions() 返回的持仓列表
    """
    _ensure_file(POSITION_SNAPSHOT_FILE)
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
    POSITION_SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))


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
    _ensure_file(POSITION_SNAPSHOT_FILE)
    _ensure_file(COOLDOWN_FILE)

    try:
        # 读取上次快照
        snapshot_data = json.loads(POSITION_SNAPSHOT_FILE.read_text())
        last_positions = snapshot_data.get("positions", {})

        # 当前持仓
        current_keys = {f"{p['symbol']}_{p['side']}" for p in current_positions}

        # 找出消失的持仓
        disappeared = set(last_positions.keys()) - current_keys

        if not disappeared:
            return

        # 读取冷却记录
        cooldown_data = json.loads(COOLDOWN_FILE.read_text())

        for key in disappeared:
            last_pos = last_positions[key]
            symbol = key.rsplit("_", 1)[0]
            pnl = last_pos.get("unrealized_pnl", 0)

            # 只有亏损状态消失才记录冷却（推断为止损触发）
            if pnl < 0:
                cooldown_data[symbol] = datetime.now(timezone.utc).isoformat()
                logger.warning(
                    f"检测到止损触发：{symbol} 持仓消失（浮亏 {pnl:.2f} USDT），"
                    f"记录冷却时间"
                )
            else:
                logger.info(
                    f"{symbol} 持仓消失（浮盈 {pnl:.2f} USDT），"
                    f"推断为止盈/手动平仓，不记录冷却"
                )

        # 保存冷却记录
        COOLDOWN_FILE.write_text(json.dumps(cooldown_data, indent=2))

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
    _ensure_file(COOLDOWN_FILE)

    try:
        cooldown_data = json.loads(COOLDOWN_FILE.read_text())
        cooldown_data[symbol] = datetime.now(timezone.utc).isoformat()
        COOLDOWN_FILE.write_text(json.dumps(cooldown_data, indent=2))
        logger.warning(f"记录止损冷却：{symbol} | 原因：{reason}")
    except Exception as e:
        logger.error(f"记录止损冷却异常：{e}")


def check_cooldown(symbol: str, cooldown_hours: int = 4) -> tuple[bool, str]:
    """
    检查合约是否在冷却期内

    返回：(是否通过, 原因)
    - (True, "无冷却记录") - 可以开仓
    - (False, "冷却中，剩余X小时") - 禁止开仓
    """
    _ensure_file(COOLDOWN_FILE)

    try:
        cooldown_data = json.loads(COOLDOWN_FILE.read_text())
        last_stop_loss = cooldown_data.get(symbol)

        if not last_stop_loss:
            return True, "无冷却记录"

        last_time = datetime.fromisoformat(last_stop_loss)
        cooldown_until = last_time + timedelta(hours=cooldown_hours)
        now = datetime.now(timezone.utc)

        if now < cooldown_until:
            remaining = (cooldown_until - now).total_seconds() / 3600
            return False, f"止损冷却中，剩余 {remaining:.1f} 小时"

        # 冷却期已过，清理记录
        del cooldown_data[symbol]
        COOLDOWN_FILE.write_text(json.dumps(cooldown_data, indent=2))
        return True, "冷却期已过"

    except Exception as e:
        logger.warning(f"冷却检查异常：{e}")
        return True, "冷却检查异常，保守放行"


def clear_cooldown(symbol: str):
    """清除冷却记录（用于手动干预）"""
    _ensure_file(COOLDOWN_FILE)

    try:
        cooldown_data = json.loads(COOLDOWN_FILE.read_text())
        if symbol in cooldown_data:
            del cooldown_data[symbol]
            COOLDOWN_FILE.write_text(json.dumps(cooldown_data, indent=2))
            logger.info(f"已清除 {symbol} 的冷却记录")
    except Exception as e:
        logger.error(f"清除冷却记录异常：{e}")
