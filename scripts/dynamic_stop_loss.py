#!/usr/bin/env python3
"""
动态止损计算模块
根据 ADX 强度自动调整止损距离
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

    return stop_loss, multiplier


def calculate_take_profit(
    entry_price: float,
    stop_loss: float,
    signal: str
) -> float:
    """
    根据止损距离计算止盈价格

    参数：
        entry_price: 入场价格
        stop_loss: 止损价格
        signal: "long" 或 "short"

    返回：
        take_profit: 止盈价格
    """
    target_rr_ratio = TRADING_CFG.get("target_rr_ratio", 1.2)

    if signal == "long":
        risk = entry_price - stop_loss
        take_profit = entry_price + target_rr_ratio * risk
    else:  # short
        risk = stop_loss - entry_price
        take_profit = entry_price - target_rr_ratio * risk

    return take_profit
