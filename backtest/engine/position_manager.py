#!/usr/bin/env python3
"""
backtest/engine/position_manager.py
持仓管理器（纯计算，无任何 IO 依赖）

复用生产 trade_manager.py 的核心逻辑，去除所有交易所 API 调用。
所有函数为纯函数，便于单元测试。
"""

import logging
from typing import Optional

from .position import Position

logger = logging.getLogger(__name__)


class PositionManager:
    """
    持仓事件检测器。
    在每根 bar 上按优先级检查各类平仓/调整事件。
    优先级：止损 > 止盈 > 移动止损更新 > 分批止盈 > 强制平仓
    """

    def __init__(self, config: dict, fee_rate: float = 0.0005, slippage_pct: float = 0.001):
        """
        参数：
            config       : 合并后的完整配置字典（包含 trade_manager / trading 节点）
            fee_rate     : taker 手续费率
            slippage_pct : 滑点百分比
        """
        tm = config.get("trade_manager", {})
        self.trailing_trigger_pct  = tm.get("trailing_stop_trigger_pct", 15.0)
        self.partial_tp1_pct       = tm.get("partial_profit_trigger_pct", 25.0)
        self.partial_tp1_ratio     = tm.get("partial_profit_ratio_1", 0.3)
        self.partial_tp2_pct       = tm.get("partial_profit_trigger_pct_2", 50.0)
        self.partial_tp2_ratio     = tm.get("partial_profit_ratio_2", 0.5)
        self.force_close_pct       = tm.get("force_close_loss_pct", -15.0)
        self.support_buffer_pct    = tm.get("support_buffer_pct", 0.3)

        self.fee_rate     = fee_rate
        self.slippage_pct = slippage_pct

    # ─────────────────────────────────────────────────────────────────
    # 主入口：对单个持仓检查所有事件
    # ─────────────────────────────────────────────────────────────────

    def process_bar(
        self,
        position: Position,
        bar: dict,
    ) -> list[dict]:
        """
        在一根 bar 上检查持仓所有管理事件。
        返回事件列表，每个事件为 dict：
            {
              'event'  : 事件类型（'close' | 'partial_close' | 'update_sl'）,
              'reason' : 原因字符串,
              'price'  : 成交/调整价格,
              'ratio'  : 平仓比例（partial_close 时使用），
            }
        调用方根据事件列表决定如何修改持仓状态。
        """
        events = []
        high  = bar["high"]
        low   = bar["low"]
        close = bar["close"]

        # 1. 止损
        sl_event = self._check_stop_loss(position, high, low)
        if sl_event:
            events.append(sl_event)
            return events  # 止损触发后无需继续检查

        # 2. 止盈
        tp_event = self._check_take_profit(position, high, low)
        if tp_event:
            events.append(tp_event)
            return events

        # 3. 更新移动止损（不平仓，只调整 stop_loss）
        trailing_event = self._check_trailing_stop(position, high, low, close)
        if trailing_event:
            events.append(trailing_event)

        # 4. 分批止盈（部分平仓，继续持有剩余）
        partial_events = self._check_partial_tp(position, close)
        events.extend(partial_events)

        # 5. 强制平仓（兜底）
        force_event = self._check_force_close(position, close)
        if force_event:
            events.append(force_event)

        return events

    # ─────────────────────────────────────────────────────────────────
    # 各事件检测（纯函数）
    # ─────────────────────────────────────────────────────────────────

    def _check_stop_loss(
        self, position: Position, high: float, low: float
    ) -> Optional[dict]:
        """检查 bar 的 low/high 是否触穿止损价"""
        sl = position.stop_loss
        triggered = False
        if position.side == 'long' and low <= sl:
            triggered = True
        elif position.side == 'short' and high >= sl:
            triggered = True

        if triggered:
            price = self._apply_cost(sl, position.side, 'close')
            logger.debug(
                f"  SL触发 {position.symbol} {position.side} "
                f"entry={position.entry_price:.4f} sl={sl:.4f} price={price:.4f}"
            )
            return {"event": "close", "reason": "sl", "price": price, "ratio": 1.0}
        return None

    def _check_take_profit(
        self, position: Position, high: float, low: float
    ) -> Optional[dict]:
        """检查 bar 的 high/low 是否触及止盈价"""
        tp = position.take_profit
        triggered = False
        if position.side == 'long' and high >= tp:
            triggered = True
        elif position.side == 'short' and low <= tp:
            triggered = True

        if triggered:
            price = self._apply_cost(tp, position.side, 'close')
            logger.debug(
                f"  TP触发 {position.symbol} {position.side} "
                f"entry={position.entry_price:.4f} tp={tp:.4f} price={price:.4f}"
            )
            return {"event": "close", "reason": "tp", "price": price, "ratio": 1.0}
        return None

    def _check_trailing_stop(
        self, position: Position, high: float, low: float, close: float
    ) -> Optional[dict]:
        """
        检查是否触发移动止损激活，或更新移动止损价格。
        返回 update_sl 事件（不平仓），或 close 事件（触发移动止损平仓）。
        """
        pnl_pct = position.unrealized_pnl_pct(close)

        # 激活移动止损
        if not position.trailing_active and pnl_pct >= self.trailing_trigger_pct:
            position.trailing_active = True
            position.peak_price = high if position.side == 'long' else low
            # 将止损上移至保本位（入场价 + 手续费滑点成本）
            breakeven = self._breakeven_price(position)
            new_sl = max(breakeven, position.stop_loss) if position.side == 'long' else min(breakeven, position.stop_loss)
            logger.debug(
                f"  移动止损激活 {position.symbol} {position.side} "
                f"pnl={pnl_pct:.1f}% 止损上移至 {new_sl:.4f}"
            )
            return {"event": "update_sl", "reason": "trailing_activated", "price": new_sl, "ratio": 0.0}

        if not position.trailing_active:
            return None

        # 更新 peak_price
        if position.side == 'long':
            if high > (position.peak_price or 0):
                position.peak_price = high
        else:
            if low < (position.peak_price or float('inf')):
                position.peak_price = low

        # 检查是否触发移动止损
        if position.side == 'long' and low <= position.stop_loss:
            price = self._apply_cost(position.stop_loss, position.side, 'close')
            logger.debug(f"  移动止损触发 {position.symbol} long price={price:.4f}")
            return {"event": "close", "reason": "trailing_sl", "price": price, "ratio": 1.0}
        elif position.side == 'short' and high >= position.stop_loss:
            price = self._apply_cost(position.stop_loss, position.side, 'close')
            logger.debug(f"  移动止损触发 {position.symbol} short price={price:.4f}")
            return {"event": "close", "reason": "trailing_sl", "price": price, "ratio": 1.0}

        return None

    def _check_partial_tp(
        self, position: Position, close: float
    ) -> list[dict]:
        """检查两批分批止盈条件"""
        events = []
        pnl_pct = position.unrealized_pnl_pct(close)

        if not position.partial_tp1_done and pnl_pct >= self.partial_tp1_pct:
            price = self._apply_cost(close, position.side, 'close')
            logger.debug(
                f"  分批止盈1 {position.symbol} pnl={pnl_pct:.1f}% "
                f"平仓{self.partial_tp1_ratio*100:.0f}%"
            )
            events.append({
                "event":  "partial_close",
                "reason": "partial_tp1",
                "price":  price,
                "ratio":  self.partial_tp1_ratio,
            })

        if not position.partial_tp2_done and pnl_pct >= self.partial_tp2_pct:
            price = self._apply_cost(close, position.side, 'close')
            logger.debug(
                f"  分批止盈2 {position.symbol} pnl={pnl_pct:.1f}% "
                f"再平仓{self.partial_tp2_ratio*100:.0f}%"
            )
            events.append({
                "event":  "partial_close",
                "reason": "partial_tp2",
                "price":  price,
                "ratio":  self.partial_tp2_ratio,
            })

        return events

    def _check_force_close(
        self, position: Position, close: float
    ) -> Optional[dict]:
        """浮亏超过阈值时强制平仓（兜底机制）"""
        pnl_pct = position.unrealized_pnl_pct(close)
        if pnl_pct <= self.force_close_pct:
            price = self._apply_cost(close, position.side, 'close')
            logger.warning(
                f"  强制平仓 {position.symbol} {position.side} "
                f"pnl={pnl_pct:.1f}% <= {self.force_close_pct}%"
            )
            return {"event": "close", "reason": "force_close", "price": price, "ratio": 1.0}
        return None

    # ─────────────────────────────────────────────────────────────────
    # 成本计算工具
    # ─────────────────────────────────────────────────────────────────

    def _apply_cost(self, price: float, side: str, action: str) -> float:
        """
        对价格施加手续费和滑点。
        开仓/平仓多头：实际成交价略高于报价；空头相反。
        action: 'open' | 'close'
        """
        cost_pct = self.fee_rate + self.slippage_pct
        if action == 'open':
            # 开多：价格上滑；开空：价格下滑
            return price * (1 + cost_pct) if side == 'long' else price * (1 - cost_pct)
        else:
            # 平多：价格下滑；平空：价格上滑
            return price * (1 - cost_pct) if side == 'long' else price * (1 + cost_pct)

    def _breakeven_price(self, position: Position) -> float:
        """保本价格（回本至入场成本）"""
        cost_pct = self.fee_rate + self.slippage_pct
        if position.side == 'long':
            return position.entry_price * (1 + cost_pct * 2)
        else:
            return position.entry_price * (1 - cost_pct * 2)

    def calc_pnl(
        self,
        position: Position,
        close_price: float,
        ratio: float = 1.0,
    ) -> tuple[float, float]:
        """
        计算平仓 PnL。
        返回 (pnl_usdt, pnl_pct)
        pnl_pct 相对于本笔占用保证金
        """
        contracts = position.contracts * ratio
        notional = contracts * position.entry_price
        margin = notional / position.leverage

        if position.side == 'long':
            pnl_usdt = contracts * (close_price - position.entry_price)
        else:
            pnl_usdt = contracts * (position.entry_price - close_price)

        pnl_pct = (pnl_usdt / margin * 100) if margin > 0 else 0.0
        return round(pnl_usdt, 4), round(pnl_pct, 4)
