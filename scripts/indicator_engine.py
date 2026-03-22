#!/usr/bin/env python3
"""
indicator_engine.py
规则引擎：技术指标计算 + 裸K形态识别 + 单边趋势过滤 + 市场快照生成

输出结构化文本快照供 LLM 文本分析使用，也作为规则预过滤门卫。
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional

from config_loader import ANALYSIS_CFG, TIMEFRAMES

logger = logging.getLogger(__name__)

# ── 读取配置 ──────────────────────────────────────────────────────────
_IND_CFG  = ANALYSIS_CFG.get("indicator", {})
_RULE_CFG = ANALYSIS_CFG.get("rule_filter", {})

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


def detect_candlestick_patterns(df: pd.DataFrame) -> list:
    """
    识别最近3根K线内的裸K形态
    返回列表，每项为 {pattern, direction, bar_index, description}
    bar_index: 0=最新，1=前一根，2=前两根
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
            direction = "long" if _is_bullish(bar) else "long"  # 锤子做多
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

    # ── 内包线（前1根被前2根包含，最新K线方向确认）
    if (c1["high"] < c2["high"] and c1["low"] > c2["low"]):
        direction = "long" if _is_bullish(c) else "short"
        patterns.append({"pattern": "inside_bar", "direction": direction,
                         "bar_index": 1, "description": f"内包线突破（方向:{direction}）"})

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

def assess_trend_direction(df: pd.DataFrame, adx_info: dict, ema_info: dict) -> str:
    """
    综合 EMA 排列 + ADX + 收盘价位置 判断单边趋势方向
    返回 "up" / "down" / "sideways"
    """
    adx = adx_info.get("adx", 0)
    plus_di  = adx_info.get("plus_di",  0)
    minus_di = adx_info.get("minus_di", 0)
    alignment = ema_info.get("alignment", "mixed")
    ema_fast = ema_info.get(f"ema{_EMA_FAST}", 0)
    current_price = float(df["close"].iloc[-1])

    if adx < ADX_THRESHOLD:
        return "sideways"

    bullish_signals = 0
    bearish_signals = 0

    if alignment == "bullish":
        bullish_signals += 2
    elif alignment == "bearish":
        bearish_signals += 2

    if plus_di > minus_di:
        bullish_signals += 1
    else:
        bearish_signals += 1

    if current_price > ema_fast:
        bullish_signals += 1
    else:
        bearish_signals += 1

    if bullish_signals >= 3:
        return "up"
    elif bearish_signals >= 3:
        return "down"
    return "sideways"


def compute_timeframe_indicators(df: pd.DataFrame, tf_label: str) -> dict:
    """
    对单个时间框架计算所有指标，返回完整的指标字典
    """
    if df is None or df.empty or len(df) < 20:
        logger.warning(f"[{tf_label}] 数据不足，跳过指标计算")
        return {"trend": "sideways", "valid": False, "timeframe": tf_label}

    ema_info    = compute_ema_alignment(df)
    adx_info    = compute_adx(df)
    rsi         = compute_rsi(df["close"])
    vol_ratio   = compute_volume_ratio(df)
    trend       = assess_trend_direction(df, adx_info, ema_info)
    patterns    = detect_candlestick_patterns(df)

    current_price = float(df["close"].iloc[-1])

    return {
        "timeframe":   tf_label,
        "valid":       True,
        "current_price": current_price,
        "trend":       trend,
        "ema":         ema_info,
        "adx":         adx_info,
        "rsi":         rsi,
        "volume_ratio": vol_ratio,
        "patterns":    patterns,
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

    # ── 4. RSI 极值过滤（趋势末端保护）
    for tf in check_tfs:
        ind = tf_indicators.get(tf, {})
        if not ind.get("valid"):
            continue
        rsi = ind.get("rsi", 50)
        if signal_direction == "long" and rsi >= RSI_OVERBOUGHT:
            reason = f"{symbol} {tf} RSI={rsi} 超买（>={RSI_OVERBOUGHT}），拒绝做多"
            logger.info(f"[规则过滤] {reason}")
            return False, "wait", reason
        if signal_direction == "short" and rsi <= RSI_OVERSOLD:
            reason = f"{symbol} {tf} RSI={rsi} 超卖（<={RSI_OVERSOLD}），拒绝做空"
            logger.info(f"[规则过滤] {reason}")
            return False, "wait", reason

    # ── 5. 成交量确认（至少一个小周期放量）
    vol_confirmed = any(
        tf_indicators.get(tf, {}).get("volume_ratio", 0) >= VOL_RATIO_THRESH
        for tf in check_tfs
        if tf_indicators.get(tf, {}).get("valid")
    )
    if not vol_confirmed:
        reason = f"{symbol} 成交量不足（各周期量比均<{VOL_RATIO_THRESH}），信号可靠性低"
        logger.info(f"[规则过滤] {reason}")
        return False, "wait", reason

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
        ind = compute_timeframe_indicators(df, tf)
        tf_indicators[tf] = ind

        if not ind.get("valid"):
            lines.append(f"【{label}】数据不足，跳过")
            lines.append("")
            continue

        adx    = ind["adx"]
        ema    = ind["ema"]
        rsi    = ind["rsi"]
        vr     = ind["volume_ratio"]
        trend  = ind["trend"]
        pats   = ind["patterns"]

        trend_cn  = {"up": "上升", "down": "下降", "sideways": "横盘"}.get(trend, trend)
        adx_cn    = f"ADX:{adx['adx']}（{'趋势' if adx['adx']>=ADX_THRESHOLD else '横盘'}）"
        align_cn  = {"bullish": "多头排列", "bearish": "空头排列", "mixed": "混乱排列"}.get(ema["alignment"])
        vol_cn    = f"{vr}x（{'放量' if vr>=VOL_RATIO_THRESH else '缩量/平量'}）"
        pat_cn    = "，".join(p["description"] for p in pats) if pats else "无明显形态"

        lines.append(f"【{label}】")
        lines.append(f"趋势：{trend_cn} | EMA排列：{align_cn} | {adx_cn}")
        lines.append(f"RSI：{rsi} | 成交量/MA5：{vol_cn}")
        lines.append(f"K线形态：{pat_cn}")

        # 支撑阻力（仅 4h 层面显示传入值，其余显示空）
        if tf == "4h" and support_levels and resistance_levels:
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
