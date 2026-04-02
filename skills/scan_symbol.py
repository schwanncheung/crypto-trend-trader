#!/usr/bin/env python3
"""
OpenClaw Skill: 扫描指定合约
用法: python skills/scan_symbol.py BTC/USDT:USDT
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dotenv import load_dotenv
from config_loader import check_env, TIMEFRAMES, setup_logging
from fetch_kline import fetch_multi_timeframe, calculate_support_resistance
from ai_analysis import analyze_symbol, save_decision_log, passes_risk_filter
from execute_trade import create_exchange, get_open_positions, execute_from_decision
from notifier import send_notification

load_dotenv()
check_env()
setup_logging("scan_symbol")

logger = logging.getLogger(__name__)


def scan_single_symbol(symbol: str):
    """扫描单个合约"""
    logger.info(f"🔍 开始扫描 {symbol}")

    exchange = create_exchange()

    # 检查是否已持有
    positions = get_open_positions(exchange)
    symbol_base = symbol.split("/")[0]
    for pos in positions:
        if pos.get("symbol", "").split("/")[0] == symbol_base:
            msg = f"⚠️ 已持有 {symbol}，跳过开仓"
            logger.warning(msg)
            print(msg)
            return

    # 获取K线数据
    data = fetch_multi_timeframe(symbol, exchange=exchange)
    anchor_tf = TIMEFRAMES[0]

    if data[anchor_tf].empty:
        msg = f"❌ {symbol} 数据获取失败"
        logger.error(msg)
        print(msg)
        return

    # 计算支撑阻力
    support, resistance = calculate_support_resistance(data[anchor_tf])

    # AI 分析
    decision = analyze_symbol(
        multi_tf_data=data,
        symbol=symbol,
        support_levels=support,
        resistance_levels=resistance,
    )

    logger.info(f"分析结果: signal={decision.get('signal')}, "
                f"confidence={decision.get('confidence')}, "
                f"strength={decision.get('signal_strength')}")

    # 规则过滤检查
    if decision.get("_analysis_mode") == "rule_filter_rejected":
        msg = f"⚠️ {symbol} 规则过滤未通过: {decision.get('_filter_reason')}"
        logger.warning(msg)
        print(msg)
        send_notification(msg)
        return

    # 风控检查
    if not passes_risk_filter(decision):
        msg = f"⚠️ {symbol} 风控未通过"
        logger.warning(msg)
        print(msg)
        send_notification(msg)
        return

    # 执行交易
    result = execute_from_decision(exchange, symbol, decision)

    if result.get("status") == "success":
        msg = (f"✅ {symbol} 开仓成功\n"
               f"方向: {decision.get('signal')}\n"
               f"入场: {decision.get('entry_price')}\n"
               f"止损: {decision.get('stop_loss')}\n"
               f"止盈: {decision.get('take_profit')}")
        logger.info(msg)
        print(msg)
        send_notification(msg)
    else:
        msg = f"❌ {symbol} 开仓失败: {result.get('reason')}"
        logger.error(msg)
        print(msg)
        send_notification(msg)

    # 保存决策日志
    save_decision_log(symbol=symbol, timeframe="multi", decision=decision)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python skills/scan_symbol.py <SYMBOL>")
        print("示例: python skills/scan_symbol.py BTC/USDT:USDT")
        sys.exit(1)

    symbol = sys.argv[1]

    try:
        scan_single_symbol(symbol)
        sys.exit(0)
    except Exception as e:
        print(f"❌ 扫描失败: {e}")
        logger.exception(e)
        sys.exit(1)
