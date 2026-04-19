#!/usr/bin/env python3
"""
indicator_engine.py
规则引擎：技术指标计算 + 裸K形态识别 + 单边趋势过滤 + 市场快照生成

⚠️ 修改核心算法后，请同步更新 CLAUDE.md「核心算法」章节

输出结构化文本快照供 LLM 文本分析使用，也作为规则预过滤门卫。

v2 新增：
  - RSI序列计算（compute_rsi_series）：返回最近N根RSI值，用于趋势delta分析
  - 趋势转折预警（detect_rsi_reversal_warning）：15m/30m连续两轮RSI回升+量能放大→触发做空暂停
  - 超卖反弹保护（detect_oversold_bounce_guard）：RSI从超卖区反弹超过阈值→禁止做空N轮
  - 多头信号规则引擎（detect_long_signals）：补充做多条件，实现双向互斥保护
  - LLM快照增强：注入RSI delta趋势（当前值 vs 前3轮）
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional, List, Tuple

from config_loader import ANALYSIS_CFG, TIMEFRAMES, TRADING_CFG
from trading_hours import get_session_label_from_ts

logger = logging.getLogger(__name__)

# ── 读取配置 ──────────────────────────────────────────────────────────
_IND_CFG  = ANALYSIS_CFG.get("indicator", {})
_RULE_CFG = ANALYSIS_CFG.get("rule_filter", {})
_PATTERN_FILTER_CFG = _RULE_CFG.get("pattern_filter", {})  # P0优化：inside_bar开关

EMA_PERIODS       = _IND_CFG.get("ema_periods", [21, 55, 200])
ADX_PERIOD        = _IND_CFG.get("adx_period", 14)
RSI_PERIOD        = _IND_CFG.get("rsi_period", 14)
VOL_MA_PERIOD     = _IND_CFG.get("volume_ma_period", 5)
SWING_LOOKBACK    = _IND_CFG.get("swing_lookback", 5)
SWING_COUNT       = _IND_CFG.get("swing_count", 3)

# 方向锚周期（用于规则引擎宏观方向判断）
ANCHOR_TF         = _RULE_CFG.get("anchor_timeframe", "4h")
REQUIRE_ANCHOR    = _RULE_CFG.get("require_anchor_aligned", True)
MIN_TRENDING_TF   = _RULE_CFG.get("min_trending_timeframes", 2)
ADX_THRESHOLD     = _RULE_CFG.get("adx_trending_threshold", 20)
VOL_RATIO_THRESH  = _RULE_CFG.get("volume_ratio_threshold", 1.2)
RSI_OVERBOUGHT    = _RULE_CFG.get("rsi_overbought", 75)
RSI_OVERSOLD      = _RULE_CFG.get("rsi_oversold", 25)

# R21新增：做空保护参数
_SHORT_MIN_ADX    = TRADING_CFG.get("short_min_adx", 40)
_RSI_SHORT_GUARD  = _RULE_CFG.get("rsi_short_guard_zone", 40)
_BULLISH_PATTERNS = set(_PATTERN_FILTER_CFG.get("bullish_patterns", {}).get("patterns", []))


def reload_config_from_dict(config: dict) -> None:
    """
    从外部配置字典重新加载参数（回测系统 override 机制）。
    在回测 pipeline 导入 indicator_engine 后调用此函数。
    """
    global REQUIRE_ANCHOR, MIN_TRENDING_TF, ADX_THRESHOLD, VOL_RATIO_THRESH, \
           RSI_OVERBOUGHT, RSI_OVERSOLD, ANCHOR_TF, \
           STRONG_TREND_ADX_THRESHOLD, STRONG_TREND_DI_DIFF_THRESHOLD, \
           _PATTERN_FILTER_CFG, _SHORT_MIN_ADX, _RSI_SHORT_GUARD, _BULLISH_PATTERNS

    rule_cfg = config.get("analysis", {}).get("rule_filter", {})
    ind_cfg = config.get("analysis", {}).get("indicator", {})
    trading_cfg = config.get("trading", {})

    # 更新全局变量
    ANCHOR_TF = rule_cfg.get("anchor_timeframe", ANCHOR_TF)
    REQUIRE_ANCHOR = rule_cfg.get("require_anchor_aligned", REQUIRE_ANCHOR)
    MIN_TRENDING_TF = rule_cfg.get("min_trending_timeframes", MIN_TRENDING_TF)
    ADX_THRESHOLD = rule_cfg.get("adx_trending_threshold", ADX_THRESHOLD)
    VOL_RATIO_THRESH = rule_cfg.get("volume_ratio_threshold", VOL_RATIO_THRESH)
    RSI_OVERBOUGHT = rule_cfg.get("rsi_overbought", RSI_OVERBOUGHT)
    RSI_OVERSOLD = rule_cfg.get("rsi_oversold", RSI_OVERSOLD)
    STRONG_TREND_ADX_THRESHOLD = rule_cfg.get("strong_trend_adx_threshold", STRONG_TREND_ADX_THRESHOLD)
    STRONG_TREND_DI_DIFF_THRESHOLD = rule_cfg.get("strong_trend_di_diff_threshold", STRONG_TREND_DI_DIFF_THRESHOLD)

    _PATTERN_FILTER_CFG = rule_cfg.get("pattern_filter", {})  # P0：inside_bar开关

    # R21新增：做空保护参数
    _SHORT_MIN_ADX = trading_cfg.get("short_min_adx", _SHORT_MIN_ADX)
    _RSI_SHORT_GUARD = rule_cfg.get("rsi_short_guard_zone", _RSI_SHORT_GUARD)
    _BULLISH_PATTERNS = set(_PATTERN_FILTER_CFG.get("bullish_patterns", {}).get("patterns", []))

    logger.info(
        f"[indicator_engine] 配置已重新加载：ADX_THRESHOLD={ADX_THRESHOLD}, "
        f"REQUIRE_ANCHOR={REQUIRE_ANCHOR}, MIN_TRENDING_TF={MIN_TRENDING_TF}, "
        f"inside_bar_enabled={_PATTERN_FILTER_CFG.get('inside_bar_enabled', True)}"
    )

# ── 新增：趋势转折预警 & 超卖反弹保护参数 ───────────────────────────
# 优化1：趋势转折预警 —— 小周期连续N轮RSI回升+量能放大触发做空暂停
RSI_REVERSAL_WARNING_TFS    = _RULE_CFG.get("rsi_reversal_warning_timeframes", ["15m", "30m"])
RSI_REVERSAL_CONSEC_ROUNDS  = _RULE_CFG.get("rsi_reversal_consecutive_rounds", 2)   # 连续回升轮数
RSI_REVERSAL_VOL_CONFIRM    = _RULE_CFG.get("rsi_reversal_vol_confirm", True)        # 是否要求量能确认

# 优化2：超卖反弹保护 —— RSI从超卖区反弹超过阈值→禁止做空
RSI_BOUNCE_GUARD_DELTA      = _RULE_CFG.get("rsi_bounce_guard_delta", 6)             # 反弹幅度阈值（点）
RSI_BOUNCE_GUARD_OVERSOLD   = _RULE_CFG.get("rsi_bounce_guard_oversold", 30)         # 超卖判定线（偏宽松）

# 方案D：趋势强度豁免 —— 极强趋势中跳过RSI超卖/超买保护
STRONG_TREND_ADX_THRESHOLD  = _RULE_CFG.get("strong_trend_adx_threshold", 60)
STRONG_TREND_DI_DIFF_THRESHOLD = _RULE_CFG.get("strong_trend_di_diff_threshold", 20)

# 优化4：多头信号规则引擎触发条件
LONG_SIGNAL_RSI_LOW         = _RULE_CFG.get("long_signal_rsi_low", 40)               # RSI低位阈值（回调买点区间上沿）
LONG_SIGNAL_VOL_RATIO       = _RULE_CFG.get("long_signal_vol_ratio", 1.0)            # 做多量比最低要求

# 方案E：RSI底背离/顶背离保护 —— 价格创新低但RSI未创新低（底背离）→ 禁止做空
RSI_DIVERGENCE_ENABLED      = _RULE_CFG.get("rsi_divergence_enabled", True)
RSI_DIVERGENCE_LOOKBACK     = _RULE_CFG.get("rsi_divergence_lookback", 10)   # 用于背离检测的RSI历史窗口（根）
RSI_DIVERGENCE_MIN_DROP_PCT = _RULE_CFG.get("rsi_divergence_min_drop_pct", 0.005)  # 价格新低幅度最低要求（0.5%）
RSI_DIVERGENCE_MIN_RSI_DIFF = _RULE_CFG.get("rsi_divergence_min_rsi_diff", 3.0)    # RSI未跟随价格创新低的最小差值
# RSI超卖持续保护 —— RSI在超卖区停留N根以上 → 动能衰竭 → 禁止做空（即使极强趋势豁免）
RSI_OVERSOLD_PERSIST_BARS   = _RULE_CFG.get("rsi_oversold_persist_bars", 6)   # 超卖持续根数门槛
RSI_OVERSOLD_PERSIST_LEVEL  = _RULE_CFG.get("rsi_oversold_persist_level", 25) # 超卖判定线（锚周期）

# ── 近期趋势连续性判断参数 ────────────────────────────────────────────
# 解决EMA200惯性滞后问题：在200根K线中，单独评估最近N根的方向连续性
# 让系统能在"趋势刚启动"时就感知到方向，而不是等EMA排列完全形成（需55+小时）
RECENT_TREND_WINDOW     = _RULE_CFG.get("recent_trend_window", 24)       # 近期窗口（根）：取最近24根K线（1h周期=近24小时）
RECENT_TREND_MIN_PCT    = _RULE_CFG.get("recent_trend_min_pct", 0.65)    # 方向一致K线占比下限（65%=24根里至少16根同向）
RECENT_TREND_MIN_MOVE   = _RULE_CFG.get("recent_trend_min_move", 0.015)  # 窗口内总振幅下限（1.5%，过滤横盘假信号）
CONTEXT_WINDOW          = _RULE_CFG.get("context_trend_window", 60)      # 背景窗口（根）：用于判断近期涨跌是否超越了前期横盘

# 快捷引用：第一个EMA周期（最短，用于价格位置判断）
_EMA_FAST = EMA_PERIODS[0] if EMA_PERIODS else 21


# ═══════════════════════════════════════════════════════════════════════
# 一、指标计算
# ═══════════════════════════════════════════════════════════════════════

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = RSI_PERIOD) -> float:
    """返回最新一根 RSI 值"""
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def compute_rsi_series(series: pd.Series, period: int = RSI_PERIOD, lookback: int = 4) -> List[float]:
    """
    返回最近 lookback 根 K 线的 RSI 值列表（从旧到新）。
    用于计算 RSI delta 趋势，供趋势转折预警和 LLM 快照使用。
    数据不足时补充 50.0。
    """
    if len(series) < period + 1:
        return [50.0] * lookback
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi_full = 100 - (100 / (1 + rs))
    # 取最后 lookback 根，不足则用 50.0 填充
    tail = rsi_full.dropna().tail(lookback).tolist()
    if len(tail) < lookback:
        tail = [50.0] * (lookback - len(tail)) + tail
    return [round(v, 2) for v in tail]


def _rsi_delta_description(rsi_series: List[float]) -> str:
    """
    将 RSI 序列（从旧→新，长度4）转换为自然语言趋势描述。
    用于注入 LLM 快照，让模型感知 RSI 动态变化方向。
    示例：[28.5, 31.2, 35.8, 42.1] → "RSI连续回升（+13.6pts，3轮）超卖修复中"
    """
    if len(rsi_series) < 2:
        return "RSI数据不足"
    current  = rsi_series[-1]
    prev3    = rsi_series[:-1]
    delta    = current - rsi_series[0]          # 全程变化量
    # 判断连续上升/下降轮数
    rising_rounds = sum(
        1 for i in range(1, len(rsi_series))
        if rsi_series[i] > rsi_series[i - 1]
    )
    falling_rounds = sum(
        1 for i in range(1, len(rsi_series))
        if rsi_series[i] < rsi_series[i - 1]
    )
    total_rounds = len(rsi_series) - 1

    # 趋势描述
    if rising_rounds == total_rounds:
        direction = f"连续回升（+{delta:+.1f}pts，{rising_rounds}轮）"
    elif falling_rounds == total_rounds:
        direction = f"连续下行（{delta:+.1f}pts，{falling_rounds}轮）"
    elif rising_rounds > falling_rounds:
        direction = f"整体回升（+{delta:+.1f}pts，震荡向上）"
    elif falling_rounds > rising_rounds:
        direction = f"整体下行（{delta:+.1f}pts，震荡向下）"
    else:
        direction = f"横盘震荡（{delta:+.1f}pts）"

    # 附加区间标签
    if current <= RSI_OVERSOLD:
        zone = "深度超卖区"
    elif current <= RSI_BOUNCE_GUARD_OVERSOLD:
        zone = "超卖区"
    elif current >= RSI_OVERBOUGHT:
        zone = "超买区"
    elif current >= 60:
        zone = "偏强区"
    elif current <= 40:
        zone = "偏弱区"
    else:
        zone = "中性区"

    return f"RSI{direction}，当前{current}（{zone}）"


# ── 方案E：RSI底背离/顶背离检测 + 超卖持续保护 ──────────────────────────
def detect_rsi_divergence(
    tf_indicators: dict,
    signal_direction: str,
    symbol: str = "",
) -> Tuple[bool, str]:
    """
    检测两种情形（即使极强趋势豁免也执行）：

    1. RSI底背离（做空保护）：
       价格创新低，但 RSI 未跟随创新低 → 动能衰竭，禁止做空。

    2. RSI超卖持续保护（做空保护）：
       锚周期 RSI 已在超卖区（<=rsi_oversold_persist_level）
       连续停留 >= rsi_oversold_persist_bars 根 K 线 → 动能耗尽，禁止做空。

    对应做多方向：顶背离 + 超买持续保护。

    返回：(detected: bool, reason: str)
    """
    if not RSI_DIVERGENCE_ENABLED:
        return False, ""

    anchor_ind = tf_indicators.get(ANCHOR_TF, {})
    if not anchor_ind.get("valid"):
        return False, ""

    rsi_series_long = anchor_ind.get("rsi_series_long", [])
    price_series    = anchor_ind.get("price_series", [])

    if len(rsi_series_long) < 4 or len(price_series) < 4:
        return False, ""

    current_price = price_series[-1]
    current_rsi   = rsi_series_long[-1]
    hist_prices   = price_series[:-1]
    hist_rsi      = rsi_series_long[:-1]

    if signal_direction == "short":
        # ── 检测1：RSI超卖连续保护（从最新往前数连续超卖根数）──────────
        consec_oversold = 0
        for r in reversed(rsi_series_long):
            if r <= RSI_OVERSOLD_PERSIST_LEVEL:
                consec_oversold += 1
            else:
                break
        if consec_oversold >= RSI_OVERSOLD_PERSIST_BARS:
            reason = (
                f"{symbol} {ANCHOR_TF} RSI超卖连续保护："
                f"连续{consec_oversold}根K线RSI<={RSI_OVERSOLD_PERSIST_LEVEL}"
                f"（当前RSI={current_rsi:.1f}），动能耗尽，禁止做空"
            )
            logger.info(f"[规则过滤] {reason}")
            return True, reason

        # ── 检测2：RSI底背离 ─────────────────────────────────────────
        prev_low_price = min(hist_prices)
        prev_low_idx   = min(range(len(hist_prices)), key=lambda i: hist_prices[i])
        prev_low_rsi   = hist_rsi[prev_low_idx]
        price_drop_pct = (prev_low_price - current_price) / prev_low_price
        rsi_diff       = current_rsi - prev_low_rsi  # 正值=底背离

        if (price_drop_pct >= RSI_DIVERGENCE_MIN_DROP_PCT and
                rsi_diff >= RSI_DIVERGENCE_MIN_RSI_DIFF):
            reason = (
                f"{symbol} {ANCHOR_TF} RSI底背离：价格新低{current_price:.6f}"
                f"（前低{prev_low_price:.6f}，跌幅{price_drop_pct*100:.2f}%），"
                f"RSI={current_rsi:.1f}高于前低RSI={prev_low_rsi:.1f}（差{rsi_diff:.1f}pts），"
                f"动能衰竭，禁止做空"
            )
            logger.info(f"[规则过滤] {reason}")
            return True, reason

    elif signal_direction == "long":
        # ── 检测1：RSI超买连续保护（从最新往前数连续超买根数）──────────
        consec_overbought = 0
        for r in reversed(rsi_series_long):
            if r >= (100 - RSI_OVERSOLD_PERSIST_LEVEL):
                consec_overbought += 1
            else:
                break
        if consec_overbought >= RSI_OVERSOLD_PERSIST_BARS:
            reason = (
                f"{symbol} {ANCHOR_TF} RSI超买连续保护："
                f"连续{consec_overbought}根K线RSI>={100-RSI_OVERSOLD_PERSIST_LEVEL}"
                f"（当前RSI={current_rsi:.1f}），动能耗尽，禁止做多"
            )
            logger.info(f"[规则过滤] {reason}")
            return True, reason

        # ── 检测2：RSI顶背离 ─────────────────────────────────────────
        prev_high_price = max(hist_prices)
        prev_high_idx   = max(range(len(hist_prices)), key=lambda i: hist_prices[i])
        prev_high_rsi   = hist_rsi[prev_high_idx]
        price_rise_pct  = (current_price - prev_high_price) / prev_high_price
        rsi_diff        = prev_high_rsi - current_rsi  # 正值=顶背离

        if (price_rise_pct >= RSI_DIVERGENCE_MIN_DROP_PCT and
                rsi_diff >= RSI_DIVERGENCE_MIN_RSI_DIFF):
            reason = (
                f"{symbol} {ANCHOR_TF} RSI顶背离：价格新高{current_price:.6f}"
                f"（前高{prev_high_price:.6f}，涨幅{price_rise_pct*100:.2f}%），"
                f"RSI={current_rsi:.1f}低于前高RSI={prev_high_rsi:.1f}（差{rsi_diff:.1f}pts），"
                f"动能衰竭，禁止做多"
            )
            logger.info(f"[规则过滤] {reason}")
            return True, reason

    return False, ""
def detect_rsi_reversal_warning(
    tf_indicators: dict,
    signal_direction: str,
    symbol: str = "",
) -> Tuple[bool, str]:
    """
    趋势转折预警：检测小周期（15m/30m）是否出现 RSI 连续回升 + 量能放大。
    当做空方向时，若预警触发，应暂停做空信号。
    当做多方向时，若预警（即连续下行+量能放大）触发，应暂停做多信号。

    返回 (triggered: bool, reason: str)
    """
    if signal_direction not in ("short", "long"):
        return False, ""

    warning_tfs = [tf for tf in RSI_REVERSAL_WARNING_TFS if tf in tf_indicators]
    if not warning_tfs:
        return False, ""

    triggered_tfs = []

    for tf in warning_tfs:
        ind = tf_indicators.get(tf, {})
        if not ind.get("valid"):
            continue

        rsi_series = ind.get("rsi_series", [])   # 由 compute_timeframe_indicators 注入
        vol_ratio  = ind.get("volume_ratio", 0)

        if len(rsi_series) < RSI_REVERSAL_CONSEC_ROUNDS + 1:
            continue

        # 取最近 N+1 根，判断是否连续 N 轮单向运动
        recent = rsi_series[-(RSI_REVERSAL_CONSEC_ROUNDS + 1):]

        if signal_direction == "short":
            # 做空暂停：检测小周期RSI是否连续N轮回升
            consec_rising = all(
                recent[i] > recent[i - 1]
                for i in range(1, len(recent))
            )
            if consec_rising:
                # 量能确认：放量则预警更可信
                vol_ok = (vol_ratio >= VOL_RATIO_THRESH) if RSI_REVERSAL_VOL_CONFIRM else True
                if vol_ok:
                    triggered_tfs.append(
                        f"{tf}(RSI:{recent[0]:.1f}→{recent[-1]:.1f} 连升{RSI_REVERSAL_CONSEC_ROUNDS}轮, 量比:{vol_ratio:.2f}x)"
                    )

        elif signal_direction == "long":
            # 做多暂停：检测小周期RSI是否连续N轮下行（反向逻辑）
            consec_falling = all(
                recent[i] < recent[i - 1]
                for i in range(1, len(recent))
            )
            if consec_falling:
                vol_ok = (vol_ratio >= VOL_RATIO_THRESH) if RSI_REVERSAL_VOL_CONFIRM else True
                if vol_ok:
                    triggered_tfs.append(
                        f"{tf}(RSI:{recent[0]:.1f}→{recent[-1]:.1f} 连降{RSI_REVERSAL_CONSEC_ROUNDS}轮, 量比:{vol_ratio:.2f}x)"
                    )

    if triggered_tfs:
        direction_cn = "做空" if signal_direction == "short" else "做多"
        reason = (
            f"趋势转折预警：{'、'.join(triggered_tfs)} "
            f"出现RSI{'回升' if signal_direction=='short' else '回落'}+量能放大，"
            f"暂停{direction_cn}信号"
        )
        logger.info(f"[转折预警] {symbol} {reason}")
        return True, reason

    return False, ""


# ── 优化2：超卖反弹保护 ────────────────────────────────────────────────────
def detect_oversold_bounce_guard(
    tf_indicators: dict,
    signal_direction: str,
    symbol: str = "",
) -> Tuple[bool, str]:
    """
    超卖反弹保护：若任一周期 RSI 曾处于超卖区（≤RSI_BOUNCE_GUARD_OVERSOLD），
    且已反弹超过 RSI_BOUNCE_GUARD_DELTA 个点，判定为"反弹修复中"，禁止做空。
    对应做多方向：若 RSI 曾超买后回落超过阈值，禁止做多（对称逻辑）。

    返回 (blocked: bool, reason: str)
    """
    if signal_direction not in ("short", "long"):
        return False, ""

    OVERBOUGHT_GUARD = 100 - RSI_BOUNCE_GUARD_OVERSOLD  # 做多对称的超买判定线（约70）

    blocked_tfs = []

    for tf, ind in tf_indicators.items():
        if not ind.get("valid"):
            continue

        rsi_series = ind.get("rsi_series", [])
        if len(rsi_series) < 2:
            continue

        current_rsi = rsi_series[-1]

        if signal_direction == "short":
            # 找最近序列中是否存在超卖区低点
            min_rsi = min(rsi_series)
            if min_rsi <= RSI_BOUNCE_GUARD_OVERSOLD:
                bounce = current_rsi - min_rsi
                if bounce >= RSI_BOUNCE_GUARD_DELTA:
                    blocked_tfs.append(
                        f"{tf}(RSI低点:{min_rsi:.1f}→当前:{current_rsi:.1f}, "
                        f"反弹+{bounce:.1f}pts)"
                    )

        elif signal_direction == "long":
            # 找最近序列中是否存在超买区高点
            max_rsi = max(rsi_series)
            if max_rsi >= OVERBOUGHT_GUARD:
                pullback = max_rsi - current_rsi
                if pullback >= RSI_BOUNCE_GUARD_DELTA:
                    blocked_tfs.append(
                        f"{tf}(RSI高点:{max_rsi:.1f}→当前:{current_rsi:.1f}, "
                        f"回落-{pullback:.1f}pts)"
                    )

    if blocked_tfs:
        direction_cn = "做空" if signal_direction == "short" else "做多"
        protect_type = "超卖反弹修复" if signal_direction == "short" else "超买回落修复"
        reason = (
            f"{protect_type}保护：{'、'.join(blocked_tfs)}，"
            f"判定为修复行情，禁止{direction_cn}"
        )
        logger.info(f"[反弹保护] {symbol} {reason}")
        return True, reason

    return False, ""


# ── 优化4：多头信号规则引擎 ───────────────────────────────────────────────
def detect_long_signal_conditions(tf_indicators: dict, symbol: str = "") -> Tuple[bool, str]:
    """
    多头信号补充规则引擎，用于在规则引擎层面判断做多入场质量。
    条件：
      1. 锚周期趋势为 up（由主流程已判断，此处做二次确认）
      2. 至少一个小周期 RSI 处于回调低位（≤LONG_SIGNAL_RSI_LOW），表明已充分回调
      3. 最新一根 RSI 开始止跌回升（rsi_series[-1] > rsi_series[-2])
      4. 至少一个周期量比 ≥ LONG_SIGNAL_VOL_RATIO

    返回 (quality_ok: bool, detail: str)
    """
    quality_signals = []
    weak_signals    = []

    short_tfs = [tf for tf in tf_indicators if tf != ANCHOR_TF and tf_indicators[tf].get("valid")]

    for tf in short_tfs:
        ind = tf_indicators[tf]
        rsi_series = ind.get("rsi_series", [])
        vol_ratio  = ind.get("volume_ratio", 0)
        current_rsi = rsi_series[-1] if rsi_series else 50

        # 条件1：RSI在低位区（已充分回调）
        in_pullback_zone = current_rsi <= LONG_SIGNAL_RSI_LOW

        # 条件2：RSI止跌回升（最新根 > 前一根）
        rsi_turning_up = (
            len(rsi_series) >= 2 and rsi_series[-1] > rsi_series[-2]
        )

        # 条件3：量能达标
        vol_ok = vol_ratio >= LONG_SIGNAL_VOL_RATIO

        if in_pullback_zone and rsi_turning_up and vol_ok:
            quality_signals.append(
                f"{tf}(RSI:{current_rsi:.1f}↑回调买点, 量比:{vol_ratio:.2f}x)"
            )
        elif in_pullback_zone or rsi_turning_up:
            weak_signals.append(f"{tf}(RSI:{current_rsi:.1f})")

    if quality_signals:
        detail = f"多头质量确认：{'、'.join(quality_signals)}"
        logger.debug(f"[多头规则] {symbol} {detail}")
        return True, detail
    elif weak_signals:
        detail = f"多头信号偏弱（{'、'.join(weak_signals)}），等待更好入场点"
        logger.debug(f"[多头规则] {symbol} {detail}")
        return False, detail
    else:
        detail = "多头回调入场条件未满足（RSI未进入低位区）"
        logger.debug(f"[多头规则] {symbol} {detail}")
        return False, detail


# ── R19新增：做空质量检查（重构版）────────────────────────────────────
def detect_short_signal_quality(
    tf_indicators: dict,
    symbol: str = "",
) -> Tuple[bool, str]:
    """
    做空质量检查（重构版）：替代原来的"禁止"逻辑为"确认"逻辑。

    做空条件（4层确认）：
    1. 趋势确认: 锚周期下跌 + 至少1个小周期也下跌
    2. 入场时机: RSI从超买回调到合理区间(50-65)，非超卖非中性
    3. 小周期同步: 小周期RSI需同步下降（无反弹信号）
    4. 无底背离: 底背离是动能衰竭信号，禁止做空

    注意：此函数在规则引擎检测到底部背离后调用，
    用于进一步过滤做空信号，不影响已有的底背离保护。

    返回: (quality_ok: bool, detail: str)
    """
    anchor_ind = tf_indicators.get(ANCHOR_TF, {})
    if not anchor_ind.get("valid"):
        return False, "锚周期数据无效"

    anchor_rsi = anchor_ind.get("rsi", 50)
    anchor_trend = anchor_ind.get("trend", "sideways")
    anchor_momentum = anchor_ind.get("momentum", {})
    momentum_dir = anchor_momentum.get("direction", "neutral")

    # ── 条件1: 趋势确认 ─────────────────────────────────────────────
    # 锚周期必须是下跌趋势
    if anchor_trend != "down":
        return False, f"锚周期趋势={anchor_trend}，非下跌趋势"

    # 检查非锚周期趋势（至少1个下跌）
    check_tfs = [tf for tf in TIMEFRAMES if tf != ANCHOR_TF and tf_indicators.get(tf, {}).get("valid")]
    down_count = sum(
        1 for tf in check_tfs
        if tf_indicators.get(tf, {}).get("trend") == "down"
    )
    if down_count < 1:
        return False, f"无小周期下跌趋势支持，做空质量不足"

    # ── R21新增：做空最低ADX要求 ────────────────────────────────────
    # 注意：anchor_ind["adx"] 存储的是 compute_adx() 返回的完整字典 {"adx": ..., "plus_di": ..., "minus_di": ...}
    anchor_adx_info = anchor_ind.get("adx", {})
    anchor_adx = anchor_adx_info.get("adx", 0) if isinstance(anchor_adx_info, dict) else 0
    if anchor_adx < _SHORT_MIN_ADX:
        return False, f"ADX={anchor_adx}<{_SHORT_MIN_ADX}，趋势不够强，不适合做空"

    # ── 条件2: 入场时机 — RSI从超买回调到合理区间 ─────────────────
    # 理想做空区间: RSI在50-65（从超买回调，尚未进入反弹区）
    # 不在RSI>70做空（超买区，等回调）
    # 不在RSI<45做空（偏弱区，可能是反弹结构）
    # 不在RSI 45-50做空（中性区，趋势不明）
    # R21新增：不在RSI 35-40做空（近超卖区，反弹风险高）
    if anchor_rsi > 70:
        return False, f"RSI={anchor_rsi}仍在超买区，等待回调"
    if anchor_rsi < 45:
        return False, f"RSI={anchor_rsi}已进入偏弱区(≤45)，反弹结构禁止做空"
    # R21新增：近超卖区检查
    if 35 <= anchor_rsi < _RSI_SHORT_GUARD:
        return False, f"RSI={anchor_rsi}接近超卖区({_RSI_SHORT_GUARD})，反弹风险高"
    if 45 <= anchor_rsi < 50:
        return False, f"RSI={anchor_rsi}在中性偏弱区(45-50)，趋势不明"

    # ── 条件3: 小周期RSI需同步下降 ──────────────────────────────────
    # 小周期RSI回升意味着反弹正在发展，不适合做空
    rising_tfs = []
    for tf in ["5m", "15m"]:
        ind = tf_indicators.get(tf, {})
        if ind.get("valid"):
            rsi_series = ind.get("rsi_series", [])
            if len(rsi_series) >= 2 and rsi_series[-1] > rsi_series[-2]:
                rising_tfs.append(f"{tf}({rsi_series[-2]:.1f}→{rsi_series[-1]:.1f})")

    if rising_tfs:
        return False, f"{'、'.join(rising_tfs)}周期RSI正在回升，反弹风险高"

    # ── 条件4: 近期动能方向确认 ────────────────────────────────────
    # 近期动能应与做空方向一致
    if momentum_dir not in ("down", "neutral"):
        return False, f"近期动能={momentum_dir}，与做空方向不一致"

    # ── R21新增：做空时检测bullish形态冲突 ─────────────────────────
    # 如果检测到看涨形态（hammer/pin_bar_bull等）出现在任何周期，拒绝做空
    bullish_conflict = []
    for tf, ind in tf_indicators.items():
        if not ind.get("valid"):
            continue
        for p in ind.get("patterns", []):
            if p.get("direction") == "long" and p.get("pattern") in _BULLISH_PATTERNS:
                bullish_conflict.append(f"{p['pattern']}({tf})")

    if bullish_conflict:
        return False, f"检测到看涨形态{','.join(bullish_conflict)}，与做空方向冲突"

    # ── 质量确认通过 ───────────────────────────────────────────────
    detail = (
        f"做空质量确认: 锚周期下跌={anchor_trend}, "
        f"小周期下跌支持={down_count}/{len(check_tfs)}, "
        f"RSI={anchor_rsi}(回调合理区), "
        f"小周期无反弹"
    )
    logger.info(f"[做空质量] {symbol} {detail}")
    return True, detail


def compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> dict:
    """
    计算 ADX / +DI / -DI
    返回 {adx, plus_di, minus_di}
    """
    if len(df) < period * 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)

    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # 当 +DM < -DM 时清零，反之亦然
    plus_dm  = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)

    atr      = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean()  / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx      = dx.ewm(span=period, adjust=False).mean()

    return {
        "adx":      round(float(adx.iloc[-1]),      2),
        "plus_di":  round(float(plus_di.iloc[-1]),  2),
        "minus_di": round(float(minus_di.iloc[-1]), 2),
        "atr":      round(float(atr.iloc[-1]),      6),  # 添加 ATR 值用于止损计算
    }


def compute_volume_ratio(df: pd.DataFrame) -> float:
    """最新成交量 / MA5 成交量，> 1 表示放量"""
    if len(df) < VOL_MA_PERIOD + 1:
        return 1.0
    vol_ma = df["volume"].rolling(VOL_MA_PERIOD).mean().iloc[-1]
    if not vol_ma or vol_ma == 0:
        return 1.0
    return round(float(df["volume"].iloc[-1] / vol_ma), 2)


def compute_ema_alignment(df: pd.DataFrame) -> dict:
    """
    计算 EMA 排列状态
    返回 {ema20, ema50, ema200, alignment: "bullish"|"bearish"|"mixed"}
    """
    close = df["close"]
    result = {}
    vals = []
    for p in EMA_PERIODS:
        v = round(float(compute_ema(close, p).iloc[-1]), 6)
        result[f"ema{p}"] = v
        vals.append(v)

    if len(vals) >= 3 and vals[0] > vals[1] > vals[2]:
        result["alignment"] = "bullish"
    elif len(vals) >= 3 and vals[0] < vals[1] < vals[2]:
        result["alignment"] = "bearish"
    else:
        result["alignment"] = "mixed"
    return result


# ═══════════════════════════════════════════════════════════════════════
# 二、裸K形态识别（最近3根K线）
# ═══════════════════════════════════════════════════════════════════════

def _body(row) -> float:
    return abs(row["close"] - row["open"])

def _upper_shadow(row) -> float:
    return row["high"] - max(row["close"], row["open"])

def _lower_shadow(row) -> float:
    return min(row["close"], row["open"]) - row["low"]

def _is_bullish(row) -> bool:
    return row["close"] > row["open"]

def _is_bearish(row) -> bool:
    return row["close"] < row["open"]


def detect_candlestick_patterns(df: pd.DataFrame, trend_direction: str = None) -> list:
    """
    识别最近3根K线内的裸K形态
    返回列表，每项为 {pattern, direction, bar_index, description}
    bar_index: 0=最新，1=前一根，2=前两根

    Args:
        df: K线 DataFrame
        trend_direction: 锚周期趋势方向（'up'/'down'/'sideways'），
                         Inside Bar 据此判断方向，而非只看当前K线阴阳
    """
    patterns = []
    if len(df) < 5:
        return patterns

    c  = df.iloc[-1]   # 最新
    c1 = df.iloc[-2]   # 前1
    c2 = df.iloc[-3]   # 前2
    avg_body = df["close"].sub(df["open"]).abs().rolling(10).mean().iloc[-1]
    if avg_body == 0:
        avg_body = c["close"] * 0.005

    # ── 吞没线（最新K线吞没前一根）
    if (_is_bullish(c) and _is_bearish(c1)
            and c["open"] <= c1["close"]
            and c["close"] >= c1["open"]
            and _body(c) > _body(c1)):
        patterns.append({"pattern": "bullish_engulfing", "direction": "long",
                         "bar_index": 0, "description": "看涨吞没（最新K线）"})

    if (_is_bearish(c) and _is_bullish(c1)
            and c["open"] >= c1["close"]
            and c["close"] <= c1["open"]
            and _body(c) > _body(c1)):
        patterns.append({"pattern": "bearish_engulfing", "direction": "short",
                         "bar_index": 0, "description": "看跌吞没（最新K线）"})

    # ── 锤子线 / 倒锤子线（最新或前1根）
    for idx, bar in [(0, c), (1, c1)]:
        body = _body(bar)
        lower = _lower_shadow(bar)
        upper = _upper_shadow(bar)
        total = bar["high"] - bar["low"]
        if total == 0:
            continue
        if lower >= body * 2 and upper <= body * 0.5 and body > 0:
            patterns.append({"pattern": "hammer", "direction": "long",
                             "bar_index": idx, "description": f"锤子线（前{idx}根）"})
        if upper >= body * 2 and lower <= body * 0.5 and body > 0:
            patterns.append({"pattern": "inverted_hammer", "direction": "long",
                             "bar_index": idx, "description": f"倒锤子线（前{idx}根）"})

    # ── Pin Bar（长影线，影线 >= 实体3倍）
    for idx, bar in [(0, c), (1, c1)]:
        body = _body(bar)
        lower = _lower_shadow(bar)
        upper = _upper_shadow(bar)
        if body == 0:
            continue
        if lower >= body * 3 and lower > upper * 2:
            patterns.append({"pattern": "pin_bar_bull", "direction": "long",
                             "bar_index": idx, "description": f"看涨Pin Bar（前{idx}根）"})
        if upper >= body * 3 and upper > lower * 2:
            patterns.append({"pattern": "pin_bar_bear", "direction": "short",
                             "bar_index": idx, "description": f"看跌Pin Bar（前{idx}根）"})

    # ── 内包线（前1根被前2根包含，需趋势背景确认方向）
    # 裸K逻辑：Inside Bar 是顺趋势的突破信号，横盘里的Inside Bar无效
    # 方向由锚周期趋势决定，不以当前K线阴阳为准
    if (c1["high"] < c2["high"] and c1["low"] > c2["low"]):
        _inside_dir = None
        if trend_direction == "up":
            _inside_dir = "long"
        elif trend_direction == "down":
            _inside_dir = "short"
        # 横盘或无趋势：Inside Bar 无效，直接跳过
        if _inside_dir is not None:
            patterns.append({"pattern": "inside_bar", "direction": _inside_dir,
                             "bar_index": 1, "description": f"内包线突破({trend_direction})"})

    # ── 启明星（三根：大阴 + 小实体 + 大阳）
    if (len(df) >= 4
            and _is_bearish(c2) and _body(c2) > avg_body
            and _body(c1) < avg_body * 0.5
            and _is_bullish(c) and _body(c) > avg_body
            and c["close"] > (c2["open"] + c2["close"]) / 2):
        patterns.append({"pattern": "morning_star", "direction": "long",
                         "bar_index": 0, "description": "启明星形态"})

    # ── 黄昏星
    if (len(df) >= 4
            and _is_bullish(c2) and _body(c2) > avg_body
            and _body(c1) < avg_body * 0.5
            and _is_bearish(c) and _body(c) > avg_body
            and c["close"] < (c2["open"] + c2["close"]) / 2):
        patterns.append({"pattern": "evening_star", "direction": "short",
                         "bar_index": 0, "description": "黄昏星形态"})

    return patterns


# ═══════════════════════════════════════════════════════════════════════
# 三、单边趋势判断（核心过滤逻辑）
# ═══════════════════════════════════════════════════════════════════════

def assess_recent_trend_momentum(df: pd.DataFrame) -> dict:
    """
    近期趋势动能评估：解耦 EMA200 的惯性滞后问题。

    核心逻辑：
      1. 取最近 RECENT_TREND_WINDOW 根K线（默认20根）
      2. 统计其中看涨/看跌K线的比例
      3. 计算近期窗口内的总价格变动幅度（相对变化%）
      4. 对比近期价格vs背景窗口中段价格，判断是否从横盘中"突破启动"

    返回：
      {
        "direction":   "up" / "down" / "neutral",  # 近期动能方向
        "bull_pct":    float,   # 看涨K线占比
        "bear_pct":    float,   # 看跌K线占比
        "total_move":  float,   # 近期总价格变动（%，正=上涨，负=下跌）
        "breakout":    bool,    # 是否从背景区间突破（price越过context中位数且振幅>min_move）
        "description": str,     # 自然语言描述，注入LLM快照
      }
    """
    if df is None or len(df) < RECENT_TREND_WINDOW + 1:
        return {"direction": "neutral", "bull_pct": 0.5, "bear_pct": 0.5,
                "total_move": 0.0, "breakout": False, "description": "近期K线数据不足"}

    recent  = df.iloc[-RECENT_TREND_WINDOW:]
    context = df.iloc[-(CONTEXT_WINDOW):] if len(df) >= CONTEXT_WINDOW else df

    # ① 统计近期方向一致性
    bull_bars = (recent["close"] > recent["open"]).sum()
    bear_bars = (recent["close"] < recent["open"]).sum()
    total     = len(recent)
    bull_pct  = round(bull_bars / total, 3)
    bear_pct  = round(bear_bars / total, 3)

    # ② 近期总价格变动
    price_start  = float(recent["close"].iloc[0])
    price_end    = float(recent["close"].iloc[-1])
    total_move   = round((price_end - price_start) / price_start, 4) if price_start > 0 else 0.0

    # ③ 背景中位价 —— 判断近期是否从"相对平稳的背景"中突破
    #    前提：背景窗口本身是低波动（横盘），否则中位价无参考意义
    #    用背景窗口的价格振幅（(high-low)/mid）衡量背景是否平稳
    context_amplitude = (float(context["close"].max()) - float(context["close"].min()))
    context_mid_val   = float(context["close"].median())
    context_volatility = (context_amplitude / context_mid_val) if context_mid_val > 0 else 1.0
    # 背景平稳判定：振幅 < 8%（背景本身是横盘，中位价才有意义）
    context_is_stable = context_volatility < 0.08

    context_mid   = context_mid_val
    breakout_up   = context_is_stable and price_end > context_mid and total_move >= RECENT_TREND_MIN_MOVE
    breakout_down = context_is_stable and price_end < context_mid and total_move <= -RECENT_TREND_MIN_MOVE

    # ④ 综合判定近期动能方向
    if bull_pct >= RECENT_TREND_MIN_PCT and total_move >= RECENT_TREND_MIN_MOVE:
        direction = "up"
    elif bear_pct >= RECENT_TREND_MIN_PCT and total_move <= -RECENT_TREND_MIN_MOVE:
        direction = "down"
    else:
        direction = "neutral"

    # ⑤ 自然语言描述（供LLM快照使用）
    move_pct_str = f"{total_move*100:+.1f}%"
    if direction == "up":
        desc = (f"近{RECENT_TREND_WINDOW}根看涨占比{bull_pct:.0%}，"
                f"区间涨幅{move_pct_str}，"
                f"{'突破背景中位价↑' if breakout_up else '区间内上行'}")
    elif direction == "down":
        desc = (f"近{RECENT_TREND_WINDOW}根看跌占比{bear_pct:.0%}，"
                f"区间跌幅{move_pct_str}，"
                f"{'跌破背景中位价↓' if breakout_down else '区间内下行'}")
    else:
        desc = (f"近{RECENT_TREND_WINDOW}根涨跌各占{bull_pct:.0%}/{bear_pct:.0%}，"
                f"区间变动{move_pct_str}，方向不明（横盘/震荡）")

    return {
        "direction":          direction,
        "bull_pct":           bull_pct,
        "bear_pct":           bear_pct,
        "total_move":         total_move,
        "breakout":           breakout_up or breakout_down,
        "context_is_stable":  context_is_stable,   # 背景是否平稳（振幅<8%）
        "context_volatility": round(context_volatility, 4),
        "description":        desc,
    }

def assess_trend_direction(df: pd.DataFrame, adx_info: dict, ema_info: dict, symbol: str = None) -> str:
    """
    综合 EMA 排列 + ADX + 收盘价位置 + 近期动能 判断单边趋势方向
    返回 "up" / "down" / "sideways"

    评分规则（满分5分，≥3分判为趋势）：
      EMA排列（2分）：解决中长期趋势结构确认
      DI方向（1分）：ADX方向分量确认
      价格vs EMA21（1分）：短期位置确认
      近期动能（1分）：[新增] 解耦EMA200惯性，感知趋势刚启动
    """
    adx = adx_info.get("adx", 0)
    plus_di  = adx_info.get("plus_di",  0)
    minus_di = adx_info.get("minus_di", 0)
    alignment = ema_info.get("alignment", "mixed")
    ema_fast = ema_info.get(f"ema{_EMA_FAST}", 0)
    current_price = float(df["close"].iloc[-1])

    # 近期动能评估（独立于EMA，专门捕捉趋势刚启动的信号）
    momentum = assess_recent_trend_momentum(df)
    momentum_dir = momentum["direction"]

    # ADX门槛：ADX<20时额外看近期动能，若动能明确可豁免ADX门槛（刚启动场景）
    adx_ok = adx >= ADX_THRESHOLD
    # 豁免条件：近期动能方向明确 + 从背景中位价突破（代表真实趋势启动）
    adx_waived = (not adx_ok) and momentum["breakout"] and momentum_dir != "neutral"

    if not adx_ok and not adx_waived:
        logger.debug(
            f"[趋势判断] {symbol or ''} ADX={adx:.2f}<{ADX_THRESHOLD}，"
            f"近期动能={momentum_dir}(突破={momentum['breakout']})，判横盘"
        )
        return "sideways"

    bullish_signals = 0
    bearish_signals = 0

    # ① EMA排列（权重2分）—— 成熟趋势的结构确认
    if alignment == "bullish":
        bullish_signals += 2
    elif alignment == "bearish":
        bearish_signals += 2

    # ② DI方向（权重1分）
    if plus_di > minus_di:
        bullish_signals += 1
    else:
        bearish_signals += 1

    # ③ 价格 vs EMA21（权重1分）
    if current_price > ema_fast:
        bullish_signals += 1
    else:
        bearish_signals += 1

    # ④ [新增] 近期动能（权重1分）：解耦EMA200惯性，感知趋势刚启动
    if momentum_dir == "up":
        bullish_signals += 1
    elif momentum_dir == "down":
        bearish_signals += 1

    symbol_tag = f"{symbol} " if symbol else ""
    adx_tag = f"ADX={adx:.2f}{'(豁免)' if adx_waived else ''}"
    logger.debug(
        f"[{symbol_tag}趋势判断] {adx_tag} | EMA={alignment} | "
        f"+DI={plus_di:.2f} vs -DI={minus_di:.2f} | 价格vs EMA21 | "
        f"动能={momentum_dir}({momentum['description'][:20]}) | "
        f"得分：多={bullish_signals}/空={bearish_signals} → "
        f"{'up' if bullish_signals>=3 else 'down' if bearish_signals>=3 else 'sideways'}"
    )

    if bullish_signals >= 3:
        return "up"
    elif bearish_signals >= 3:
        return "down"
    return "sideways"


def detect_momentum_acceleration(df: pd.DataFrame) -> dict:
    """
    动量加速检测（优化1）：比较最近N根K线平均实体大小 vs 前M根基准均值。
    实体大小 = abs(close - open)
    返回：{"accelerating": bool, "decelerating": bool, "ratio": float,
           "recent_avg": float, "baseline_avg": float}
    """
    recent_bars   = _IND_CFG.get("momentum_accel_recent_bars",   3)
    baseline_bars = _IND_CFG.get("momentum_accel_baseline_bars", 10)
    accel_ratio   = _IND_CFG.get("momentum_accel_ratio",         1.5)
    decel_ratio   = 0.8  # 近期/基准 < 此值视为动量衰减

    min_bars = recent_bars + baseline_bars
    if df is None or len(df) < min_bars:
        return {"accelerating": False, "decelerating": False, "ratio": 1.0,
                "recent_avg": 0.0, "baseline_avg": 0.0}

    bodies = (df["close"] - df["open"]).abs()
    recent_avg   = float(bodies.iloc[-recent_bars:].mean())
    baseline_avg = float(bodies.iloc[-(recent_bars + baseline_bars):-recent_bars].mean())

    if baseline_avg == 0:
        return {"accelerating": False, "decelerating": False, "ratio": 1.0,
                "recent_avg": recent_avg, "baseline_avg": baseline_avg}

    ratio = round(recent_avg / baseline_avg, 2)
    return {
        "accelerating":  ratio >= accel_ratio,
        "decelerating":  ratio < decel_ratio,
        "ratio":         ratio,
        "recent_avg":    round(recent_avg, 6),
        "baseline_avg":  round(baseline_avg, 6),
    }


def detect_volume_price_alignment(df: pd.DataFrame, signal_direction: str) -> dict:
    """
    量价同向验证（优化3）：检测方向量能是否强于反向量能。
    多头：最近N根阳线平均量 > 阴线平均量 * threshold → aligned=True
    空头：最近N根阴线平均量 > 阳线平均量 * threshold → aligned=True
    返回：{"aligned": bool, "bull_vol_avg": float, "bear_vol_avg": float, "ratio": float}
    """
    lookback  = _IND_CFG.get("vol_price_align_lookback",  3)
    threshold = _IND_CFG.get("vol_price_align_threshold", 1.0)

    if df is None or len(df) < lookback * 2:
        return {"aligned": True, "bull_vol_avg": 0.0, "bear_vol_avg": 0.0, "ratio": 1.0}

    recent = df.iloc[-lookback * 2:]
    bull_vols = recent.loc[recent["close"] > recent["open"], "volume"]
    bear_vols = recent.loc[recent["close"] < recent["open"], "volume"]

    bull_avg = float(bull_vols.mean()) if len(bull_vols) > 0 else 0.0
    bear_avg = float(bear_vols.mean()) if len(bear_vols) > 0 else 0.0

    if signal_direction == "long":
        denom = bear_avg if bear_avg > 0 else 1.0
        ratio = round(bull_avg / denom, 2)
        aligned = ratio >= threshold
    else:
        denom = bull_avg if bull_avg > 0 else 1.0
        ratio = round(bear_avg / denom, 2)
        aligned = ratio >= threshold

    return {
        "aligned":      aligned,
        "bull_vol_avg": round(bull_avg, 2),
        "bear_vol_avg": round(bear_avg, 2),
        "ratio":        ratio,
    }


def detect_momentum_decay(df: pd.DataFrame, signal_direction: str) -> dict:
    """
    动量衰减检测（优化4）：持仓期间检测趋势动量是否衰减。
    条件1：连续N根K线实体逐渐缩小
    条件2：出现方向相反的显著影线（影线/实体 > shadow_ratio）
    两个条件同时满足才判定为动量衰减。
    返回：{"decaying": bool, "reason": str, "body_shrinking": bool, "shadow_pressure": bool}
    """
    lookback     = _IND_CFG.get("momentum_decay_lookback",     3)
    shadow_ratio = _IND_CFG.get("momentum_decay_shadow_ratio", 1.0)

    if df is None or len(df) < lookback + 1:
        return {"decaying": False, "reason": "数据不足", "body_shrinking": False, "shadow_pressure": False}

    recent = df.iloc[-lookback:]
    bodies = (recent["close"] - recent["open"]).abs().tolist()

    # 条件1：实体逐渐缩小（每根 < 前一根）
    body_shrinking = all(bodies[i] < bodies[i - 1] for i in range(1, len(bodies)))

    # 条件2：反向影线压力
    shadow_pressure = False
    last = df.iloc[-1]
    body = abs(last["close"] - last["open"])
    if body > 0:
        if signal_direction == "long":
            # 多头：上影线（卖压）显著
            upper_shadow = last["high"] - max(last["close"], last["open"])
            shadow_pressure = (upper_shadow / body) >= shadow_ratio
        else:
            # 空头：下影线（买压）显著
            lower_shadow = min(last["close"], last["open"]) - last["low"]
            shadow_pressure = (lower_shadow / body) >= shadow_ratio

    decaying = body_shrinking and shadow_pressure
    reason = ""
    if decaying:
        reason = (
            f"动量衰减：实体连续缩小{lookback}根"
            f"（{[round(b, 6) for b in bodies]}），"
            f"{'上' if signal_direction == 'long' else '下'}影线压力显著"
        )

    return {
        "decaying":       decaying,
        "reason":         reason,
        "body_shrinking": body_shrinking,
        "shadow_pressure": shadow_pressure,
    }


def compute_timeframe_indicators(df: pd.DataFrame, tf_label: str, symbol: str = None) -> dict:
    """
    对单个时间框架计算所有指标，返回完整的指标字典
    """
    if df is None or df.empty or len(df) < 20:
        logger.warning(f"[{tf_label}] 数据不足，跳过指标计算")
        return {"trend": "sideways", "valid": False, "timeframe": tf_label}

    ema_info    = compute_ema_alignment(df)
    adx_info    = compute_adx(df)
    rsi         = compute_rsi(df["close"])
    rsi_series  = compute_rsi_series(df["close"], lookback=4)  # 最近4根RSI序列
    rsi_series_long = compute_rsi_series(df["close"], lookback=RSI_DIVERGENCE_LOOKBACK)  # 底背离检测用长序列
    price_series = df["close"].tail(RSI_DIVERGENCE_LOOKBACK).tolist()  # 底背离检测用价格序列
    vol_ratio   = compute_volume_ratio(df)
    momentum    = assess_recent_trend_momentum(df)              # 近期动能评估
    trend       = assess_trend_direction(df, adx_info, ema_info, symbol)
    patterns    = detect_candlestick_patterns(df, trend_direction=trend)

    # ── P0优化：彻底关闭 inside_bar 信号（两月7/7全亏，亏损-120U）────────
    _inside_bar_enabled = _PATTERN_FILTER_CFG.get('inside_bar_enabled', True)
    if not _inside_bar_enabled:
        _before = len(patterns)
        patterns = [p for p in patterns if p.get('pattern') != 'inside_bar']
        if len(patterns) < _before:
            logger.info(f"[形态过滤] inside_bar 已关闭，移除 {_before - len(patterns)} 个信号")

    # ── Round 8：彻底关闭 bearish_engulfing 做空信号（7连败0胜率亏损-176U）
    _bearish_engulfing_short_ban = _PATTERN_FILTER_CFG.get('bearish_engulfing_short_ban', False)
    if _bearish_engulfing_short_ban:
        _before = len(patterns)
        patterns = [p for p in patterns if not (p.get('pattern') == 'bearish_engulfing' and p.get('direction') == 'short')]
        if len(patterns) < _before:
            logger.info(f"[形态过滤] bearish_engulfing 做空已关闭，移除 {_before - len(patterns)} 个信号")

    mom_accel   = detect_momentum_acceleration(df)              # 动量加速检测（优化1）

    current_price = float(df["close"].iloc[-1])

    # 获取当前 bar 的 UTC 毫秒时间戳（用于时段判断）
    try:
        ts_ms = int(df.index[-1].value // 10**6)
    except Exception:
        ts_ms = 0

    return {
        "timeframe":    tf_label,
        "valid":        True,
        "current_price": current_price,
        "trend":        trend,
        "ema":          ema_info,
        "adx":          adx_info,
        "rsi":          rsi,
        "rsi_series":   rsi_series,   # [rsi_t-3, rsi_t-2, rsi_t-1, rsi_t]
        "rsi_series_long": rsi_series_long,  # 底背离检测用长RSI序列
        "price_series": price_series,         # 底背离检测用价格序列
        "volume_ratio": vol_ratio,
        "momentum":     momentum,     # 近期动能：{direction, bull_pct, bear_pct, total_move, breakout, description}
        "momentum_acceleration": mom_accel,  # 动量加速：{accelerating, decelerating, ratio}
        "vol_price_alignment": None,  # 量价同向：在 rule_engine_filter 中按方向计算后注入
        "patterns":     patterns,
        "atr":          adx_info.get("atr", 0),  # ATR用于止损计算
        "ts_ms":        ts_ms,        # 当前 bar UTC ms 时间戳（用于时段判断）
    }


# ═══════════════════════════════════════════════════════════════════════
# 四、规则引擎预过滤（进入 LLM 前的门卫）
# ═══════════════════════════════════════════════════════════════════════

def rule_engine_filter(
    tf_indicators: dict,
    symbol: str,
) -> tuple:
    """
    单边趋势规则引擎预过滤。
    只有通过后才值得花 token 调用 LLM。

    参数：
        tf_indicators: 各周期指标字典，key 为时间框架字符串
        symbol: 合约名称（仅用于日志）

    返回：
        (passed: bool, signal_direction: str, reason: str)
        signal_direction: "long" / "short" / "wait"
    """
    # ── 1. 方向锚周期必须有明确方向（非横盘）
    anchor = tf_indicators.get(ANCHOR_TF, {})
    anchor_trend = anchor.get("trend", "sideways") if anchor.get("valid") else "sideways"

    if REQUIRE_ANCHOR and anchor_trend == "sideways":
        reason = f"{symbol} 锚周期 {ANCHOR_TF} 横盘（ADX<{ADX_THRESHOLD}），规则引擎拒绝"
        logger.info(f"[规则过滤] {reason}")
        return False, "wait", reason

    # ── 2. 统计非锚周期的单边趋势数量
    check_tfs = [tf for tf in TIMEFRAMES if tf != ANCHOR_TF]
    up_count   = 0
    down_count = 0
    tf_summary = []

    for tf in check_tfs:
        ind = tf_indicators.get(tf, {})
        if not ind.get("valid"):
            tf_summary.append(f"{tf}=无效")
            continue
        t = ind.get("trend", "sideways")
        tf_summary.append(f"{tf}={t}")
        if t == "up":
            up_count += 1
        elif t == "down":
            down_count += 1

    logger.info(f"[规则过滤] {symbol} 趋势统计: 锚={ANCHOR_TF}:{anchor_trend} | {' '.join(tf_summary)}")

    # ── 3. 判断信号方向：非锚周期达标且与锚周期对齐
    signal_direction = "wait"

    if up_count >= MIN_TRENDING_TF and (not REQUIRE_ANCHOR or anchor_trend == "up"):
        signal_direction = "long"
    elif down_count >= MIN_TRENDING_TF and (not REQUIRE_ANCHOR or anchor_trend == "down"):
        signal_direction = "short"
    else:
        reason = (
            f"{symbol} 趋势不一致，多头周期={up_count}/{len(check_tfs)}，"
            f"空头周期={down_count}/{len(check_tfs)}，{ANCHOR_TF}锚={anchor_trend}，"
            f"需要{MIN_TRENDING_TF}个非锚周期对齐"
        )
        logger.info(f"[规则过滤] {reason}")
        return False, "wait", reason

    # ── 3b. 量价同向验证（优化3）：计算并注入锚周期量价对齐信息
    anchor_df_key = ANCHOR_TF
    # 此处 tf_indicators 已包含各周期指标，但原始 df 需从外部传入
    # 通过 momentum 数据间接判断：若锚周期动量方向与信号方向一致，量价对齐概率更高
    # 实际量价对齐计算在 ai_analysis.py 中通过 multi_tf_data 完成
    anchor_mom_accel = anchor.get("momentum_acceleration", {})
    if anchor_mom_accel.get("decelerating", False):
        logger.info(
            f"[规则过滤] {symbol} 锚周期动量衰减（实体比={anchor_mom_accel.get('ratio', 1.0):.2f}），"
            f"信号强度将被降低"
        )

    # ── 4. 趋势强度豁免检查（方案D：极强趋势中跳过RSI超卖/超买保护）
    anchor_adx_info = anchor.get("adx", {})
    anchor_adx = anchor_adx_info.get("adx", 0) if isinstance(anchor_adx_info, dict) else 0
    anchor_plus_di = anchor_adx_info.get("plus_di", 0) if isinstance(anchor_adx_info, dict) else 0
    anchor_minus_di = anchor_adx_info.get("minus_di", 0) if isinstance(anchor_adx_info, dict) else 0
    di_diff = abs(anchor_plus_di - anchor_minus_di)

    # 初步判断是否满足极强趋势指标条件
    strong_trend_indicators = (
        anchor_adx >= STRONG_TREND_ADX_THRESHOLD and
        di_diff >= STRONG_TREND_DI_DIFF_THRESHOLD
    )

    # 极强趋势豁免前，追加检查近期趋势连续性（避免刚从横盘突破的短暂极强趋势）
    strong_trend_exemption = False
    if strong_trend_indicators:
        anchor_momentum = anchor.get("momentum", {})
        momentum_direction = anchor_momentum.get("direction", "neutral")

        # 近期动能必须与信号方向一致，且不能是横盘
        momentum_aligned = (
            (signal_direction == "long" and momentum_direction == "up") or
            (signal_direction == "short" and momentum_direction == "down")
        )

        if momentum_aligned:
            strong_trend_exemption = True
            logger.info(
                f"[规则过滤] {symbol} 极强趋势豁免：ADX={anchor_adx:.1f}(>={STRONG_TREND_ADX_THRESHOLD}), "
                f"DI差值={di_diff:.1f}(>={STRONG_TREND_DI_DIFF_THRESHOLD}), "
                f"近期动能={momentum_direction}（与{signal_direction}对齐），跳过RSI超卖/超买保护"
            )
        else:
            logger.info(
                f"[规则过滤] {symbol} 极强趋势指标达标但近期动能不一致："
                f"ADX={anchor_adx:.1f}, DI差值={di_diff:.1f}, "
                f"近期动能={momentum_direction}（期望{'up' if signal_direction=='long' else 'down'}），"
                f"判定为横盘突破初期，不予豁免"
            )

    # ── 4b. RSI 底背离/顶背离检测（方案E：即使极强趋势豁免也执行）
    divergence_detected, divergence_reason = detect_rsi_divergence(
        tf_indicators, signal_direction, symbol
    )
    if divergence_detected:
        return False, "wait", divergence_reason

    # ── 5. RSI 极值过滤（趋势末端保护，强趋势时豁免）
    # 做多：RSI超买禁止；做空：使用新的做空质量检查替代旧的多层禁止规则
    if not strong_trend_exemption:
        # 检查是否满足 ADX 豁免条件（中等强度趋势即可豁免，无需极强趋势）
        adx_exemption_threshold = _RULE_CFG.get("rsi_adx_exemption_threshold", 40)
        adx_exemption_enabled = _RULE_CFG.get("rsi_adx_exemption_enabled", False)
        adx_exemption_active = adx_exemption_enabled and anchor_adx >= adx_exemption_threshold

        if adx_exemption_active:
            logger.info(
                f"[规则过滤] {symbol} ADX 豁免 RSI 超买/超卖保护：ADX={anchor_adx:.1f}(>={adx_exemption_threshold})，"
                f"允许在 RSI 极端值下开仓（防止强趋势 RSI 钝化漏单）"
            )
        else:
            # 不满足豁免条件，执行正常 RSI 过滤
            # 做多：只检查锚周期的 RSI 超买
            anchor_ind = tf_indicators.get(ANCHOR_TF, {})
            if anchor_ind.get("valid"):
                rsi = anchor_ind.get("rsi", 50)
                if signal_direction == "long" and rsi >= RSI_OVERBOUGHT:
                    reason = f"{symbol} {ANCHOR_TF} RSI={rsi} 超买（>={RSI_OVERBOUGHT}），拒绝做多"
                    logger.info(f"[规则过滤] {reason}")
                    return False, "wait", reason
                # ── R19重构：做空使用新的做空质量检查
                # 旧逻辑：RSI超卖禁止 + RSI中性偏弱区禁止 + 超卖反弹保护 + 时段禁止
                # 新逻辑：趋势确认 + RSI回调位置 + 小周期同步 + 无反弹风险
                if signal_direction == "short":
                    short_ok, short_detail = detect_short_signal_quality(tf_indicators, symbol)
                    if not short_ok:
                        reason = f"{symbol} 做空质量不达标：{short_detail}"
                        logger.info(f"[规则过滤] {reason}")
                        return False, "wait", reason
                    else:
                        logger.info(f"[规则过滤] {symbol} {short_detail}")

    # ── 6. 成交量确认（至少一个小周期放量，极强趋势时豁免）
    if not strong_trend_exemption:
        vol_confirmed = any(
            tf_indicators.get(tf, {}).get("volume_ratio", 0) >= VOL_RATIO_THRESH
            for tf in check_tfs
            if tf_indicators.get(tf, {}).get("valid")
        )
        if not vol_confirmed:
            reason = f"{symbol} 成交量不足（各周期量比均<{VOL_RATIO_THRESH}），信号可靠性低"
            logger.info(f"[规则过滤] {reason}")
            return False, "wait", reason

    # ── 7. [R19重构] 做空已使用新的做空质量检查（detect_short_signal_quality）
    #    旧规则（超卖反弹保护、时段禁止）已移除，由新函数统一处理

    # ── 8. 趋势转折预警：仅对做多方向执行（做空由新函数处理）
    if not strong_trend_exemption and signal_direction == "long":
        reversal_warned, reversal_reason = detect_rsi_reversal_warning(tf_indicators, signal_direction, symbol)
        if reversal_warned:
            logger.info(f"[规则过滤] {symbol} {reversal_reason}")
            return False, "wait", f"{symbol} {reversal_reason}"

    # ── 9. [优化4] 做多质量检查：多头信号需满足回调买点条件（非硬过滤，仅记录质量）
    if signal_direction == "long":
        long_ok, long_detail = detect_long_signal_conditions(tf_indicators, symbol)
        if not long_ok:
            # 做多质量不达标：记录warning但不硬过滤，由LLM最终判断
            logger.info(f"[规则过滤] {symbol} 做多质量提示：{long_detail}（LLM继续判断）")
        else:
            logger.info(f"[规则过滤] {symbol} {long_detail}")

    # ── 10. 规则引擎通过
    reason = (
        f"{symbol} 规则引擎通过：方向={signal_direction}，"
        f"{ANCHOR_TF}锚={anchor_trend}，对齐周期={'多头' if signal_direction=='long' else '空头'}{max(up_count,down_count)}/{len(check_tfs)}"
    )
    logger.info(f"[规则过滤] {reason}")
    return True, signal_direction, reason


# ═══════════════════════════════════════════════════════════════════════
# 五、多周期市场快照生成（LLM 输入文本）
# ═══════════════════════════════════════════════════════════════════════

TF_LABELS = {
    "5m":  "5分钟 5M",
    "15m": "15分钟 15M",
    "30m": "30分钟 30M",
    "1h":  "1小时 1H",
    "4h":  "4小时 4H",
    "1d":  "日线 1D",
}


def generate_market_snapshot(
    multi_tf_data: dict,
    symbol: str,
    support_levels: list = None,
    resistance_levels: list = None,
) -> tuple:
    """
    将多周期 OHLCV DataFrame 计算为结构化文本快照。

    返回：
        (snapshot_text: str, tf_indicators: dict)
        tf_indicators 同时返回，供规则引擎复用，避免重复计算。
    """
    tf_indicators = {}
    lines = []

    current_price = None
    for tf in TIMEFRAMES:
        df = multi_tf_data.get(tf)
        if df is not None and not df.empty:
            current_price = float(df["close"].iloc[-1])
            break

    lines.append(f"【合约】{symbol}  当前价格：{current_price:,.6g}")
    lines.append("")

    for tf in TIMEFRAMES:
        df = multi_tf_data.get(tf)
        label = TF_LABELS.get(tf, tf)
        ind = compute_timeframe_indicators(df, tf, symbol)
        tf_indicators[tf] = ind

        if not ind.get("valid"):
            lines.append(f"【{label}】数据不足，跳过")
            lines.append("")
            continue

        adx    = ind["adx"]
        ema    = ind["ema"]
        rsi    = ind["rsi"]
        rsi_s  = ind.get("rsi_series", [rsi])    # RSI序列（优化3）
        vr     = ind["volume_ratio"]
        trend  = ind["trend"]
        pats   = ind["patterns"]
        mom    = ind.get("momentum", {})          # 近期动能

        trend_cn  = {"up": "上升", "down": "下降", "sideways": "横盘"}.get(trend, trend)
        adx_cn    = f"ADX:{adx['adx']}（{'趋势' if adx['adx']>=ADX_THRESHOLD else '横盘'}）"
        align_cn  = {"bullish": "多头排列", "bearish": "空头排列", "mixed": "混乱排列"}.get(ema["alignment"])
        vol_cn    = f"{vr}x（{'放量' if vr>=VOL_RATIO_THRESH else '缩量/平量'}）"
        pat_cn    = "，".join(p["description"] for p in pats) if pats else "无明显形态"
        # RSI动态趋势描述（优化3）
        rsi_delta_cn = _rsi_delta_description(rsi_s)
        # 近期动能描述（新增）
        mom_cn = mom.get("description", "")

        lines.append(f"【{label}】")
        lines.append(f"趋势：{trend_cn} | EMA排列：{align_cn} | {adx_cn}")
        lines.append(f"RSI：{rsi} | RSI趋势：{rsi_delta_cn} | 成交量/MA5：{vol_cn}")
        if mom_cn:
            lines.append(f"近期动能：{mom_cn}")

        # 动量加速信息（优化1）
        mom_accel = ind.get("momentum_acceleration", {})
        if mom_accel.get("accelerating"):
            lines.append(f"动量加速：实体放大{mom_accel.get('ratio', 1.0):.1f}x（趋势加速中）")
        elif mom_accel.get("decelerating"):
            lines.append(f"动量衰减：实体缩小{mom_accel.get('ratio', 1.0):.1f}x（动能减弱）")

        lines.append(f"K线形态：{pat_cn}")

        # 支撑阻力（仅入场周期显示，入场周期的支撑阻力对开仓决策最直接）
        if tf == TIMEFRAMES[-1] and support_levels and resistance_levels:
            sup_str = " / ".join(f"{v:,.6g}" for v in support_levels[:2])
            res_str = " / ".join(f"{v:,.6g}" for v in resistance_levels[:2])
            lines.append(f"支撑：{sup_str} | 阻力：{res_str}")
        lines.append("")

    # 多周期共振汇总
    trends = {tf: tf_indicators[tf].get("trend", "sideways") for tf in TIMEFRAMES}
    up_cnt   = sum(1 for t in trends.values() if t == "up")
    down_cnt = sum(1 for t in trends.values() if t == "down")
    align_score = max(up_cnt, down_cnt)
    total_tfs = len(TIMEFRAMES)
    dominant = "多头" if up_cnt >= down_cnt else "空头"
    lines.append(f"【多周期共振】{dominant}对齐 {align_score}/{total_tfs} | 各周期：" +
                 " ".join(f"{tf}={trends[tf]}" for tf in TIMEFRAMES))

    snapshot = "\n".join(lines)
    return snapshot, tf_indicators
