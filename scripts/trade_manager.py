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
TRAILING_STOP_PCT      = TRADE_MGR_CFG.get("trailing_stop_trigger_pct",   15.0)
PARTIAL_PROFIT_PCT     = TRADE_MGR_CFG.get("partial_profit_trigger_pct",   25.0)
PARTIAL_PROFIT_RATIO_1 = TRADE_MGR_CFG.get("partial_profit_ratio_1",        0.3)
PARTIAL_PROFIT_PCT_2   = TRADE_MGR_CFG.get("partial_profit_trigger_pct_2", 50.0)
PARTIAL_PROFIT_RATIO_2 = TRADE_MGR_CFG.get("partial_profit_ratio_2",        0.5)
FORCE_CLOSE_PCT        = TRADE_MGR_CFG.get("force_close_loss_pct",         -10.0)
STRUCTURE_TF           = TRADE_MGR_CFG.get("structure_check_timeframe",     "1h")
SUPPORT_BUFFER_PCT     = TRADE_MGR_CFG.get("support_buffer_pct",             0.3)

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
from trade_report import generate_close_report


def _cancel_all_symbol_orders(exchange, symbol: str):
    """撤销某品种所有挂单（OKX 不支持 cancel_all_orders，逐个取消）"""
    try:
        open_orders = exchange.fetch_orders(symbol, params={"instType": "SWAP", "state": "live"})
        for order in open_orders:
            try:
                exchange.cancel_order(order["id"], symbol)
            except Exception as e:
                logger.warning(f"  撤单失败 {order['id']}: {e}")
    except Exception as e:
        logger.warning(f"  查询挂单失败：{e}")


def _move_stop_to_breakeven(exchange, symbol: str, side: str, contracts: float, entry_price: float):
    """将止损移至保本位（入场价）"""
    try:
        _cancel_all_symbol_orders(exchange, symbol)
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
        logger.info(f"  止损已移至保本位 {entry_price}")
    except Exception as e:
        logger.error(f"  ⚠️ 移动止损至保本失败：{e}")


