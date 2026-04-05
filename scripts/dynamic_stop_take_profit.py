#!/usr/bin/env python3
"""
动态止损计算模块
根据 ADX 强度自动调整止损距离

⚠️ 修改止损逻辑后，请同步更新 CLAUDE.md「动态止损」章节
"""

import logging
from config_loader import TRADING_CFG

logger = logging.getLogger(__name__)


def calculate_dynamic_stop_loss(
    entry_price: float,
    atr: float,
    signal: str,
    adx: float = None
) -> tuple[float, float]:
    """
    根据 ADX 动态计算止损距离

    参数：
        entry_price: 入场价格
        atr: 平均真实波幅
        signal: "long" 或 "short"
        adx: ADX 值（可选，用于动态调整）

    返回：
        (stop_loss, multiplier_used)
    """
    base_multiplier = TRADING_CFG.get("stop_loss_atr_multiplier", 2.5)
    adx_scaling_cfg = TRADING_CFG.get("stop_loss_adx_scaling", {})

    # 默认使用基础倍数
    multiplier = base_multiplier

    # 如果启用 ADX 动态调整且提供了 ADX 值
    if adx_scaling_cfg.get("enabled", False) and adx is not None:
        extreme_threshold = adx_scaling_cfg.get("extreme_trend_threshold", 60)
        strong_threshold = adx_scaling_cfg.get("strong_trend_threshold", 40)
        extreme_multiplier = adx_scaling_cfg.get("extreme_trend_multiplier", 2.0)
        strong_multiplier = adx_scaling_cfg.get("strong_trend_multiplier", 1.5)

        if adx >= extreme_threshold:
            # 极强趋势：止损距离 × 2.0
            multiplier = base_multiplier * extreme_multiplier
            logger.info(
                f"ADX={adx:.1f} 极强趋势，止损倍数调整为 "
                f"{base_multiplier} × {extreme_multiplier} = {multiplier:.2f}"
            )
        elif adx >= strong_threshold:
            # 强趋势：止损距离 × 1.5
            multiplier = base_multiplier * strong_multiplier
            logger.info(
                f"ADX={adx:.1f} 强趋势，止损倍数调整为 "
                f"{base_multiplier} × {strong_multiplier} = {multiplier:.2f}"
            )
        else:
            logger.info(f"ADX={adx:.1f} 正常趋势，使用基础止损倍数 {base_multiplier}")
    else:
        logger.info(f"使用基础止损倍数 {base_multiplier}")

    # 计算止损价格
    if signal == "long":
        stop_loss = entry_price - multiplier * atr
    else:  # short
        stop_loss = entry_price + multiplier * atr

    # ── 硬性上限检查（风控红线）──────────────────────
    max_stop_loss_pct = TRADING_CFG.get("max_stop_loss_pct", 3.0) / 100
    stop_loss_distance = abs(stop_loss - entry_price)
    stop_loss_distance_pct = stop_loss_distance / entry_price

    if stop_loss_distance_pct > max_stop_loss_pct:
        # 超过上限，强制收缩到上限
        logger.warning(
            f"止损距离 {stop_loss_distance_pct*100:.2f}% 超过上限 {max_stop_loss_pct*100:.1f}%，"
            f"强制收缩到上限"
        )
        if signal == "long":
            stop_loss = entry_price * (1 - max_stop_loss_pct)
        else:  # short
            stop_loss = entry_price * (1 + max_stop_loss_pct)

        logger.info(f"止损价格已调整为：{stop_loss:.6g}（距离：{max_stop_loss_pct*100:.1f}%）")

    return stop_loss, multiplier


