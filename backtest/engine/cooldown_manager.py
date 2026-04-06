#!/usr/bin/env python3
"""
backtest/engine/cooldown_manager.py
回测专用冷却管理器（纯内存实现，无文件 IO）

复用生产逻辑：
- 止损触发后：长冷却（stop_loss_cooldown_hours，默认4小时）
- 止盈触发后：短冷却（take_profit_cooldown_minutes，默认30分钟）
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class CooldownManager:
    """
    冷却管理器。

    在平仓时记录冷却状态，在开仓前检查是否处于冷却期。
    所有数据保存在内存中，适合回测场景。
    """

    def __init__(self, config: dict) -> None:
        """
        参数：
            config : 完整合并配置字典
        """
        risk_cfg = config.get("risk", {})
        self.stop_loss_cooldown_hours = risk_cfg.get("stop_loss_cooldown_hours", 4)
        self.take_profit_cooldown_minutes = risk_cfg.get("take_profit_cooldown_minutes", 30)

        # 内部存储：{symbol: {"time_ms": int, "type": "stop_loss"|"take_profit"}}
        self._cooldown_records: dict[str, dict] = {}

        logger.info(
            f"CooldownManager 初始化：止损冷却={self.stop_loss_cooldown_hours}h, "
            f"止盈冷却={self.take_profit_cooldown_minutes}min"
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def record_close(
        self,
        symbol: str,
        close_reason: str,
        close_time_ms: int
    ) -> None:
        """
        平仓时记录冷却状态。

        根据 close_reason 判断冷却类型：
        - 止损类（sl, force_close, structure_break_*）：长冷却
        - 止盈类（tp, partial_tp1, partial_tp2, trailing_sl）：短冷却
        - 其他（eod 等）：不记录冷却

        参数：
            symbol        : 品种代码
            close_reason  : 平仓原因
            close_time_ms : 平仓时间戳（毫秒）
        """
        cooldown_type = self._classify_close_reason(close_reason)
        if cooldown_type is None:
            return

        self._cooldown_records[symbol] = {
            "time_ms": close_time_ms,
            "type": cooldown_type,
        }

        cooldown_desc = "止损" if cooldown_type == "stop_loss" else "止盈"
        logger.debug(f"记录冷却：{symbol} | 类型={cooldown_desc} | 原因={close_reason}")

    def is_in_cooldown(
        self,
        symbol: str,
        current_time_ms: int
    ) -> tuple[bool, str]:
        """
        检查品种是否在冷却期内。

        返回：(是否可以开仓, 原因说明)
        - (True, "...")  : 可以开仓
        - (False, "...") : 处于冷却期，禁止开仓
        """
        record = self._cooldown_records.get(symbol)
        if not record:
            return True, "无冷却记录"

        cooldown_type = record["type"]
        cooldown_time_ms = record["time_ms"]

        # 计算冷却持续时间
        if cooldown_type == "take_profit":
            cooldown_duration_ms = self.take_profit_cooldown_minutes * 60 * 1000
            remaining_ms = cooldown_time_ms + cooldown_duration_ms - current_time_ms
            if remaining_ms > 0:
                remaining_min = remaining_ms / 1000 / 60
                return False, f"止盈冷却中，剩余{remaining_min:.0f}分钟"
        else:  # stop_loss
            cooldown_duration_ms = self.stop_loss_cooldown_hours * 3600 * 1000
            remaining_ms = cooldown_time_ms + cooldown_duration_ms - current_time_ms
            if remaining_ms > 0:
                remaining_hr = remaining_ms / 1000 / 3600
                return False, f"止损冷却中，剩余{remaining_hr:.1f}小时"

        # 冷却期已过，清理记录
        self._cooldown_records.pop(symbol, None)
        return True, "冷却期已过"

    def clear_cooldown(self, symbol: str) -> None:
        """手动清除冷却记录（用于调试或特殊场景）"""
        self._cooldown_records.pop(symbol, None)
        logger.debug(f"已清除 {symbol} 的冷却记录")

    def clear_all(self) -> None:
        """清除所有冷却记录（用于重置回测状态）"""
        self._cooldown_records.clear()
        logger.debug("已清除所有冷却记录")

    def get_active_cooldowns(self) -> dict[str, dict]:
        """获取当前所有冷却记录（用于调试）"""
        return dict(self._cooldown_records)

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _classify_close_reason(self, close_reason: str) -> Optional[str]:
        """
        根据平仓原因分类冷却类型。

        返回：
            "stop_loss"   : 止损类，长冷却
            "take_profit" : 止盈类，短冷却
            None          : 不需要冷却（如 eod）
        """
        # 止损类：触发长冷却
        stop_loss_reasons = {
            "sl",
            "force_close",
        }
        if close_reason in stop_loss_reasons or close_reason.startswith("structure_break"):
            return "stop_loss"

        # 止盈类：触发短冷却
        take_profit_reasons = {
            "tp",
            "trailing_sl",
            "partial_tp1",
            "partial_tp2",
        }
        if close_reason in take_profit_reasons:
            return "take_profit"

        # 其他：不记录冷却
        # 包括：eod（回测结束平仓）、support_break、resistance_break 等
        return None