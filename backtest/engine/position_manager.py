#!/usr/bin/env python3
from __future__ import annotations
"""
backtest/engine/position_manager.py
持仓管理器（纯计算，无任何 IO 依赖）

复用生产 trade_manager.py 的核心逻辑，去除所有交易所 API 调用。
所有函数为纯函数，便于单元测试。
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from .position import Position

logger = logging.getLogger(__name__)

# ── 将生产 scripts/ 目录注入 sys.path 以复用函数 ─────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# 懒加载生产函数（避免导入时的副作用）
_detect_trend_structure = None
_calculate_support_resistance = None
_detect_momentum_decay = None
_compute_atr = None
_calculate_trailing_stop = None


class PositionManager:
    """
    持仓事件检测器。
    在每根 bar 上按优先级检查各类平仓/调整事件。
    优先级：止损 > 止盈 > 移动止损更新 > 分批止盈 > 结构破坏 > 动量衰减 > 支撑阻力突破 > 强制平仓
    """

    def __init__(self, config: dict, fee_rate: float = 0.0005, slippage_pct: float = 0.001):
        """
        参数：
            config       : 合并后的完整配置字典（包含 trade_manager / trading 节点）
            fee_rate     : taker 手续费率
            slippage_pct : 滑点百分比
        """
        self.config = config

        tm = config.get("trade_manager", {})
        self.trailing_trigger_pct  = tm.get("trailing_stop_trigger_pct", 15.0)
        self.partial_tp1_pct       = tm.get("partial_profit_trigger_pct", 25.0)
        self.partial_tp1_ratio     = tm.get("partial_profit_ratio_1", 0.3)
        self.partial_tp2_pct       = tm.get("partial_profit_trigger_pct_2", 50.0)
        self.partial_tp2_ratio     = tm.get("partial_profit_ratio_2", 0.5)
        self.force_close_pct       = tm.get("force_close_loss_pct", -15.0)
        self.support_buffer_pct    = tm.get("support_buffer_pct", 0.3)

        # 结构破坏检测配置
        self.structure_check_timeframe = tm.get("structure_check_timeframe", "1h")

        # 动量衰减检测配置
        self.momentum_decay_enabled = tm.get("momentum_decay_exit_enabled", True)
        self.momentum_decay_min_profit_pct = tm.get("momentum_decay_min_profit_pct", 5.0)

        # ATR 动态止损配置
        trading_cfg = config.get("trading", {})
        self.trailing_atr_multiplier = trading_cfg.get("trailing_stop_atr_multiplier", 1.5)

        # 基础时间框架（用于动量衰减等检测）
        self.timeframes = config.get("timeframes", ["1h", "15m", "5m"])
        self.base_timeframe = self.timeframes[-1] if self.timeframes else "15m"

        self.fee_rate     = fee_rate
        self.slippage_pct = slippage_pct

        # 性能优化：检测频率控制
        self._bar_counter: dict[str, int] = {}  # {symbol: bar_count}
        self._structure_check_interval = 5  # 每5根bar检查一次结构
        self._atr_cache: dict[str, float] = {}  # {symbol: atr_value}

    # ─────────────────────────────────────────────────────────────────
    # 主入口：对单个持仓检查所有事件
    # ─────────────────────────────────────────────────────────────────

    def process_bar(
        self,
        position: Position,
        bar: dict,
        multi_tf_data: dict = None,  # 多周期K线数据 {tf: DataFrame}
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

        参数：
            position      : 持仓对象
            bar           : 当前K线数据 {open, high, low, close, volume}
            multi_tf_data : 多周期K线数据（用于结构破坏、动量衰减等检测）
        """
        if multi_tf_data is None:
            multi_tf_data = {}

        events = []
        high  = bar["high"]
        low   = bar["low"]
        close = bar["close"]

        # 更新 bar 计数器（用于频率控制）
        self._bar_counter[position.symbol] = self._bar_counter.get(position.symbol, 0) + 1
        bar_count = self._bar_counter[position.symbol]

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
        trailing_event = self._check_trailing_stop(position, high, low, close, multi_tf_data)
        if trailing_event:
            events.append(trailing_event)

        # 4. 分批止盈（部分平仓，继续持有剩余）
        partial_events = self._check_partial_tp(position, close)
        events.extend(partial_events)

        # 以下检测需要多周期数据，且使用频率控制
        check_advanced = (bar_count % self._structure_check_interval == 0) and bool(multi_tf_data)

        # 5. 结构破坏平仓（新增）
        if check_advanced:
            structure_event = self._check_structure_break(position, multi_tf_data)
            if structure_event:
                events.append(structure_event)
                return events

        # 6. 动量衰减出场（新增）
        if check_advanced:
            decay_event = self._check_momentum_decay(position, close, multi_tf_data)
            if decay_event:
                events.append(decay_event)
                return events

        # 7. 支撑/阻力突破平仓（新增）
        if check_advanced:
            sr_event = self._check_support_resistance_break(position, multi_tf_data)
            if sr_event:
                events.append(sr_event)
                return events

        # 8. 强制平仓（兜底）
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
        self,
        position: Position,
        high: float,
        low: float,
        close: float,
        multi_tf_data: dict = None,
    ) -> Optional[dict]:
        """
        检查是否触发移动止损激活，或更新移动止损价格。

        改进：第一批止盈后使用 ATR 动态止损替代固定百分比。
        返回 update_sl 事件（不平仓），或 close 事件（触发移动止损平仓）。
        """
        if multi_tf_data is None:
            multi_tf_data = {}

        pnl_pct = position.unrealized_pnl_pct(close)

        # 激活条件：第一批止盈完成后（与生产逻辑对齐）
        # 生产代码：第一批止盈后立即激活 ATR 跟踪止损
        if not position.trailing_active and position.partial_tp1_done:
            position.trailing_active = True
            position.peak_price = high if position.side == 'long' else low
            # 将止损上移至保本位
            breakeven = self._breakeven_price(position)
            new_sl = max(breakeven, position.stop_loss) if position.side == 'long' else min(breakeven, position.stop_loss)
            logger.debug(
                f"  移动止损激活（止盈后） {position.symbol} {position.side} "
                f"止损上移至保本位 {new_sl:.4f}"
            )
            return {"event": "update_sl", "reason": "trailing_activated", "price": new_sl, "ratio": 0.0}

        # 兼容旧逻辑：未分批止盈时，使用百分比触发
        if not position.trailing_active and pnl_pct >= self.trailing_trigger_pct:
            position.trailing_active = True
            position.peak_price = high if position.side == 'long' else low
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

        # 计算新止损价：优先使用 ATR 动态止损
        use_atr = position.partial_tp1_done and bool(multi_tf_data)
        if use_atr:
            atr = self._get_atr(position.symbol, multi_tf_data)
            if atr > 0:
                new_trail_sl = self._calculate_trailing_stop_atr(
                    (position.peak_price or close), atr, position.side
                )
                # 确保 ATR 止损不低于保本位
                breakeven = self._breakeven_price(position)
                if position.side == 'long':
                    new_trail_sl = max(new_trail_sl, breakeven)
                else:
                    new_trail_sl = min(new_trail_sl, breakeven)
            else:
                # fallback: 固定百分比
                if position.side == 'long':
                    new_trail_sl = (position.peak_price or close) * (1 - self.trailing_trigger_pct / 100)
                else:
                    new_trail_sl = (position.peak_price or close) * (1 + self.trailing_trigger_pct / 100)
        else:
            # 固定百分比
            if position.side == 'long':
                new_trail_sl = (position.peak_price or close) * (1 - self.trailing_trigger_pct / 100)
            else:
                new_trail_sl = (position.peak_price or close) * (1 + self.trailing_trigger_pct / 100)

        # 更新止损（只往有利方向移动）
        if position.side == 'long':
            if new_trail_sl > position.stop_loss:
                old_sl = position.stop_loss
                position.stop_loss = new_trail_sl
                method = "ATR" if use_atr else "固定%"
                logger.debug(
                    f"  移动止损上移 [{method}] {position.symbol} long "
                    f"peak={position.peak_price:.4f} {old_sl:.4f}→{new_trail_sl:.4f}"
                )
        else:
            if new_trail_sl < position.stop_loss:
                old_sl = position.stop_loss
                position.stop_loss = new_trail_sl
                method = "ATR" if use_atr else "固定%"
                logger.debug(
                    f"  移动止损下移 [{method}] {position.symbol} short "
                    f"peak={position.peak_price:.4f} {old_sl:.4f}→{new_trail_sl:.4f}"
                )

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

    # ─────────────────────────────────────────────────────────────────
    # 新增检测：结构破坏、动量衰减、支撑阻力
    # ─────────────────────────────────────────────────────────────────

    def _check_structure_break(
        self,
        position: Position,
        multi_tf_data: dict,
    ) -> Optional[dict]:
        """
        检测趋势结构破坏（复用生产 detect_trend_structure）。

        多头结构破坏：价格跌破前一波段低点
        空头结构破坏：价格突破前一波段高点
        """
        structure_df = multi_tf_data.get(self.structure_check_timeframe)
        if structure_df is None or structure_df.empty or len(structure_df) < 10:
            return None

        # 懒加载生产函数
        global _detect_trend_structure
        if _detect_trend_structure is None:
            from fetch_kline import detect_trend_structure as _detect_trend_structure

        try:
            structure_result = _detect_trend_structure(structure_df)
        except Exception as e:
            logger.debug(f"结构检测异常 {position.symbol}: {e}")
            return None

        current_price = float(structure_df.iloc[-1]["close"])

        # 多头结构破坏
        if position.side == 'long' and structure_result.get("structure_broken_long", False):
            price = self._apply_cost(current_price, position.side, 'close')
            logger.info(
                f"  结构破坏 [多头] {position.symbol} "
                f"价格跌破前低，当前价={current_price:.4f}"
            )
            return {
                "event": "close",
                "reason": f"structure_break_long_{self.structure_check_timeframe}",
                "price": price,
                "ratio": 1.0,
            }

        # 空头结构破坏
        if position.side == 'short' and structure_result.get("structure_broken_short", False):
            price = self._apply_cost(current_price, position.side, 'close')
            logger.info(
                f"  结构破坏 [空头] {position.symbol} "
                f"价格突破前高，当前价={current_price:.4f}"
            )
            return {
                "event": "close",
                "reason": f"structure_break_short_{self.structure_check_timeframe}",
                "price": price,
                "ratio": 1.0,
            }

        return None

    def _check_momentum_decay(
        self,
        position: Position,
        close: float,
        multi_tf_data: dict,
    ) -> Optional[dict]:
        """
        检测动量衰减（复用生产 detect_momentum_decay）。

        条件1：连续N根K线实体逐渐缩小
        条件2：出现方向相反的显著影线
        """
        if not self.momentum_decay_enabled:
            return None

        # 浮盈门槛检查
        pnl_pct = position.unrealized_pnl_pct(close)
        if pnl_pct < self.momentum_decay_min_profit_pct:
            return None

        # 使用基础周期数据
        decay_df = multi_tf_data.get(self.base_timeframe)
        if decay_df is None or decay_df.empty or len(decay_df) < 5:
            return None

        # 懒加载生产函数
        global _detect_momentum_decay
        if _detect_momentum_decay is None:
            from indicator_engine import detect_momentum_decay as _detect_momentum_decay

        try:
            decay_result = _detect_momentum_decay(decay_df, position.side)
        except Exception as e:
            logger.debug(f"动量衰减检测异常 {position.symbol}: {e}")
            return None

        if decay_result.get("decaying"):
            price = self._apply_cost(close, position.side, 'close')
            logger.info(
                f"  动量衰减 {position.symbol} {position.side} "
                f"pnl={pnl_pct:.1f}% | {decay_result.get('reason', '')}"
            )
            return {
                "event": "close",
                "reason": "momentum_decay",
                "price": price,
                "ratio": 1.0,
            }

        return None

    def _check_support_resistance_break(
        self,
        position: Position,
        multi_tf_data: dict,
    ) -> Optional[dict]:
        """
        检测支撑/阻力突破（复用生产 calculate_support_resistance）。

        多头：价格跌破支撑位（用 bar 的 low 判断）
        空头：价格突破阻力位（用 bar 的 high 判断）

        注意：使用 high/low 而非 close，与止损检测保持一致。
        """
        structure_df = multi_tf_data.get(self.structure_check_timeframe)
        if structure_df is None or structure_df.empty or len(structure_df) < 10:
            return None

        # 懒加载生产函数
        global _calculate_support_resistance
        if _calculate_support_resistance is None:
            from fetch_kline import calculate_support_resistance as _calculate_support_resistance

        try:
            supports, resistances = _calculate_support_resistance(structure_df)
        except Exception as e:
            logger.debug(f"支撑阻力计算异常 {position.symbol}: {e}")
            return None

        # 使用 bar 的 high/low，与止损检测保持一致
        last_bar = structure_df.iloc[-1]
        bar_high = float(last_bar["high"])
        bar_low = float(last_bar["low"])

        # 优先使用开仓时AI标注的关键位
        ai_support = position.key_support
        ai_resistance = position.key_resistance

        # 多头：检测跌破支撑（用 low 判断）
        if position.side == 'long':
            nearest_support = ai_support if ai_support else (supports[0] if supports else None)
            if nearest_support:
                buffer = 1 - self.support_buffer_pct / 100
                if bar_low < nearest_support * buffer:
                    price = self._apply_cost(bar_low, position.side, 'close')
                    logger.info(
                        f"  支撑跌破 {position.symbol} [多头] "
                        f"支撑={nearest_support:.6g} bar_low={bar_low:.4f}"
                    )
                    return {
                        "event": "close",
                        "reason": f"support_break_{nearest_support:.6g}",
                        "price": price,
                        "ratio": 1.0,
                    }

        # 空头：检测突破阻力（用 high 判断）
        elif position.side == 'short':
            nearest_resistance = ai_resistance if ai_resistance else (resistances[0] if resistances else None)
            if nearest_resistance:
                buffer = 1 + self.support_buffer_pct / 100
                if bar_high > nearest_resistance * buffer:
                    price = self._apply_cost(bar_high, position.side, 'close')
                    logger.info(
                        f"  阻力突破 {position.symbol} [空头] "
                        f"阻力={nearest_resistance:.6g} bar_high={bar_high:.4f}"
                    )
                    return {
                        "event": "close",
                        "reason": f"resistance_break_{nearest_resistance:.6g}",
                        "price": price,
                        "ratio": 1.0,
                    }

        return None

    # ─────────────────────────────────────────────────────────────────
    # 辅助函数：ATR 计算与缓存
    # ─────────────────────────────────────────────────────────────────

    def _get_atr(self, symbol: str, multi_tf_data: dict) -> float:
        """
        获取 ATR 值（带缓存）。

        使用基础周期（如 15m）的 ATR。
        """
        # 检查缓存
        if symbol in self._atr_cache:
            return self._atr_cache[symbol]

        atr_df = multi_tf_data.get(self.base_timeframe)
        if atr_df is None or atr_df.empty or len(atr_df) < 14:
            return 0.0

        # 懒加载生产函数
        global _compute_atr
        if _compute_atr is None:
            try:
                from indicator_engine import compute_adx
                _compute_atr = lambda df: compute_adx(df).get("atr", 0.0)
            except Exception:
                _compute_atr = lambda df: 0.0

        try:
            atr = _compute_atr(atr_df)
            if isinstance(atr, dict):
                atr = atr.get("atr", 0.0)
            atr = float(atr) if atr else 0.0
        except Exception:
            atr = 0.0

        # 缓存
        self._atr_cache[symbol] = atr
        return atr

    def _calculate_trailing_stop_atr(
        self,
        current_price: float,
        atr: float,
        side: str,
    ) -> float:
        """
        计算 ATR 动态跟踪止损价（复用生产 calculate_trailing_stop）。
        """
        if atr <= 0:
            return 0.0

        # 懒加载生产函数
        global _calculate_trailing_stop
        if _calculate_trailing_stop is None:
            try:
                from dynamic_stop_take_profit import calculate_trailing_stop as _calculate_trailing_stop
            except Exception:
                # fallback: 使用简单 ATR 倍数计算
                _calculate_trailing_stop = lambda price, atr_val, sig: (
                    price - atr_val * self.trailing_atr_multiplier if sig == 'long'
                    else price + atr_val * self.trailing_atr_multiplier
                )

        try:
            return _calculate_trailing_stop(current_price, atr, side)
        except Exception:
            # fallback
            if side == 'long':
                return current_price - atr * self.trailing_atr_multiplier
            else:
                return current_price + atr * self.trailing_atr_multiplier
