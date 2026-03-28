#!/usr/bin/env python3
from __future__ import annotations
"""
backtest/engine/engine.py
核心回测引擎

bar-by-bar 驱动，管理信号→开仓→持仓管理→平仓全流程。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from ..data.feed import DataFeed
from .position import Position
from .position_manager import PositionManager

logger = logging.getLogger(__name__)

_UTC = timezone.utc


class BacktestEngine:
    def __init__(
        self,
        config: dict,
        data_feed: DataFeed,
        signal_pipeline,          # backtest.signal.pipeline.SignalPipeline
    ):
        """
        参数：
            config          : 完整合并配置（backtest + settings.yaml override）
            data_feed       : DataFeed 实例（已 load()）
            signal_pipeline : SignalPipeline 实例
        """
        self.config = config
        self.feed   = data_feed
        self.pipeline = signal_pipeline

        bt_cfg = config.get("backtest", {})
        self.initial_balance     = bt_cfg.get("initial_balance", 10000.0)
        self.leverage            = bt_cfg.get("leverage", 10)
        self.fee_rate            = bt_cfg.get("fee_rate", 0.0005)
        self.slippage_pct        = bt_cfg.get("slippage_pct", 0.001)
        self.signal_interval     = bt_cfg.get("signal_interval_bars", 4)
        self.max_positions       = config.get("risk", {}).get("max_open_positions", 3)
        self.max_daily_loss_pct  = config.get("risk", {}).get("max_daily_loss_pct", -5.0)

        self.pos_manager = PositionManager(
            config=config,
            fee_rate=self.fee_rate,
            slippage_pct=self.slippage_pct,
        )

        # ── 运行时状态 ──────────────────────────────────
        self.balance:   float = self.initial_balance
        self.positions: list[Position] = []      # 当前持仓
        self.trades:    list[dict]     = []      # 已平仓记录
        self.equity_curve: list[dict]  = []      # 逐bar权益快照

        self._bar_counter:  dict[str, int] = {}  # {symbol: bar_count}
        self._daily_pnl:    dict[str, float] = {}  # {date_str: pnl_usdt}
        self._day_open_balance: float = self.initial_balance

    # ─────────────────────────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        执行回测主循环。
        遍历所有品种的 base_timeframe bars，逐根处理。
        多品种时以时间戳合并排序后统一驱动。
        返回回测结果字典。
        """
        symbols = self.feed.available_symbols()
        if not symbols:
            logger.error("DataFeed 无有效数据，回测终止")
            return self._build_result()

        logger.info(
            f"回测引擎启动：{len(symbols)} 个品种，"
            f"初始资金={self.initial_balance} USDT，"
            f"最大持仓={self.max_positions}，"
            f"信号间隔={self.signal_interval} bars"
        )

        # 构建多品种合并时间轴：[(ts, symbol), ...] 按 ts 升序
        timeline = []
        for symbol in symbols:
            for ts in self.feed.get_all_timestamps(symbol):
                timeline.append((ts, symbol))
        timeline.sort(key=lambda x: x[0])

        total_bars = len(timeline)
        logger.info(f"总 bar 数：{total_bars}")

        prev_date = None
        for i, (ts, symbol) in enumerate(timeline):
            bar = self.feed.get_bar_at(symbol, self.feed.base_timeframe, ts)
            if bar is None:
                continue

            date_str = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")

            # 新的一天：重置日亏损计数
            if date_str != prev_date:
                self._on_new_day(date_str)
                prev_date = date_str

            self._on_bar(ts, symbol, bar)

            # 每1000根bar记录一次进度
            if (i + 1) % 1000 == 0:
                equity = self._calc_equity(bar["close"])
                logger.info(
                    f"进度 {i+1}/{total_bars} | "
                    f"日期={date_str} | "
                    f"权益={equity:.2f} USDT | "
                    f"持仓={len(self.positions)} | "
                    f"已成交={len(self.trades)}"
                )

        # 回测结束：强制平仓所有持仓
        self._close_all_eod()
        logger.info(
            f"回测完成：总交易={len(self.trades)}，"
            f"最终权益={self.balance:.2f} USDT"
        )
        return self._build_result()

    # ─────────────────────────────────────────────────────────────────
    # Bar 处理
    # ─────────────────────────────────────────────────────────────────

    def _on_bar(self, ts: int, symbol: str, bar: dict) -> None:
        """处理单根 bar：先管理持仓，再检查新信号"""
        close = bar["close"]

        # 1. 管理当前持仓（该品种）
        for pos in list(self.positions):
            if pos.symbol == symbol:
                self._process_position(pos, bar)

        # 2. 记录权益曲线快照
        equity = self._calc_equity(close)
        self.equity_curve.append({"timestamp": ts, "symbol": symbol, "equity": equity, "balance": self.balance})

        # 3. 检查信号（每 signal_interval 根触发一次）
        self._bar_counter[symbol] = self._bar_counter.get(symbol, 0) + 1
        if self._bar_counter[symbol] % self.signal_interval != 0:
            return

        # 检查日亏损上限
        if self._is_daily_loss_exceeded():
            return

        # 检查最大持仓数
        open_count = len(self.positions)
        if open_count >= self.max_positions:
            return

        # 检查该品种是否已有持仓
        if any(p.symbol == symbol for p in self.positions):
            return

        # 生成信号
        signal = self.pipeline.generate_signal(symbol, ts, self.feed)
        if signal is None:
            return

        self._open_position(symbol, ts, signal, close)

    def _process_position(self, position: Position, bar: dict) -> None:
        """对单个持仓处理 bar 上的所有管理事件"""
        events = self.pos_manager.process_bar(position, bar)
        for evt in events:
            event_type = evt["event"]
            if event_type == "close":
                self._close_position(position, evt["price"], evt["reason"])
                return
            elif event_type == "partial_close":
                self._partial_close(position, evt["price"], evt["reason"], evt["ratio"])
            elif event_type == "update_sl":
                position.stop_loss = evt["price"]
                logger.debug(
                    f"  止损更新 {position.symbol} → {evt['price']:.4f} ({evt['reason']})"
                )

    # ─────────────────────────────────────────────────────────────────
    # 开仓 / 平仓
    # ─────────────────────────────────────────────────────────────────

    def _open_position(self, symbol: str, ts: int, signal: dict, current_price: float) -> None:
        """根据信号开仓，下一根 bar open 成交模拟（此处直接用信号入场价）"""
        entry_price = self.pos_manager._apply_cost(
            signal["entry_price"], signal["side"], 'open'
        )
        contracts   = signal.get("contracts", 0.0)
        margin      = contracts * entry_price / self.leverage

        if margin <= 0 or margin > self.balance:
            logger.debug(
                f"  {symbol} 开仓跳过：保证金不足 "
                f"(需要={margin:.2f}, 可用={self.balance:.2f})"
            )
            return

        self.balance -= margin

        pos = Position(
            symbol=symbol,
            side=signal["side"],
            entry_price=entry_price,
            contracts=contracts,
            stop_loss=signal["stop_loss"],
            take_profit=signal["take_profit"],
            open_time=ts,
            leverage=self.leverage,
            signal_strength=signal.get("signal_strength", 0),
            key_support=signal.get("key_support"),
            key_resistance=signal.get("key_resistance"),
            signal_reason=signal.get("reason", ""),
        )
        self.positions.append(pos)
        logger.info(
            f"开仓 {symbol} {signal['side'].upper()} "
            f"entry={entry_price:.4f} sl={signal['stop_loss']:.4f} "
            f"tp={signal['take_profit']:.4f} margin={margin:.2f} USDT"
        )

    def _close_position(self, position: Position, price: float, reason: str) -> None:
        """全仓平仓，释放保证金，记录交易"""
        pnl_usdt, pnl_pct = self.pos_manager.calc_pnl(position, price, ratio=1.0)
        margin = position.margin_usdt

        position.close_time   = None  # 由调用方补充当前 ts
        position.close_price  = price
        position.close_reason = reason
        position.pnl_usdt     = pnl_usdt
        position.pnl_pct      = pnl_pct

        self.balance += margin + pnl_usdt
        if self.balance < 0:
            self.balance = 0.0

        # 更新日亏损统计
        self._update_daily_pnl(pnl_usdt)

        trade_record = position.to_dict()
        self.trades.append(trade_record)
        self.positions.remove(position)

        icon = "盈" if pnl_usdt >= 0 else "亏"
        logger.info(
            f"平仓[{reason}] {position.symbol} {position.side.upper()} "
            f"entry={position.entry_price:.4f} exit={price:.4f} "
            f"{icon}={pnl_usdt:+.2f} USDT ({pnl_pct:+.1f}%) "
            f"余额={self.balance:.2f}"
        )

    def _partial_close(self, position: Position, price: float, reason: str, ratio: float) -> None:
        """分批平仓，按 ratio 比例平掉部分合约"""
        if reason == "partial_tp1" and position.partial_tp1_done:
            return
        if reason == "partial_tp2" and position.partial_tp2_done:
            return

        actual_ratio = ratio * position.remaining_ratio
        closed_contracts = position.contracts * actual_ratio
        pnl_usdt, pnl_pct = self.pos_manager.calc_pnl(position, price, ratio=actual_ratio)
        released_margin = closed_contracts * position.entry_price / self.leverage

        self.balance += released_margin + pnl_usdt
        self._update_daily_pnl(pnl_usdt)

        # 更新持仓状态
        if reason == "partial_tp1":
            position.partial_tp1_done = True
            position.remaining_ratio -= actual_ratio
        elif reason == "partial_tp2":
            position.partial_tp2_done = True
            position.remaining_ratio -= actual_ratio

        logger.info(
            f"分批止盈[{reason}] {position.symbol} "
            f"平仓{actual_ratio*100:.0f}% price={price:.4f} "
            f"pnl={pnl_usdt:+.2f} USDT 剩余仓位={position.remaining_ratio*100:.0f}%"
        )

    def _close_all_eod(self) -> None:
        """回测结束强制平仓所有持仓"""
        for pos in list(self.positions):
            last_bar = self.feed.get_history(pos.symbol, self.feed.base_timeframe, self._end_ms(), limit=1)
            price = float(last_bar.iloc[-1]["close"]) if not last_bar.empty else pos.entry_price
            self._close_position(pos, price, "eod")

    def _end_ms(self) -> int:
        """回测截止时间戳（毫秒）"""
        from datetime import datetime, timezone
        end_date = self.config.get("backtest", {}).get("end_date", "2099-01-01")
        return int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() * 1000)

    # ─────────────────────────────────────────────────────────────────
    # 日亏损 / 权益计算
    # ─────────────────────────────────────────────────────────────────

    def _on_new_day(self, date_str: str) -> None:
        self._daily_pnl[date_str] = 0.0
        self._day_open_balance = self.balance

    def _update_daily_pnl(self, pnl_usdt: float) -> None:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + pnl_usdt

    def _is_daily_loss_exceeded(self) -> bool:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = self._daily_pnl.get(today, 0.0)
        if self._day_open_balance <= 0:
            return False
        pct = daily_pnl / self._day_open_balance * 100
        if pct <= self.max_daily_loss_pct:
            logger.debug(f"日亏损上限触发：{pct:.1f}% <= {self.max_daily_loss_pct}%")
            return True
        return False

    def _calc_equity(self, current_price: float) -> float:
        """当前总权益 = 可用余额 + 所有持仓的浮动盈亏 + 占用保证金"""
        floating_pnl = sum(
            p.contracts * (current_price - p.entry_price)
            if p.side == 'long'
            else p.contracts * (p.entry_price - current_price)
            for p in self.positions
        )
        occupied_margin = sum(p.margin_usdt for p in self.positions)
        return self.balance + occupied_margin + floating_pnl

    # ─────────────────────────────────────────────────────────────────
    # 结果构建
    # ─────────────────────────────────────────────────────────────────

    def _build_result(self) -> dict:
        return {
            "trades":       self.trades,
            "equity_curve": self.equity_curve,
            "final_balance": self.balance,
            "initial_balance": self.initial_balance,
            "total_trades":  len(self.trades),
            "daily_pnl":    self._daily_pnl,
        }