def main():
    """主执行流程"""
    logger.info("🚀 Trade Manager 启动")

    exchange = create_exchange()

    # ── 第一步：获取当前持仓 ──
    positions = get_open_positions(exchange)

    if not positions:
        logger.info("无持仓，跳过本轮管理")
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

        logger.info(f"\n--- 处理持仓 [{symbol}] ---")
        logger.info(f"  方向：{side} | 数量：{contracts} | 入场：{entry_price}")
        logger.info(f"  浮盈亏：{unrealized_pnl:.2f} USDT")

        try:
            # 计算盈亏百分比：直接使用交易所返回的 percentage，避免 margin 缺失导致错误
            pnl_pct = pos.get("percentage", 0)
            total_pnl += unrealized_pnl

            # 2.1 获取最新K线数据
            data = fetch_multi_timeframe(symbol, exchange=exchange)

            # ── 2.2 盈亏状态判断 ──

            # 情况A：浮盈在移动止损阈值和第一批止盈阈值之间，仅移止损至保本
            # 注：浮盈超过 PARTIAL_PROFIT_PCT 时，止盈逻辑（情况B）内部会自行移止损，无需重复
            if TRAILING_STOP_PCT < pnl_pct < PARTIAL_PROFIT_PCT:
                logger.info(f"  浮盈{pnl_pct:.1f}%（>{TRAILING_STOP_PCT}%），移动止损至保本位")
                try:
                    _move_stop_to_breakeven(exchange, symbol, side, contracts, entry_price)
                    send_notification(
                        f"✅ {symbol} 浮盈{pnl_pct:.1f}%，已将止损移至保本位 {entry_price}"
                    )
                except Exception as e:
                    logger.error(f"  ⚠️ 移动止损失败：{e}")

            # 情况B：分两批部分止盈（使用实时持仓量，止盈后立即保本）
            # 第二批：浮盈 > PARTIAL_PROFIT_PCT_2，平 PARTIAL_PROFIT_RATIO_2 仓位
            # 第一批：浮盈 > PARTIAL_PROFIT_PCT，平 PARTIAL_PROFIT_RATIO_1 仓位
            if pnl_pct > PARTIAL_PROFIT_PCT_2:
                live_positions = get_open_positions(exchange)
                live_pos = next(
                    (p for p in live_positions if p["symbol"] == symbol and p["side"] == side), None
                )
                live_contracts = live_pos["contracts"] if live_pos else contracts
                partial_contracts = int(live_contracts * PARTIAL_PROFIT_RATIO_2)
                if partial_contracts > 0:
                    logger.info(f"  浮盈{pnl_pct:.1f}%（>{PARTIAL_PROFIT_PCT_2:.0f}%），第二批止盈{int(PARTIAL_PROFIT_RATIO_2*100)}%（{partial_contracts}张）")
                    try:
                        exchange.create_order(
                            symbol=symbol,
                            type="market",
                            side="sell" if side == "long" else "buy",
                            amount=partial_contracts,
                            params={"tdMode": "cross", "reduceOnly": True},
                        )
                        send_notification(
                            f"{symbol} 浮盈{pnl_pct:.1f}%，第二批止盈{int(PARTIAL_PROFIT_RATIO_2*100)}%（{partial_contracts}张），剩余持仓继续运行"
                        )
                        closed_count += 1
                        _move_stop_to_breakeven(exchange, symbol, side, live_contracts - partial_contracts, entry_price)
                    except Exception as e:
                        logger.error(f"  ⚠️ 第二批止盈失败：{e}")
            elif pnl_pct > PARTIAL_PROFIT_PCT:
                live_positions = get_open_positions(exchange)
                live_pos = next(
                    (p for p in live_positions if p["symbol"] == symbol and p["side"] == side), None
                )
                live_contracts = live_pos["contracts"] if live_pos else contracts
                partial_contracts = int(live_contracts * PARTIAL_PROFIT_RATIO_1)
                if partial_contracts > 0:
                    logger.info(f"  浮盈{pnl_pct:.1f}%（>{PARTIAL_PROFIT_PCT}%），第一批止盈{int(PARTIAL_PROFIT_RATIO_1*100)}%（{partial_contracts}张）")
                    try:
                        exchange.create_order(
                            symbol=symbol,
                            type="market",
                            side="sell" if side == "long" else "buy",
                            amount=partial_contracts,
                            params={"tdMode": "cross", "reduceOnly": True},
                        )
                        send_notification(
                            f"{symbol} 浮盈{pnl_pct:.1f}%，第一批止盈{int(PARTIAL_PROFIT_RATIO_1*100)}%（{partial_contracts}张），剩余持仓继续运行"
                        )
                        closed_count += 1
                        _move_stop_to_breakeven(exchange, symbol, side, live_contracts - partial_contracts, entry_price)
                    except Exception as e:
                        logger.error(f"  ⚠️ 第一批止盈失败：{e}")

            # 情况C：亏损超过阈值，强制平仓
            if pnl_pct < FORCE_CLOSE_PCT:
                logger.info(f"  亏损{pnl_pct:.1f}%（<{FORCE_CLOSE_PCT}%），触发动态止损")
                try:
                    close_position(exchange, symbol, reason=f"动态止损：亏损{pnl_pct:.1f}%")
                    send_notification(f"{symbol} 亏损{pnl_pct:.1f}%，触发动态止损，已强制平仓")
                    closed_count += 1
                    generate_close_report(symbol, f"动态止损：亏损{pnl_pct:.1f}%", unrealized_pnl, pnl_pct)
                except Exception as e:
                    logger.error(f"  强制平仓失败：{e}")

            # 情况D：盈亏在正常范围内
            if FORCE_CLOSE_PCT <= pnl_pct <= TRAILING_STOP_PCT:
                logger.info(f"  持仓状态正常，盈亏{pnl_pct:.1f}%")

            # ── 2.3 趋势反转检测（纯价格结构，不调用AI）──
            if STRUCTURE_TF in data and not data[STRUCTURE_TF].empty:
                structure_tf = detect_trend_structure(data[STRUCTURE_TF])
                structure_broken = structure_tf.get("structure_broken", False)

                current_price = float(data[STRUCTURE_TF].iloc[-1]["close"])

                from fetch_kline import calculate_support_resistance
                rt_supports, rt_resistances = calculate_support_resistance(data[STRUCTURE_TF])

                # 优先使用开仓时 AI 标注的 key_support/key_resistance
                ai_support = pos.get("key_support")
                ai_resistance = pos.get("key_resistance")
                if ai_support and float(ai_support) > 0:
                    support_levels = [float(ai_support)] + [s for s in rt_supports if s != float(ai_support)]
                else:
                    support_levels = rt_supports
                if ai_resistance and float(ai_resistance) > 0:
                    resistance_levels = [float(ai_resistance)] + [r for r in rt_resistances if r != float(ai_resistance)]
                else:
                    resistance_levels = rt_resistances

                should_close = False
                close_reason = ""

                if structure_broken:
                    should_close = True
                    close_reason = f"{STRUCTURE_TF.upper()}结构破坏（structure_broken=True），当前价：{current_price}"

                elif side == "long" and support_levels:
                    nearest_support = support_levels[0]
                    buffer = 1 - SUPPORT_BUFFER_PCT / 100
                    if current_price < nearest_support * buffer:
                        should_close = True
                        close_reason = f"做多跌破支撑位 {nearest_support:.6g}（缓冲{SUPPORT_BUFFER_PCT}%），当前价：{current_price}"

                elif side == "short" and resistance_levels:
                    nearest_resistance = resistance_levels[0]
                    buffer = 1 + SUPPORT_BUFFER_PCT / 100
                    if current_price > nearest_resistance * buffer:
                        should_close = True
                        close_reason = f"做空突破阻力位 {nearest_resistance:.6g}（缓冲{SUPPORT_BUFFER_PCT}%），当前价：{current_price}"

                if should_close:
                    logger.warning(f"  {symbol} 触发结构平仓：{close_reason}")
                    close_position(exchange, symbol, reason=close_reason)
                    send_notification(f"{symbol} 结构平仓\n原因：{close_reason}")
                    closed_count += 1
                    generate_close_report(symbol, close_reason, unrealized_pnl, pnl_pct)
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
        "key_support": position.get("key_support"),
        "key_resistance": position.get("key_resistance"),
    }

    log_path = log_dir / f"position_{symbol_safe}_{ts}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
