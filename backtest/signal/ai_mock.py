#!/usr/bin/env python3
"""
backtest/signal/ai_mock.py
AI 分析层替代实现

两种模式：
  RuleOnlyMock  : 基于指标量化分数构造伪AI决策（无API调用，可重复执行）
  LLMMockCache  : 读取预缓存的LLM响应JSON（用于验证真实AI效果）
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class RuleOnlyMock:
    """
    纯规则构造伪AI决策。
    从 indicator_engine 输出的 tf_indicators 中提取量化指标，
    按固定规则计算信号强度并构造与生产 risk_filter 兼容的 decision dict。
    """

    def __init__(self, config: dict):
        trading = config.get("trading", {})
        self.min_signal_strength = trading.get("min_signal_strength", 7)
        self.min_rr_ratio        = trading.get("min_rr_ratio", 2.0)
        self.timeframes          = config.get("timeframes", ["1h", "30m", "15m"])

    def analyze(
        self,
        tf_indicators: dict,
        support_levels: list,
        resistance_levels: list,
        current_price: float,
    ) -> dict:
        """
        参数：
            tf_indicators   : {tf: {trend, adx, rsi, ema_align, volume_ratio, pattern, ...}}
            support_levels  : 关键支撑位列表（降序）
            resistance_levels: 关键阻力位列表（升序）
            current_price   : 当前价格（最低周期最新收盘价）

        返回：与 risk_filter.check_signal_quality() 兼容的 decision dict
        """
        # ── 1. 方向判断：多周期趋势共振 ────────────────────────────────
        up_count   = sum(1 for v in tf_indicators.values() if v.get("trend") == "up")
        down_count = sum(1 for v in tf_indicators.values() if v.get("trend") == "down")
        total_tfs  = len(self.timeframes)

        if up_count > down_count and up_count >= max(1, total_tfs - 1):
            direction = "long"
        elif down_count > up_count and down_count >= max(1, total_tfs - 1):
            direction = "short"
        else:
            return self._wait_decision("多周期趋势不共振")

        # ── 2. 锚周期（最高周期）趋势校验 ──────────────────────────────
        anchor_tf = self.timeframes[0]
        anchor_trend = tf_indicators.get(anchor_tf, {}).get("trend", "sideways")
        expected = "up" if direction == "long" else "down"
        if anchor_trend != expected:
            return self._wait_decision(f"锚周期{anchor_tf}趋势不符：{anchor_trend}")

        # ── 3. 信号强度评分（0-10）─────────────────────────────────────
        score = 0.0

        # ADX 评分（最高周期）
        adx = tf_indicators.get(anchor_tf, {}).get("adx", 0)
        if adx >= 30:   score += 2.5
        elif adx >= 20: score += 1.5
        elif adx >= 15: score += 0.5

        # EMA 排列评分
        ema_align_ok = sum(
            1 for v in tf_indicators.values()
            if v.get("ema_align") == ("bull" if direction == "long" else "bear")
        )
        score += ema_align_ok / total_tfs * 2.5

        # 成交量评分（最低周期）
        base_tf = self.timeframes[-1]
        vol_ratio = tf_indicators.get(base_tf, {}).get("volume_ratio", 0)
        volume_confirmed = vol_ratio >= 1.2
        if vol_ratio >= 1.5:  score += 2.0
        elif vol_ratio >= 1.2: score += 1.0

        # K线形态评分（最低周期）
        pattern = tf_indicators.get(base_tf, {}).get("pattern", "none")
        if pattern not in ("none", "", None):
            score += 1.5

        # RSI 评分（避免极值区域）
        rsi = tf_indicators.get(base_tf, {}).get("rsi", 50)
        if direction == "long" and 40 <= rsi <= 65:   score += 1.5
        elif direction == "short" and 35 <= rsi <= 60: score += 1.5

        signal_strength = min(10, int(score))

        # ── 4. 计算入场/止损/止盈 ──────────────────────────────────────
        entry = current_price
        stop_loss, take_profit, rr = self._calc_sl_tp(
            direction, entry, support_levels, resistance_levels
        )

        if rr < self.min_rr_ratio:
            return self._wait_decision(f"RR不足：{rr:.2f} < {self.min_rr_ratio}")

        # ── 5. 置信度 ──────────────────────────────────────────────────
        confidence = "high" if (signal_strength >= self.min_signal_strength and volume_confirmed) else "low"

        # ── 6. 趋势强度（用最高周期ADX映射到1-10）─────────────────────
        trend_strength = min(10, int(adx / 4))

        return {
            "signal":           direction,
            "signal_type":      pattern or "pullback",
            "signal_strength":  signal_strength,
            "trend":            expected,
            "trend_phase":      "mid",
            "trend_strength":   trend_strength,
            "volume_confirmed": volume_confirmed,
            "volume_note":      f"量比={vol_ratio:.2f}",
            "key_support":      support_levels[0] if support_levels else entry * 0.97,
            "key_resistance":   resistance_levels[0] if resistance_levels else entry * 1.03,
            "entry_price":      entry,
            "stop_loss":        stop_loss,
            "take_profit":      take_profit,
            "risk_reward":      f"1:{rr:.1f}",
            "divergence_risk":  False,
            "structure_broken": False,
            "confidence":       confidence,
            "reason":           (
                f"规则引擎信号：{direction}，ADX={adx:.1f}，"
                f"EMA对齐={ema_align_ok}/{total_tfs}，"
                f"量比={vol_ratio:.2f}，形态={pattern}，RSI={rsi:.1f}"
            ),
            "warning":          None,
        }

    def _calc_sl_tp(
        self,
        direction: str,
        entry: float,
        support_levels: list,
        resistance_levels: list,
    ) -> tuple[float, float, float]:
        """基于支撑阻力位计算止损和止盈，返回 (stop_loss, take_profit, rr)"""
        if direction == "long":
            # 止损：最近支撑位下方1%
            sl_base = support_levels[0] if support_levels else entry * 0.97
            stop_loss = sl_base * 0.99
            # 止盈：最近阻力位
            tp_base = resistance_levels[0] if resistance_levels else entry * 1.06
            take_profit = tp_base * 0.995
        else:
            # 止损：最近阻力位上方1%
            sl_base = resistance_levels[0] if resistance_levels else entry * 1.03
            stop_loss = sl_base * 1.01
            # 止盈：最近支撑位
            tp_base = support_levels[0] if support_levels else entry * 0.94
            take_profit = tp_base * 1.005

        risk   = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        rr = reward / risk if risk > 0 else 0.0
        return stop_loss, take_profit, round(rr, 2)

    @staticmethod
    def _wait_decision(reason: str) -> dict:
        return {
            "signal":           "wait",
            "signal_strength":  0,
            "confidence":       "low",
            "reason":           reason,
            "volume_confirmed": False,
            "divergence_risk":  True,
            "structure_broken": True,
            "risk_reward":      "1:0",
            "trend_strength":   0,
        }


class LLMMockCache:
    """
    从预缓存 JSON 文件读取历史 LLM 决策。
    缓存 key：{symbol_safe}_{timeframe}_{timestamp_str}
    未命中时降级为 RuleOnlyMock。
    """

    def __init__(self, config: dict, cache_dir: str | Path):
        self.cache_dir   = Path(cache_dir)
        self._rule_mock  = RuleOnlyMock(config)
        self._cache: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        count = 0
        for jf in self.cache_dir.glob("**/*.json"):
            try:
                with open(jf, encoding="utf-8") as f:
                    data = json.load(f)
                # 文件名即为 key
                self._cache[jf.stem] = data
                count += 1
            except Exception as e:
                logger.warning(f"LLMMockCache 加载失败：{jf}：{e}")
        logger.info(f"LLMMockCache 加载 {count} 条缓存决策")

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        ts_ms: int,
        tf_indicators: dict,
        support_levels: list,
        resistance_levels: list,
        current_price: float,
    ) -> dict:
        from datetime import datetime, timezone
        ts_str = datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y%m%d_%H%M")
        symbol_safe = symbol.replace("/", "_").replace(":", "_")
        key = f"{symbol_safe}_{timeframe}_{ts_str}"

        if key in self._cache:
            logger.debug(f"LLMMockCache 命中：{key}")
            return self._cache[key]

        logger.debug(f"LLMMockCache 未命中，降级为RuleOnly：{key}")
        return self._rule_mock.analyze(tf_indicators, support_levels, resistance_levels, current_price)
