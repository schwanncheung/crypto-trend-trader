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
    直接复用生产 ai_analysis._build_rule_only_decision()，保持逻辑一致。
    """

    _use_raw_indicators = True  # 告知 pipeline 传入原始 tf_indicators

    def __init__(self, config: dict):
        self.timeframes = config.get("timeframes", ["1h", "15m", "5m"])

    def analyze(
        self,
        tf_indicators: dict,
        current_price: float,
    ) -> dict:
        import sys
        from pathlib import Path
        _scripts = Path(__file__).parent.parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))

        # 方向判断（规则引擎已过滤，此处做二次校验）
        up_count   = sum(1 for v in tf_indicators.values() if v.get("trend") == "up")
        down_count = sum(1 for v in tf_indicators.values() if v.get("trend") == "down")
        total_tfs  = len(self.timeframes)
        if up_count > down_count and up_count >= max(1, total_tfs - 1):
            direction = "long"
        elif down_count > up_count and down_count >= max(1, total_tfs - 1):
            direction = "short"
        else:
            return self._wait_decision("多周期趋势不共振")

        anchor_tf = self.timeframes[0]
        expected = "up" if direction == "long" else "down"
        if tf_indicators.get(anchor_tf, {}).get("trend") != expected:
            return self._wait_decision(f"锚周期{anchor_tf}趋势不符")

        from ai_analysis import _build_rule_only_decision
        return _build_rule_only_decision(tf_indicators, direction, symbol="")

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
