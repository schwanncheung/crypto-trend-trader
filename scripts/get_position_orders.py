#!/usr/bin/env python3
"""
获取持仓的止盈止损价格
"""

import ccxt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from execute_trade import create_exchange

def get_position_orders(exchange, symbol: str) -> dict:
    """
    获取持仓的止盈止损挂单价格

    返回：
    {
        "stop_loss": float or None,
        "take_profit": float or None
    }
    """
    try:
        orders = exchange.fetch_open_orders(symbol, params={"instType": "SWAP"})

        stop_loss = None
        take_profit = None

        for order in orders:
            info = order.get("info", {})
            # OKX 止损单
            sl_trigger = info.get("slTriggerPx") or info.get("stopLossPrice")
            if sl_trigger:
                stop_loss = float(sl_trigger)

            # OKX 止盈单
            tp_trigger = info.get("tpTriggerPx") or info.get("takeProfitPrice")
            if tp_trigger:
                take_profit = float(tp_trigger)

        return {"stop_loss": stop_loss, "take_profit": take_profit}

    except Exception as e:
        print(f"获取 {symbol} 挂单失败：{e}")
        return {"stop_loss": None, "take_profit": None}


if __name__ == "__main__":
    exchange = create_exchange()

    # 测试
    symbol = "BTC/USDT:USDT"
    orders = get_position_orders(exchange, symbol)
    print(f"{symbol} 止损: {orders['stop_loss']}, 止盈: {orders['take_profit']}")
