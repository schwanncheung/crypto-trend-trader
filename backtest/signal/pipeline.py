#!/usr/bin/env python3
from __future__ import annotations
"""
backtest/signal/pipeline.py
信号生成流水线

直接复用生产代码 indicator_engine / risk_filter，严格无前视偏差：
  1. 从 DataFeed.get_history() 取历史K线切片
  2. 调用 generate_market_snapshot() 计算指标
  3. 调用 rule_engine_filter() 预过滤
  4. 通过 → 调用 AI Mock 构造决策
  5. 调用 check_signal_quality() 信号质量检查
  6. 调用 calculate_position_size() 计算仓位
  7. 返回可执行信号 dict，或 None（不交易）
"""

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 将生产 scripts/ 目录注入 sys.path ──────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


class SignalPipeline:
    """
    回测信号流水线。

    与生产 market_scanner.py 逻辑对齐，但：
    - 从 DataFeed 取历史切片，而非实时 API
    - AI 分析由 ai_mock（RuleOnlyMock 或 LLMMockCache）替代
    - 不触发任何 IO（通知/日志写文件除外）
    """

    def __init__(self, config: dict, ai_mock):
        """
        参数：
            config  : 完整合并配置（backtest + settings.yaml override 后的结果）
            ai_mock : RuleOnlyMock 或 LLMMockCache 实例
        """
        self.config   = config
        self.ai_mock  = ai_mock
        self.timeframes = config.get("timeframes", ["1h", "30m", "15m"])

        # 从配置读取 K 线数量限制（与生产保持一致）
        kline_limits_cfg = config.get("kline", {}).get("limits", {})
        self._default_limit = 300
        self._kline_limits = {tf: kline_limits_cfg.get(tf, self._default_limit)
                               for tf in self.timeframes}

        # 懒加载生产模块（避免 import 时就触发 check_env 等副作用）
        self._indicator_engine = None
        self._risk_filter      = None
        self._fetch_kline      = None

        # 引擎实时余额注入（由 BacktestEngine 在每次开仓前更新）
        self.available_balance: float = self.config.get("backtest", {}).get("initial_balance", 10000.0)

        logger.info(
            f"SignalPipeline 初始化：timeframes={self.timeframes}，"
            f"ai_mode={config.get('backtest', {}).get('ai_mode', 'rule_only')}"
        )

    # ─────────────────────────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────────────────────────

    def generate_signal(
        self,
        symbol: str,
        ts_ms: int,
        data_feed,          # DataFeed 实例
    ) -> Optional[dict]:
        """
        在指定时间戳生成交易信号。

        参数：
            symbol   : 品种，如 'BTC/USDT:USDT'
            ts_ms    : 当前 bar 的收盘时间戳（Unix ms），严格不包含此时刻之后的数据
            data_feed: DataFeed 实例（已 load()）

        返回：
            可执行信号 dict（含 side/entry_price/stop_loss/take_profit/contracts 等），
            或 None（不产生交易）
        """
        ie = self._get_indicator_engine()
        rf = self._get_risk_filter()
        fk = self._get_fetch_kline()

        # ── 步骤 1：从 DataFeed 取各周期历史切片 ──────────────────────
        multi_tf_data = {}
        for tf in self.timeframes:
            limit = self._kline_limits.get(tf, self._default_limit)
            hist = data_feed.get_history(symbol, tf, end_ts_ms=ts_ms, limit=limit)
            if hist.empty:
                logger.debug(f"  {symbol} {tf} 历史数据为空，跳过信号生成")
                return None
            # 转换列名以匹配生产代码期望的 DataFrame 格式（timestamp 为索引）
            df = hist.copy()
            df["timestamp"] = hist["timestamp"]
            # 生产 indicator_engine 使用数字列而非 DatetimeIndex，直接传入即可
            multi_tf_data[tf] = df

        # ── 步骤 2：计算指标 & 生成市场快照 ───────────────────────────
        try:
            snapshot, tf_indicators = ie.generate_market_snapshot(
                multi_tf_data, symbol
            )
        except Exception as e:
            logger.warning(f"  {symbol} 指标计算失败：{e}")
            return None

        # ── 步骤 3：规则引擎预过滤 ────────────────────────────────────
        try:
            passed, direction, filter_reason = ie.rule_engine_filter(tf_indicators, symbol)
        except Exception as e:
            logger.warning(f"  {symbol} 规则引擎异常：{e}")
            return None

        if not passed:
            logger.debug(f"  {symbol} 规则引擎拒绝：{filter_reason}")
            return None

        logger.debug(f"  {symbol} 规则引擎通过：{direction}")

        # ── 步骤 4：计算支撑阻力位 ────────────────────────────────────
        anchor_tf = self.timeframes[0]
        anchor_df = multi_tf_data.get(anchor_tf)
        support_levels, resistance_levels = [], []
        if anchor_df is not None and not anchor_df.empty:
            try:
                support_levels, resistance_levels = fk.calculate_support_resistance(anchor_df)
            except Exception as e:
                logger.debug(f"  {symbol} 支撑阻力计算失败（忽略）：{e}")

        # ── 步骤 5：AI Mock 构造决策 ────────────────────────────────────
        base_tf = self.timeframes[-1]
        base_df = multi_tf_data.get(base_tf)
        current_price = float(base_df.iloc[-1]["close"]) if base_df is not None and not base_df.empty else 0.0

        # 整理 tf_indicators 中的关键字段供 ai_mock 使用
        simplified_indicators = self._simplify_indicators(tf_indicators)

        try:
            decision = self.ai_mock.analyze(
                tf_indicators=simplified_indicators,
                support_levels=support_levels,
                resistance_levels=resistance_levels,
                current_price=current_price,
            )
        except Exception as e:
            logger.warning(f"  {symbol} AI Mock 异常：{e}")
            return None

        if decision.get("signal") not in ("long", "short"):
            logger.debug(f"  {symbol} AI Mock 返回 wait")
            return None

        # 注入规则引擎方向，作为最终方向验证
        if decision["signal"] != direction:
            logger.debug(
                f"  {symbol} AI方向({decision['signal']}) 与规则引擎方向({direction}) 不一致，跳过"
            )
            return None

        # ── 步骤 6：信号质量检查（复用生产 risk_filter）──────────────
        try:
            quality_ok, quality_reason = rf.check_signal_quality(decision)
        except Exception as e:
            logger.warning(f"  {symbol} 信号质量检查异常：{e}")
            return None

        if not quality_ok:
            logger.debug(f"  {symbol} 信号质量不足：{quality_reason}")
            return None

        # ── 步骤 7：计算仓位大小 ──────────────────────────────────────
        balance = self._get_available_balance()
        try:
            pos_info = rf.calculate_position_size(
                balance_usdt=balance,
                entry_price=decision.get("entry_price", current_price),
                stop_loss=decision.get("stop_loss", 0),
                leverage=self.config.get("backtest", {}).get("leverage", 10),
            )
        except Exception as e:
            logger.warning(f"  {symbol} 仓位计算异常：{e}")
            return None

        if not pos_info or pos_info.get("contracts", 0) <= 0:
            logger.debug(f"  {symbol} 仓位计算为0，跳过")
            return None

        # ── 组装最终信号 ──────────────────────────────────────────────
        signal = {
            "symbol":        symbol,
            "side":          decision["signal"],          # 'long' | 'short'
            "entry_price":   decision.get("entry_price", current_price),
            "stop_loss":     decision.get("stop_loss"),
            "take_profit":   decision.get("take_profit"),
            "contracts":     pos_info.get("contracts", 0),
            "margin_usdt":   pos_info.get("margin_usdt", 0),
            "signal_strength": decision.get("signal_strength", 0),
            "confidence":    decision.get("confidence", "low"),
            "risk_reward":   decision.get("risk_reward", "1:0"),
            "key_support":   decision.get("key_support"),
            "key_resistance":decision.get("key_resistance"),
            "reason":        decision.get("reason", ""),
            "ts_ms":         ts_ms,
        }

        logger.info(
            f"✅ 信号生成 {symbol} {signal['side'].upper()} "
            f"entry={signal['entry_price']:.4f} "
            f"sl={signal['stop_loss']:.4f} "
            f"tp={signal['take_profit']:.4f} "
            f"strength={signal['signal_strength']} "
            f"rr={signal['risk_reward']}"
        )
        return signal

    # ─────────────────────────────────────────────────────────────────
    # 内部工具
    # ─────────────────────────────────────────────────────────────────

    def _simplify_indicators(self, tf_indicators: dict) -> dict:
        """
        将生产 indicator_engine 输出的完整指标字典简化，
        提取 ai_mock 需要的关键字段。
        """
        simplified = {}
        for tf, ind in tf_indicators.items():
            if not ind.get("valid"):
                continue
            adx_info = ind.get("adx", {})
            ema_info = ind.get("ema", {})
            patterns = ind.get("patterns", [])

            # 取最新K线形态（方向与趋势一致的优先）
            pattern_name = "none"
            if patterns:
                pattern_name = patterns[0].get("pattern", "none")

            simplified[tf] = {
                "trend":        ind.get("trend", "sideways"),
                "adx":          adx_info.get("adx", 0),
                "plus_di":      adx_info.get("plus_di", 0),
                "minus_di":     adx_info.get("minus_di", 0),
                "ema_align":    self._ema_align_short(ema_info.get("alignment", "mixed")),
                "rsi":          ind.get("rsi", 50),
                "volume_ratio": ind.get("volume_ratio", 1.0),
                "pattern":      pattern_name,
            }
        return simplified

    @staticmethod
    def _ema_align_short(alignment: str) -> str:
        """将生产 'bullish'/'bearish'/'mixed' 映射为简短的 'bull'/'bear'/'mixed'"""
        return {"bullish": "bull", "bearish": "bear", "mixed": "mixed"}.get(alignment, "mixed")

    def _get_available_balance(self) -> float:
        """供 calculate_position_size 使用的余额（由引擎实时注入）"""
        return self.available_balance

    # ── 懒加载生产模块 ────────────────────────────────────────────────

    def _get_indicator_engine(self):
        if self._indicator_engine is None:
            import indicator_engine as ie
            self._indicator_engine = ie
            logger.debug("indicator_engine 模块已加载")
        return self._indicator_engine

    def _get_risk_filter(self):
        if self._risk_filter is None:
            # risk_filter 模块顶层会调用 check_env()，回测时跳过
            import importlib, types
            # 临时 mock check_env 避免因缺少 .env 中断回测
            import config_loader as cl
            _orig = cl.check_env
            cl.check_env = lambda: None
            import risk_filter as rf
            cl.check_env = _orig
            self._risk_filter = rf
            logger.debug("risk_filter 模块已加载")
        return self._risk_filter

    def _get_fetch_kline(self):
        if self._fetch_kline is None:
            import fetch_kline as fk
            self._fetch_kline = fk
            logger.debug("fetch_kline 模块已加载")
        return self._fetch_kline
