#!/usr/bin/env python3
"""
trade_manager.py
持仓管理技能：动态止损、部分止盈、趋势反转检测
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from dotenv import load_dotenv
load_dotenv()

# 配置日志：同时输出到控制台和文件
from config_loader import check_env, RISK_CFG, TRADE_MGR_CFG, setup_logging
check_env()
setup_logging("trade_manager")
logger = logging.getLogger(__name__)

# ── 持仓管理阈值（全部从 settings.yaml trade_manager 节点读取）
TRAILING_STOP_PCT    = TRADE_MGR_CFG.get("trailing_stop_trigger_pct",  15.0)
PARTIAL_PROFIT_PCT   = TRADE_MGR_CFG.get("partial_profit_trigger_pct", 25.0)
PARTIAL_PROFIT_RATIO = TRADE_MGR_CFG.get("partial_profit_ratio",       0.5)
FORCE_CLOSE_PCT      = TRADE_MGR_CFG.get("force_close_loss_pct",      -10.0)
STRUCTURE_TF         = TRADE_MGR_CFG.get("structure_check_timeframe",  "1h")
SUPPORT_BUFFER_PCT   = TRADE_MGR_CFG.get("support_buffer_pct",          0.3)

from execute_trade import (
    create_exchange,
    get_open_positions,
    close_position,
)
from fetch_kline import (
    fetch_multi_timeframe,
    detect_trend_structure,
)

from notifier import send_notification


def main():
    """主执行流程"""
    logger.info("🚀 Trade Manager 启动")
    
    exchange = create_exchange()
    
    # ── 第一步：获取当前持仓 ──
    positions = get_open_positions(exchange)
    
    if not positions:
        logger.info("无持仓，跳过本轮管理")
        # send_notification("当前无持仓")
        return
    
    logger.info(f"当前持仓数量：{len(positions)}")
    
    total_pnl = 0
    closed_count = 0
    
    # ── 第二步：逐个处理持仓 ──
    for pos in positions:
        symbol = pos.get("symbol")
        side = pos.get("side", "")
        contracts = pos.get("contracts", 0)
        entry_price = pos.get("entry_price", 0)
        unrealized_pnl = pos.get("unrealized_pnl", 0)
        margin = pos.get("margin", 1)
        
        logger.info(f"\n--- 处理持仓 [{symbol}] ---")
        logger.info(f"  方向：{side} | 数量：{contracts} | 入场：{entry_price}")
        logger.info(f"  浮盈亏：{unrealized_pnl:.2f} USDT")
        
        try:
            # 计算盈亏百分比
            pnl_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0
            total_pnl += unrealized_pnl
            
            # 2.1 获取最新K线数据
            data = fetch_multi_timeframe(symbol, exchange=exchange)
            
            # ── 2.2 盈亏状态判断 ──
            
            # 情况A：浮盈超过阈值，移动止损至保本
            if pnl_pct > TRAILING_STOP_PCT:
                logger.info(f"  浮盈{pnl_pct:.1f}%（>{TRAILING_STOP_PCT}%），移动止损至保本位")
                try:
                    # 撤销原止损单
                    exchange.cancel_all_orders(symbol)
                    # 重新挂保本止损
                    exchange.create_order(
                        symbol=symbol,
                        type="stop_market",
                        side="sell" if side == "long" else "buy",
                        amount=contracts,
                        price=float(entry_price),
                        params={
                            "stopLossPrice": float(entry_price),
                            "reduceOnly": True,
                            "tdMode": "cross",
                        },
                    )
                    send_notification(
                        f"✅ {symbol} 浮盈{pnl_pct:.1f}%，已将止损移至保本位 {entry_price}"
                    )
                except Exception as e:
                    logger.error(f"  ⚠️ 移动止损失败：{e}")
            
            # 情况B：浮盈超过阈值，部分止盈
            if pnl_pct > PARTIAL_PROFIT_PCT:
                partial_contracts = int(contracts * PARTIAL_PROFIT_RATIO)
                logger.info(f"  浮盈{pnl_pct:.1f}%（>{PARTIAL_PROFIT_PCT}%），执行部分止盈{int(PARTIAL_PROFIT_RATIO*100)}%")
                try:
                    # 市价平掉一半
                    exchange.create_order(
                        symbol=symbol,
                        type="market",
                        side="sell" if side == "long" else "buy",
                        amount=partial_contracts,
                        params={"tdMode": "cross"},
                    )
                    send_notification(
                        f"{symbol} 浮盈{pnl_pct:.1f}%，已部分止盈{int(PARTIAL_PROFIT_RATIO*100)}%（{partial_contracts}张），剩余持仓继续运行"
                    )
                    closed_count += 1
                except Exception as e:
                    logger.error(f"  ⚠️ 部分止盈失败：{e}")
            
            # 情况C：亏损超过阈值，强制平仓
            if pnl_pct < FORCE_CLOSE_PCT:
                logger.info(f"  亏损{pnl_pct:.1f}%（<{FORCE_CLOSE_PCT}%），触发动态止损")
                try:
                    close_position(exchange, symbol, reason=f"动态止损：亏损{pnl_pct:.1f}%")
                    send_notification(f"{symbol} 亏损{pnl_pct:.1f}%，触发动态止损，已强制平仓")
                    closed_count += 1
                except Exception as e:
                    logger.error(f"  强制平仓失败：{e}")

            # 情况D：盈亏在正常范围内
            if FORCE_CLOSE_PCT <= pnl_pct <= TRAILING_STOP_PCT:
                logger.info(f"  持仓状态正常，盈亏{pnl_pct:.1f}%")

            # ── 2.3 趋势反转检测（纯价格结构，不调用AI）──
            if STRUCTURE_TF in data and not data[STRUCTURE_TF].empty:
                structure_tf = detect_trend_structure(data[STRUCTURE_TF])
                structure_broken = structure_tf.get("structure_broken", False)

                # 获取当前价格（最新K线收盘价）
                current_price = float(data[STRUCTURE_TF].iloc[-1]["close"])

                # 计算支撑阻力位
                from fetch_kline import calculate_support_resistance
                support_levels, resistance_levels = calculate_support_resistance(data[STRUCTURE_TF])

                should_close = False
                close_reason = ""

                if structure_broken:
                    # 结构已破坏，直接平仓
                    should_close = True
                    close_reason = f"{STRUCTURE_TF.upper()}结构破坏（structure_broken=True），当前价：{current_price}"

                elif side == "long" and support_levels:
                    # 做多：跌破最近支撑位（允许缓冲区）
                    nearest_support = support_levels[0]
                    buffer = 1 - SUPPORT_BUFFER_PCT / 100
                    if current_price < nearest_support * buffer:
                        should_close = True
                        close_reason = f"做多跌破支撑位 {nearest_support:.4f}（缓冲{SUPPORT_BUFFER_PCT}%），当前价：{current_price}"

                elif side == "short" and resistance_levels:
                    # 做空：突破最近阻力位（允许缓冲区）
                    nearest_resistance = resistance_levels[0]
                    buffer = 1 + SUPPORT_BUFFER_PCT / 100
                    if current_price > nearest_resistance * buffer:
                        should_close = True
                        close_reason = f"做空突破阻力位 {nearest_resistance:.4f}（缓冲{SUPPORT_BUFFER_PCT}%），当前价：{current_price}"

                if should_close:
                    logger.warning(f"  {symbol} 触发结构平仓：{close_reason}")
                    close_position(exchange, symbol, reason=close_reason)
                    send_notification(f"{symbol} 结构平仓\n原因：{close_reason}")
                    closed_count += 1
                else:
                    logger.info(f"  {symbol} 结构完好，持仓继续 | 当前价：{current_price}")
            
            # 2.4 更新持仓日志
            _save_position_log(pos, pnl_pct)
            
        except Exception as e:
            logger.error(f"  ❌ 处理持仓异常：{e}")
            continue
    
    # ── 第三步：汇总报告 ──
    remaining = len(positions) - closed_count
    logger.info("=" * 50)
    logger.info(f"持仓巡检完成 | 持仓数：{remaining} | 净盈亏：{total_pnl:.2f} USDT")
    logger.info("=" * 50)
    
    send_notification(
        f"持仓巡检完成\n"
        f"持仓数：{remaining}\n"
        f"总浮盈亏：{total_pnl:.2f} USDT\n"
        f"已平仓：{closed_count}"
    )


def _save_position_log(position: dict, pnl_pct: float):
    """保存持仓状态到日志"""
    import json
    from datetime import timezone
    
    log_dir = Path("logs/trades")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    symbol_safe = position.get("symbol", "UNKNOWN").replace("/", "_").replace(":", "_")
    
    log_data = {
        "timestamp": ts,
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "contracts": position.get("contracts"),
        "entry_price": position.get("entry_price"),
        "unrealized_pnl": position.get("unrealized_pnl"),
        "pnl_pct": f"{pnl_pct:.2f}%",
    }
    
    log_path = log_dir / f"position_{symbol_safe}_{ts}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()