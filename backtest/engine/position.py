#!/usr/bin/env python3
from __future__ import annotations
"""
backtest/engine/position.py
持仓数据类
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Position:
    """
    单笔持仓记录。
    open_time / close_time 均为 UTC Unix 毫秒时间戳（int）。
    """
    symbol: str
    side: str                    # 'long' | 'short'
    entry_price: float
    contracts: float             # 合约张数（以 USDT 名义价值计算）
    stop_loss: float
    take_profit: float
    open_time: int               # UTC ms
    leverage: int = 10

    # ── 平仓信息（开仓时为 None）────────────────────────
    close_time: Optional[int] = None        # UTC ms
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    # close_reason 枚举：
    #   'tp'          止盈触发
    #   'sl'          止损触发
    #   'trailing_sl' 移动止损触发
    #   'partial_tp1' 第一批分批止盈
    #   'partial_tp2' 第二批分批止盈
    #   'force_close' 强制平仓（兜底）
    #   'structure'   结构破坏平仓
    #   'eod'         回测结束强制平仓

    pnl_usdt: float = 0.0
    pnl_pct: float = 0.0         # 相对保证金的百分比收益

    # ── 移动止损跟踪 ─────────────────────────────────
    trailing_active: bool = False
    peak_price: Optional[float] = None   # 多头最高价 / 空头最低价
    original_stop_loss: Optional[float] = None

    # ── 分批止盈跟踪 ─────────────────────────────────
    partial_tp1_done: bool = False
    partial_tp2_done: bool = False
    # 分批止盈后剩余合约比例（1.0 = 全仓，0.7 = 剩余70%）
    remaining_ratio: float = 1.0

    # ── 扩展信息（来自信号）────────────────────────────
    signal_strength: int = 0
    key_support: Optional[float] = None
    key_resistance: Optional[float] = None
    signal_reason: str = ""

    # ── 分析维度：R:R 比率 ───────────────────────────────
    entry_atr: float = 0.0              # 入场时的 ATR 值
    stop_loss_distance: float = 0.0     # 止损距离（价格差）
    take_profit_distance: float = 0.0   # 止盈距离（价格差）
    stop_loss_atr_multiple: float = 0.0 # 止损 ATR 倍数
    take_profit_atr_multiple: float = 0.0 # 止盈 ATR 倍数
    risk_reward_ratio: float = 0.0      # R:R 比率

    # ── 分析维度：入场指标状态 ───────────────────────────
    entry_adx: float = 0.0              # 入场时 ADX 值
    entry_rsi: float = 0.0              # 入场时 RSI 值（基础周期）
    entry_ema_score: int = 0            # 入场时 EMA 对齐得分 (0-3)
    entry_volume_ratio: float = 1.0     # 入场时量比

    # ── 分析维度：K线形态 ───────────────────────────────
    entry_pattern: str = ""             # 入场K线形态 (pin_bar, engulfing, none)

    # ── 分析维度：时间 ───────────────────────────────
    entry_hour: int = 0                 # 入场小时 (0-23, UTC)

    # ── 内部 ID ──────────────────────────────────────
    position_id: str = ""

    def __post_init__(self):
        if not self.position_id:
            self.position_id = f"{self.symbol}_{self.side}_{self.open_time}"
        if self.original_stop_loss is None:
            self.original_stop_loss = self.stop_loss

    @property
    def is_open(self) -> bool:
        return self.close_time is None

    @property
    def notional_usdt(self) -> float:
        """名义价值 = 合约张数 × 入场价（USDT 计价永续合约）"""
        return self.contracts * self.entry_price

    @property
    def margin_usdt(self) -> float:
        """占用保证金"""
        return self.notional_usdt / self.leverage

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """当前浮动盈亏百分比（相对保证金）"""
        if self.side == 'long':
            price_pct = (current_price - self.entry_price) / self.entry_price
        else:
            price_pct = (self.entry_price - current_price) / self.entry_price
        return price_pct * self.leverage * 100

    def to_dict(self) -> dict:
        """序列化为字典，用于报告输出"""
        return {
            "position_id":     self.position_id,
            "symbol":          self.symbol,
            "side":            self.side,
            "status":          "closed" if self.close_time is not None else "open",
            "entry_price":     self.entry_price,
            "contracts":       self.contracts,
            "stop_loss":       self.stop_loss,
            "take_profit":     self.take_profit,
            "open_time":       self.open_time,
            "close_time":      self.close_time,
            "close_price":     self.close_price,
            "close_reason":    self.close_reason,
            "pnl_usdt":        round(self.pnl_usdt, 4),
            "pnl_pct":         round(self.pnl_pct, 4),
            "leverage":        self.leverage,
            "margin_usdt":     round(self.margin_usdt, 4),
            "trailing_active": self.trailing_active,
            "partial_tp1":     self.partial_tp1_done,
            "partial_tp2":     self.partial_tp2_done,
            "signal_strength": self.signal_strength,
            "signal_reason":   self.signal_reason,
            # 分析维度：R:R 比率
            "entry_atr":       round(self.entry_atr, 6) if self.entry_atr else 0.0,
            "sl_atr_mult":     round(self.stop_loss_atr_multiple, 2) if self.stop_loss_atr_multiple else 0.0,
            "tp_atr_mult":     round(self.take_profit_atr_multiple, 2) if self.take_profit_atr_multiple else 0.0,
            "risk_reward":     round(self.risk_reward_ratio, 2) if self.risk_reward_ratio else 0.0,
            # 分析维度：入场指标
            "entry_adx":       round(self.entry_adx, 1) if self.entry_adx else 0.0,
            "entry_rsi":       round(self.entry_rsi, 1) if self.entry_rsi else 0.0,
            "ema_score":       self.entry_ema_score,
            "volume_ratio":    round(self.entry_volume_ratio, 2) if self.entry_volume_ratio else 1.0,
            # 分析维度：K线形态
            "entry_pattern":   self.entry_pattern,
            # 分析维度：时间
            "entry_hour":      self.entry_hour,
        }
