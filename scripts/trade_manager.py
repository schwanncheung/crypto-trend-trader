#!/usr/bin/env python3
"""
trade_manager.py
持仓管理技能：动态止损、部分止盈、趋势反转检测
"""

import sys
import logging
from pathlib import Path

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

# 配置日志：同时输出到控制台和文件
from config_loader import check_env, TRADE_MGR_CFG, setup_logging, now_cst_str, TIMEFRAMES
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
MOMENTUM_DECAY_ENABLED = TRADE_MGR_CFG.get("momentum_decay_exit_enabled",   True)
MOMENTUM_DECAY_MIN_PCT = TRADE_MGR_CFG.get("momentum_decay_min_profit_pct",  5.0)

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
from stop_loss_tracker import record_stop_loss_manual, detect_and_record_stop_loss, save_position_snapshot
from file_lock import atomic_read_json, atomic_write_json, atomic_update_json
from dynamic_stop_take_profit import calculate_trailing_stop
from indicator_engine import detect_momentum_decay


def _cancel_all_symbol_orders(exchange, symbol: str):
    """撤销某品种所有挂单（OKX 不支持 cancel_all_orders，逐个取消）"""
    try:
        open_orders = exchange.fetch_open_orders(symbol, params={"instType": "SWAP"})
        for order in open_orders:
            try:
                exchange.cancel_order(order["id"], symbol)
            except Exception as e:
                logger.warning(f"  撤单失败 {order['id']}: {e}")
    except Exception as e:
        logger.warning(f"  查询挂单失败：{e}")


BREAKEVEN_STATE_FILE = Path("logs/breakeven_state.json")
PARTIAL_PROFIT_STATE_FILE = Path("logs/partial_profit_state.json")
TRAILING_STOP_STATE_FILE = Path("logs/trailing_stop_state.json")


def _load_breakeven_state() -> dict:
    return atomic_read_json(BREAKEVEN_STATE_FILE, default={})


def _save_breakeven_state(state: dict):
    atomic_write_json(BREAKEVEN_STATE_FILE, state)


def _clear_breakeven_state(symbol: str, side: str):
    def update_fn(state: dict) -> dict:
        state.pop(f"{symbol}_{side}", None)
        return state
    atomic_update_json(BREAKEVEN_STATE_FILE, update_fn, default={})


def _load_partial_profit_state() -> dict:
    return atomic_read_json(PARTIAL_PROFIT_STATE_FILE, default={})


def _save_partial_profit_state(state: dict):
    atomic_write_json(PARTIAL_PROFIT_STATE_FILE, state)


def _mark_partial_profit_done(symbol: str, side: str, batch: int):
    """标记某批次止盈已执行，batch=1 或 2，使用原子更新"""
    def update_fn(state: dict) -> dict:
        key = f"{symbol}_{side}"
        if key not in state:
            state[key] = {"batch1": False, "batch2": False}
        state[key][f"batch{batch}"] = True
        return state
    atomic_update_json(PARTIAL_PROFIT_STATE_FILE, update_fn, default={})


def _is_partial_profit_done(symbol: str, side: str, batch: int) -> bool:
    """检查某批次止盈是否已执行"""
    state = _load_partial_profit_state()
    key = f"{symbol}_{side}"
    return state.get(key, {}).get(f"batch{batch}", False)


def _clear_partial_profit_state(symbol: str, side: str):
    def update_fn(state: dict) -> dict:
        state.pop(f"{symbol}_{side}", None)
        return state
    atomic_update_json(PARTIAL_PROFIT_STATE_FILE, update_fn, default={})


# ── 跟踪止损状态管理（优化2）────────────────────────────────────────────

def _mark_trailing_stop_active(symbol: str, side: str, stop_price: float):
    """激活跟踪止损，记录当前止损价"""
    def update_fn(state: dict) -> dict:
        state[f"{symbol}_{side}"] = {"active": True, "stop_price": stop_price}
        return state
    atomic_update_json(TRAILING_STOP_STATE_FILE, update_fn, default={})


def _is_trailing_stop_active(symbol: str, side: str) -> bool:
    state = atomic_read_json(TRAILING_STOP_STATE_FILE, default={})
    return state.get(f"{symbol}_{side}", {}).get("active", False)


