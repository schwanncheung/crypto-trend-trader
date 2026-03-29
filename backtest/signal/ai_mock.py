#!/usr/bin/env python3
from __future__ import annotations
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
        rule_engine = config.get("analysis", {}).get("rule_engine", {})
        rule_filter = config.get("analysis", {}).get("rule_filter", {})
        self.min_signal_strength  = trading.get("min_signal_strength", 7)
        self.min_rr_ratio         = trading.get("min_rr_ratio", 2.0)
        self.atr_multiplier       = trading.get("stop_loss_atr_multiplier", 2.5)
        self.timeframes           = config.get("timeframes", ["1h", "30m", "15m"])
        self.vol_ratio_threshold  = rule_filter.get("volume_ratio_threshold", 0.8)
        self.strong_trend_adx     = rule_filter.get("strong_trend_adx_threshold", 60)
        self.strong_trend_di_diff = rule_filter.get("strong_trend_di_diff_threshold", 20)

    def analyze(
        self,
        tf_indicators: dict,
        current_price: float,
    ) -> dict:
        """
        参数：
            tf_indicators   : {tf: {trend, adx, rsi, ema_align, volume_ratio, pattern, atr, ...}}
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

        # ── 方案D：检查极强趋势豁免条件（提前计算，供后续评分使用）──
        anchor_tf = self.timeframes[0]
        anchor_ind = tf_indicators.get(anchor_tf, {})
        _adx = anchor_ind.get("adx", 0)
        _plus_di = anchor_ind.get("plus_di", 0)
        _minus_di = anchor_ind.get("minus_di", 0)
        _di_diff = abs(_plus_di - _minus_di)
        strong_trend_exemption = (_adx >= self.strong_trend_adx and _di_diff >= self.strong_trend_di_diff)

        # 成交量评分（最低周期）
        base_tf = self.timeframes[-1]
        vol_ratio = tf_indicators.get(base_tf, {}).get("volume_ratio", 0)
        volume_confirmed = vol_ratio >= self.vol_ratio_threshold
        if vol_ratio >= self.vol_ratio_threshold * 2:  score += 2.0
        elif vol_ratio >= self.vol_ratio_threshold:    score += 1.0
        # 方案D豁免：极强趋势中 volume_confirmed 强制置 True
        if strong_trend_exemption:
            volume_confirmed = True

        # K线形态评分（最低周期）
        pattern = tf_indicators.get(base_tf, {}).get("pattern", "none")
        if pattern not in ("none", "", None):
            score += 1.5

        # RSI 评分（方案B：放宽区间，允许深度超卖/超买趋势中仍得分）
        rsi = tf_indicators.get(base_tf, {}).get("rsi", 50)
        if direction == "long" and 25 <= rsi <= 65:   score += 1.5
        elif direction == "short" and 25 <= rsi <= 65: score += 1.5
        # 方案D豁免：极强趋势中深度超卖/超买也得分（RSI<25做空, RSI>75做多）
        elif strong_trend_exemption and direction == "short" and rsi < 25: score += 1.5
        elif strong_trend_exemption and direction == "long" and rsi > 75:  score += 1.5

        signal_strength = min(10, int(score))

        # ── 4. 计算入场/止损/止盈（与生产一致：ATR动态止损 + 固定盈亏比止盈）──
        entry = current_price
        # 取最低周期 ATR
        atr = tf_indicators.get(base_tf, {}).get("atr", entry * 0.01)
        stop_loss, take_profit, rr = self._calc_sl_tp(direction, entry, atr)

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
            "key_support":      entry * 0.97,
            "key_resistance":   entry * 1.03,
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
        atr: float,
    ) -> tuple[float, float, float]:
        """与生产一致：ATR动态止损 + 固定盈亏比止盈
        long : stop_loss = entry - atr_multiplier×ATR
               take_profit = entry + min_rr_ratio×(entry - stop_loss)
        short: stop_loss = entry + atr_multiplier×ATR
               take_profit = entry - min_rr_ratio×(stop_loss - entry)
        """
        if direction == "long":
            stop_loss   = entry - self.atr_multiplier * atr
            take_profit = entry + self.min_rr_ratio * (entry - stop_loss)
        else:
            stop_loss   = entry + self.atr_multiplier * atr
            take_profit = entry - self.min_rr_ratio * (stop_loss - entry)

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
        return self._rule_mock.analyze(tf_indicators, current_price)


class LLMRealAnalyzer:
    """
    调用生产环境的真实文本 LLM 分析逻辑。
    直接复用 scripts/ai_analysis.py 的 analyze_with_text_llm() 函数。
    """

    def __init__(self, config: dict):
        self.config = config
        logger.info("LLMRealAnalyzer 初始化：将调用生产环境文本 LLM API")

    def analyze(
        self,
        tf_indicators: dict,
        current_price: float,
    ) -> dict:
        """
        参数：
            tf_indicators   : {tf: {trend, adx, rsi, ema_align, volume_ratio, pattern, atr, ...}}
            current_price   : 当前价格（最低周期最新收盘价）

        返回：与 risk_filter.check_signal_quality() 兼容的 decision dict
        """
        import sys
        from pathlib import Path

        # 确保 scripts/ 在 sys.path 中
        _PROJECT_ROOT = Path(__file__).parent.parent.parent
        _SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
        if str(_SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS_DIR))

        # 解决命名冲突：临时移除 backtest.signal 包，避免标准库 signal 被遮蔽
        _saved_modules = {}
        for key in list(sys.modules.keys()):
            if key == 'signal' or key.startswith('signal.'):
                _saved_modules[key] = sys.modules.pop(key)

        try:
            # 导入生产环境的 indicator_engine 和 ai_analysis
            import indicator_engine as ie
            import ai_analysis as ai

            # 构造市场快照字符串（模拟 generate_market_snapshot 的输出格式）
            snapshot = self._build_market_snapshot(tf_indicators, current_price)

            # 调用生产环境的文本 LLM 分析
            logger.info("[LLMRealAnalyzer] 调用生产环境文本 LLM 分析...")
            result = ai.analyze_with_text_llm(snapshot)

            logger.info(
                f"[LLMRealAnalyzer] LLM 返回：signal={result.get('signal')}, "
                f"confidence={result.get('confidence')}, "
                f"strength={result.get('signal_strength')}"
            )
            return result

        except Exception as e:
            logger.error(f"[LLMRealAnalyzer] 调用失败：{e}")
            return {
                "signal":           "wait",
                "signal_strength":  0,
                "confidence":       "low",
                "reason":           f"LLM调用失败：{e}",
                "volume_confirmed": False,
                "divergence_risk":  True,
                "structure_broken": True,
                "risk_reward":      "1:0",
                "trend_strength":   0,
            }
        finally:
            # 恢复 backtest.signal 包
            sys.modules.update(_saved_modules)

    def _build_market_snapshot(self, tf_indicators: dict, current_price: float) -> str:
        """
        构造市场快照字符串，格式与 indicator_engine.generate_market_snapshot() 输出一致。
        """
        lines = [f"当前价格：{current_price:.6f}\n"]

        for tf, ind in tf_indicators.items():
            trend = ind.get("trend", "sideways")
            adx = ind.get("adx", 0)
            plus_di = ind.get("plus_di", 0)
            minus_di = ind.get("minus_di", 0)
            rsi = ind.get("rsi", 50)
            ema_align = ind.get("ema_align", "mixed")
            vol_ratio = ind.get("volume_ratio", 1.0)
            pattern = ind.get("pattern", "none")
            atr = ind.get("atr", 0)

            lines.append(f"【{tf}】")
            lines.append(f"  趋势：{trend}")
            lines.append(f"  ADX：{adx:.1f}  +DI：{plus_di:.1f}  -DI：{minus_di:.1f}")
            lines.append(f"  RSI：{rsi:.1f}")
            lines.append(f"  EMA排列：{ema_align}")
            lines.append(f"  量比：{vol_ratio:.2f}")
            lines.append(f"  K线形态：{pattern}")
            lines.append(f"  ATR：{atr:.6f}")
            lines.append("")

        return "\n".join(lines)