def calculate_take_profit(
    entry_price: float,
    stop_loss: float,
    signal: str,
    key_support: float = None,
    key_resistance: float = None,
    adx: float = None
) -> tuple[float, str]:
    """
    智能计算止盈价格（考虑关键位和 ADX）

    优先级：
    1. 关键位优先：止盈不超过最近的关键支撑/阻力
    2. ADX 调整：强趋势可以适当放宽止盈距离
    3. 最小 R:R 保护：确保至少 1:1

    参数：
        entry_price: 入场价格
        stop_loss: 止损价格
        signal: "long" 或 "short"
        key_support: 关键支撑位（做空时的目标）
        key_resistance: 关键阻力位（做多时的目标）
        adx: ADX 值（用于动态调整）

    返回：
        (take_profit, reason): 止盈价格和设置原因
    """
    target_rr_ratio = TRADING_CFG.get("target_rr_ratio", 1.2)
    risk = abs(entry_price - stop_loss)

    # 基础止盈：按 R:R 计算
    if signal == "long":
        base_tp = entry_price + target_rr_ratio * risk
    else:  # short
        base_tp = entry_price - target_rr_ratio * risk

    # 检查关键位限制
    key_level = None
    if signal == "long" and key_resistance:
        # 做多：止盈不能超过阻力位，且阻力位必须在入场价上方
        if entry_price < key_resistance < entry_price * 1.5:  # 阻力位合理性检查
            buffer = key_resistance * 0.003  # 0.3% 缓冲
            key_level = key_resistance - buffer
    elif signal == "short" and key_support:
        # 做空：止盈不能低于支撑位
        if 0 < key_support < entry_price:  # 支撑位合理性检查
            buffer = key_support * 0.003  # 0.3% 缓冲
            key_level = key_support + buffer

    # ADX 动态调整（强趋势可以适当放宽）
    adx_scaling_cfg = TRADING_CFG.get("stop_loss_adx_scaling", {})
    if adx_scaling_cfg.get("enabled", False) and adx is not None and adx >= 50:
        # ADX >= 50 的强趋势，允许止盈距离 × 1.3
        tp_distance = abs(base_tp - entry_price)
        if signal == "long":
            extended_tp = entry_price + tp_distance * 1.3
        else:
            extended_tp = entry_price - tp_distance * 1.3
        logger.info(f"ADX={adx:.1f} 强趋势，止盈距离可放宽 30%")
    else:
        extended_tp = base_tp

    # 决策逻辑
    if key_level:
        # 有关键位限制
        if signal == "long":
            if extended_tp > key_level:
                # 止盈超过阻力位，收缩到阻力位前
                actual_tp = key_level
                actual_rr = (actual_tp - entry_price) / risk
                if actual_rr < 1.0:
                    # R:R 不足 1:1，放弃关键位限制
                    actual_tp = entry_price + risk  # 至少 1:1
                    reason = f"阻力位 {key_level:.6g} 过近，使用最小 R:R=1:1"
                else:
                    reason = f"受阻力位 {key_level:.6g} 限制，R:R={actual_rr:.2f}"
            else:
                actual_tp = extended_tp
                reason = f"基础 R:R={target_rr_ratio}"
        else:  # short
            if extended_tp < key_level:
                # 止盈低于支撑位，收缩到支撑位上
                actual_tp = key_level
                actual_rr = (entry_price - actual_tp) / risk
                if actual_rr < 1.0:
                    # R:R 不足 1:1，放弃关键位限制
                    actual_tp = entry_price - risk  # 至少 1:1
                    reason = f"支撑位 {key_level:.6g} 过近，使用最小 R:R=1:1"
                else:
                    reason = f"受支撑位 {key_level:.6g} 限制，R:R={actual_rr:.2f}"
            else:
                actual_tp = extended_tp
                reason = f"基础 R:R={target_rr_ratio}"
    else:
        # 无关键位限制
        actual_tp = extended_tp
        reason = f"基础 R:R={target_rr_ratio}"

    # ── 硬性上限检查（风控红线）──────────────────────
    max_take_profit_pct = TRADING_CFG.get("max_take_profit_pct", 5.0) / 100
    take_profit_distance = abs(actual_tp - entry_price)
    take_profit_distance_pct = take_profit_distance / entry_price

    if take_profit_distance_pct > max_take_profit_pct:
        # 超过上限，强制收缩到上限
        logger.warning(
            f"止盈距离 {take_profit_distance_pct*100:.2f}% 超过上限 {max_take_profit_pct*100:.1f}%，"
            f"强制收缩到上限"
        )
        if signal == "long":
            actual_tp = entry_price * (1 + max_take_profit_pct)
        else:  # short
            actual_tp = entry_price * (1 - max_take_profit_pct)

        logger.info(f"止盈价格已调整为：{actual_tp:.6g}（距离：{max_take_profit_pct*100:.1f}%）")
        reason = f"受上限 {max_take_profit_pct*100:.1f}% 限制"

    logger.info(f"止盈计算：{reason}")
    return actual_tp, reason


def calculate_trailing_stop(
    current_price: float,
    atr: float,
    signal: str,
) -> float:
    """
    ATR 跟踪止损计算（优化2）：基于当前价格动态计算跟踪止损位。

    参数：
        current_price: 当前市场价格
        atr: 当前 ATR 值
        signal: "long" 或 "short"

    返回：
        trailing_stop_price: 跟踪止损价格
    """
    multiplier = TRADING_CFG.get("trailing_stop_atr_multiplier", 1.5)

    if signal == "long":
        trailing_stop = current_price - multiplier * atr
    else:  # short
        trailing_stop = current_price + multiplier * atr

    logger.info(
        f"跟踪止损计算：当前价={current_price:.6g}, ATR={atr:.6g}, "
        f"倍数={multiplier}, 跟踪止损={trailing_stop:.6g}"
    )
    return trailing_stop