def _get_trailing_stop_price(symbol: str, side: str) -> float:
    state = atomic_read_json(TRAILING_STOP_STATE_FILE, default={})
    return state.get(f"{symbol}_{side}", {}).get("stop_price", 0.0)


def _clear_trailing_stop_state(symbol: str, side: str):
    def update_fn(state: dict) -> dict:
        state.pop(f"{symbol}_{side}", None)
        return state
    atomic_update_json(TRAILING_STOP_STATE_FILE, update_fn, default={})


def _update_trailing_stop(
    exchange,
    symbol: str,
    side: str,
    contracts: float,
    current_price: float,
    atr: float,
    entry_price: float,
) -> bool:
    """
    更新跟踪止损单（优化2）：
    - 计算新止损价
    - 仅当新止损价比旧止损价更有利时才更新
    - 撤旧止损单，挂新止损单
    返回：True=实际更新了止损，False=无需更新
    """
    try:
        new_stop = calculate_trailing_stop(current_price, atr, side)
        old_stop = _get_trailing_stop_price(symbol, side)

        # 确保跟踪止损不低于保本位（多头）或不高于保本位（空头）
        if side == "long":
            new_stop = max(new_stop, entry_price)
            if old_stop > 0 and new_stop <= old_stop:
                logger.info(f"  跟踪止损无需更新：新止损{new_stop:.6g} <= 旧止损{old_stop:.6g}")
                return False
        else:
            new_stop = min(new_stop, entry_price)
            if old_stop > 0 and new_stop >= old_stop:
                logger.info(f"  跟踪止损无需更新：新止损{new_stop:.6g} >= 旧止损{old_stop:.6g}")
                return False

        # 撤旧止损单，挂新止损单
        _cancel_all_symbol_orders(exchange, symbol)
        sl_order = exchange.create_order(
            symbol=symbol,
            type="conditional",
            side="sell" if side == "long" else "buy",
            amount=contracts,
            price=None,
            params={
                "ordType": "conditional",
                "slTriggerPx": str(float(new_stop)),
                "slOrdPx": "-1",
                "reduceOnly": True,
                "tdMode": "cross",
            },
        )
        _mark_trailing_stop_active(symbol, side, new_stop)
        logger.info(
            f"  跟踪止损已更新：{old_stop:.6g} → {new_stop:.6g} | 订单ID：{sl_order.get('id')}"
        )
        return True
    except Exception as e:
        logger.error(f"  ⚠️ 更新跟踪止损失败：{e}")
        return False


def _move_stop_to_breakeven(exchange, symbol: str, side: str, contracts: float, entry_price: float) -> bool:
    """
    将止损移至保本位（入场价），若已在保本位则跳过
    返回：True=实际执行了移动，False=已跳过（无需操作）
    """
    try:
        # 1. 本地状态优先：已记录过保本则直接跳过，无需查询交易所
        state = _load_breakeven_state()
        state_key = f"{symbol}_{side}"
        saved = state.get(state_key)
        if saved and abs(saved - float(entry_price)) / float(entry_price) < 0.0001:
            logger.info(f"  止损已在保本位 {entry_price}（本地状态），跳过重复操作")
            return False

        # 2. 兜底：查交易所挂单，防止本地状态丢失后重复操作
        open_orders = exchange.fetch_open_orders(symbol, params={"instType": "SWAP"})
        for order in open_orders:
            sl_px = order.get("info", {}).get("slTriggerPx") or order.get("info", {}).get("stopLossPrice")
            if sl_px and abs(float(sl_px) - float(entry_price)) / float(entry_price) < 0.0001:
                logger.info(f"  止损已在保本位 {entry_price}（交易所确认），跳过重复操作")
                state[state_key] = float(entry_price)
                _save_breakeven_state(state)
                return False

        # 3. 撤销旧挂单并创建新止损单（与开仓时保持一致的参数格式）
        _cancel_all_symbol_orders(exchange, symbol)
        sl_order = exchange.create_order(
            symbol=symbol,
            type="conditional",
            side="sell" if side == "long" else "buy",
            amount=contracts,
            price=None,
            params={
                "ordType": "conditional",
                "slTriggerPx": str(float(entry_price)),
                "slOrdPx": "-1",  # -1 表示市价触发
                "reduceOnly": True,
                "tdMode": "cross",
            },
        )
        logger.info(f"  止损已移至保本位 {entry_price}（市价触发）| 订单 ID：{sl_order.get('id')}")
        state[state_key] = float(entry_price)
        _save_breakeven_state(state)
        return True
    except Exception as e:
        logger.error(f"  ⚠️ 移动止损至保本失败：{e}")
        return False


def main():
    """主执行流程"""
    logger.info("🚀 Trade Manager 启动")

    exchange = create_exchange()

    # ── 第一步：获取当前持仓 ──
    positions = get_open_positions(exchange)

    # 检测交易所自动止损/止盈（对比上次持仓快照），记录冷却
    detect_and_record_stop_loss(positions)
    save_position_snapshot(positions)

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

            # ── 2.2 盈亏状态判断 ──

            # 情况 A：浮盈在移动止损阈值和第一批止盈阈值之间，仅移止损至保本
            # 注：浮盈超过 PARTIAL_PROFIT_PCT 时，止盈逻辑（情况 B）内部会自行移止损，无需重复
            if TRAILING_STOP_PCT < pnl_pct < PARTIAL_PROFIT_PCT:
                logger.info(f"  浮盈{pnl_pct:.1f}%（>{TRAILING_STOP_PCT}%），检查是否需要移动止损至保本位")
                try:
                    moved = _move_stop_to_breakeven(exchange, symbol, side, contracts, entry_price)
                    if moved:  # 仅当真正移动时才发通知
                        send_notification(
                            f"✅ {symbol} 浮盈{pnl_pct:.1f}%，已将止损移至保本位 {entry_price}"
                        )
                except Exception as e:
                    logger.error(f"  ⚠️ 移动止损失败：{e}")

            # 情况 B：分两批部分止盈（使用实时持仓量，止盈后立即保本）
            # 第二批：浮盈 >= PARTIAL_PROFIT_PCT_2，平 PARTIAL_PROFIT_RATIO_2 仓位
            # 第一批：浮盈 >= PARTIAL_PROFIT_PCT，平 PARTIAL_PROFIT_RATIO_1 仓位
            if pnl_pct >= PARTIAL_PROFIT_PCT_2:
                if _is_partial_profit_done(symbol, side, 2):
                    logger.info(f"  第二批止盈已执行过，跳过")
                else:
                    live_positions = get_open_positions(exchange)
                    live_pos = next(
                        (p for p in live_positions if p["symbol"] == symbol and p["side"] == side), None
                    )
                    live_contracts = live_pos["contracts"] if live_pos else contracts
                    partial_contracts = int(live_contracts * PARTIAL_PROFIT_RATIO_2)
                    if partial_contracts > 0:
                        logger.info(f"  浮盈{pnl_pct:.1f}%（>{PARTIAL_PROFIT_PCT_2:.0f}%），第二批止盈{int(PARTIAL_PROFIT_RATIO_2*100)}%（{partial_contracts} 张）")
                        try:
                            exchange.create_order(
                                symbol=symbol,
                                type="market",
                                side="sell" if side == "long" else "buy",
                                amount=partial_contracts,
                                params={"tdMode": "cross", "reduceOnly": True},
                            )
                            _mark_partial_profit_done(symbol, side, 2)
                            send_notification(
                                f"{symbol} 浮盈{pnl_pct:.1f}%，第二批止盈{int(PARTIAL_PROFIT_RATIO_2*100)}%（{partial_contracts} 张），剩余持仓继续运行"
                            )
                            closed_count += 1
                            _move_stop_to_breakeven(exchange, symbol, side, live_contracts - partial_contracts, entry_price)
                        except Exception as e:
                            logger.error(f"  ⚠️ 第二批止盈失败：{e}")
            elif pnl_pct >= PARTIAL_PROFIT_PCT:
                if _is_partial_profit_done(symbol, side, 1):
                    logger.info(f"  第一批止盈已执行过，跳过")
                else:
                    live_positions = get_open_positions(exchange)
                    live_pos = next(
                        (p for p in live_positions if p["symbol"] == symbol and p["side"] == side), None
                    )
                    live_contracts = live_pos["contracts"] if live_pos else contracts
                    partial_contracts = int(live_contracts * PARTIAL_PROFIT_RATIO_1)
                    if partial_contracts > 0:
                        logger.info(f"  浮盈{pnl_pct:.1f}%（>{PARTIAL_PROFIT_PCT}%），第一批止盈{int(PARTIAL_PROFIT_RATIO_1*100)}%（{partial_contracts} 张）")
                        try:
                            exchange.create_order(
                                symbol=symbol,
                                type="market",
                                side="sell" if side == "long" else "buy",
                                amount=partial_contracts,
                                params={"tdMode": "cross", "reduceOnly": True},
                            )
                            _mark_partial_profit_done(symbol, side, 1)
                            send_notification(
                                f"{symbol} 浮盈{pnl_pct:.1f}%，第一批止盈{int(PARTIAL_PROFIT_RATIO_1*100)}%（{partial_contracts} 张），剩余仓位启用ATR跟踪止损"
                            )
                            closed_count += 1
                            remaining_contracts = live_contracts - partial_contracts
                            _move_stop_to_breakeven(exchange, symbol, side, remaining_contracts, entry_price)
                            # 激活跟踪止损（优化2）
                            _mark_trailing_stop_active(symbol, side, entry_price)
                            logger.info(f"  第一批止盈完成，剩余{remaining_contracts}张启用ATR跟踪止损")
                        except Exception as e:
                            logger.error(f"  ⚠️ 第一批止盈失败：{e}")

            # 情况 C：亏损超过阈值，强制平仓
            if pnl_pct < FORCE_CLOSE_PCT:
                logger.info(f"  亏损{pnl_pct:.1f}%（<{FORCE_CLOSE_PCT}%），触发动态止损")
                try:
                    reason = f"动态止损：亏损{pnl_pct:.1f}%"
                    close_position(exchange, symbol, reason=reason)
                    record_stop_loss_manual(symbol, reason)
                    send_notification(f"{symbol} 亏损{pnl_pct:.1f}%，触发动态止损，已强制平仓")
                    closed_count += 1
                    generate_close_report(symbol, reason, unrealized_pnl, pnl_pct)
                    _clear_breakeven_state(symbol, side)
                    _clear_partial_profit_state(symbol, side)
                    _clear_trailing_stop_state(symbol, side)
                except Exception as e:
                    logger.error(f"  强制平仓失败：{e}")

            # 情况 D：盈亏在正常范围内
            if FORCE_CLOSE_PCT <= pnl_pct <= TRAILING_STOP_PCT:
                logger.info(f"  持仓状态正常，盈亏{pnl_pct:.1f}%")

            # ── 2.3 趋势反转检测（纯价格结构，不调用 AI）──
            data = fetch_multi_timeframe(symbol, exchange=exchange)
            if STRUCTURE_TF in data and not data[STRUCTURE_TF].empty:
                structure_tf = detect_trend_structure(data[STRUCTURE_TF])

                current_price = float(data[STRUCTURE_TF].iloc[-1]["close"])

                # ── 跟踪止损更新（优化2）──────────────────────────────────
                if _is_trailing_stop_active(symbol, side):
                    # 跟踪止损用 15m ATR，避免 5m ATR 过小导致止损过紧
                    trailing_tf = "15m" if "15m" in data else (TIMEFRAMES[1] if len(TIMEFRAMES) > 1 else STRUCTURE_TF)
                    base_data = data.get(trailing_tf) or data.get(STRUCTURE_TF)
                    if base_data is not None and not base_data.empty:
                        from indicator_engine import compute_adx
                        adx_info = compute_adx(base_data)
                        atr = adx_info.get("atr", current_price * 0.01)
                        live_positions = get_open_positions(exchange)
                        live_pos = next(
                            (p for p in live_positions if p["symbol"] == symbol and p["side"] == side), None
                        )
                        live_contracts = live_pos["contracts"] if live_pos else contracts
                        updated = _update_trailing_stop(
                            exchange, symbol, side, live_contracts,
                            current_price, atr, entry_price
                        )
                        if updated:
                            send_notification(
                                f"{symbol} 跟踪止损已更新 → {_get_trailing_stop_price(symbol, side):.6g}"
                            )

                # ── 动量衰减出场（优化4）──────────────────────────────────
                if MOMENTUM_DECAY_ENABLED and pnl_pct >= MOMENTUM_DECAY_MIN_PCT:
                    base_tf = TIMEFRAMES[-1] if TIMEFRAMES else "15m"
                    base_data = data.get(base_tf) or data.get(STRUCTURE_TF)
                    if base_data is not None and not base_data.empty:
                        decay_result = detect_momentum_decay(base_data, side)
                        if decay_result.get("decaying"):
                            decay_reason = decay_result.get("reason", "动量衰减")
                            logger.warning(f"  {symbol} 动量衰减出场：{decay_reason}")
                            close_position(exchange, symbol, reason=decay_reason)
                            record_stop_loss_manual(symbol, decay_reason)
                            send_notification(
                                f"{symbol} 浮盈{pnl_pct:.1f}%，动量衰减主动出场\n原因：{decay_reason}"
                            )
                            closed_count += 1
                            generate_close_report(symbol, decay_reason, unrealized_pnl, pnl_pct)
                            _clear_breakeven_state(symbol, side)
                            _clear_partial_profit_state(symbol, side)
                            _clear_trailing_stop_state(symbol, side)
                            continue

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

                if structure_broken_tf := (
                    (side == "long" and structure_tf.get("structure_broken_long", False)) or
                    (side == "short" and structure_tf.get("structure_broken_short", False))
                ):
                    should_close = True
                    close_reason = f"{STRUCTURE_TF.upper()}结构破坏（方向:{side}），当前价：{current_price}"

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
                    record_stop_loss_manual(symbol, close_reason)  # 记录冷却
                    send_notification(f"{symbol} 结构平仓\n原因：{close_reason}")
                    closed_count += 1
                    generate_close_report(symbol, close_reason, unrealized_pnl, pnl_pct)
                    _clear_breakeven_state(symbol, side)
                    _clear_partial_profit_state(symbol, side)
                    _clear_trailing_stop_state(symbol, side)
                else:
                    logger.info(f"  {symbol} 结构完好，持仓继续 | 当前价：{current_price}")

            # 2.4 更新持仓日志
            _save_position_log(pos, pnl_pct)

        except Exception as e:
            logger.error(f"  ❌ 处理持仓异常：{e}")
            continue

    # ── 第三步：汇总报告 ──
    remaining = len(positions) - closed_count
    logger.info("=" * 20)
    logger.info(f"持仓巡检完成 | 持仓数：{remaining} | 净盈亏：{total_pnl:.2f} USDT")
    logger.info("=" * 20)

    # 构建持仓明细
    lines = []
    for pos in positions:
        sym = pos.get("symbol", "")
        # 合约简写：BTC/USDT:USDT → BTC
        short_name = sym.split("/")[0] if "/" in sym else sym
        side = pos.get("side", "")
        side_label = "🔴" if side == "long" else "🟢"
        contracts = pos.get("contracts", 0)
        entry = pos.get("entry_price", 0)
        liq = pos.get("liquidation_price", 0)
        pnl = pos.get("unrealized_pnl", 0)
        pnl_pct = pos.get("percentage", 0)
        pnl_sign = "+" if pnl >= 0 else ""
        liq_str = f"{liq:.4g}" if liq else "N/A"

        lines.append(
            f"{short_name} {side_label} {contracts} 张 | 开仓:{entry:.4g} | 强平:{liq_str} | {pnl_sign}{pnl:.2f}U ({pnl_sign}{pnl_pct:.1f}%)"
        )

    detail = "\n".join(lines)
    send_notification(
        f"持仓巡检完成\n"
        f"持仓数：{remaining} | 总浮盈亏：{total_pnl:+.2f}U | 已平仓：{closed_count}\n"
        f"{'─' * 20}\n"
        f"{detail}"
    )


def _save_position_log(position: dict, pnl_pct: float):
    """保存持仓状态到日志"""
    import json

    log_dir = Path("logs/trades")
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = now_cst_str()
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
