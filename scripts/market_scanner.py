#!/usr/bin/env python3
"""
Market Scanner - 动态热门合约扫描交易系统
每15分钟执行一次：获取热门合约 -> AI分析 -> 风控 -> 执行交易
"""

import sys
import re
import logging
from datetime import datetime
from pathlib import Path

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

import ccxt  # noqa: F401 - 间接使用（create_exchange内部）
from dotenv import load_dotenv

from config_loader import check_env, RISK_CFG, SCANNER_CFG, TRADE_MGR_CFG, TRADING_CFG, ANALYSIS_CFG, CHART_CFG, BLACKLIST_CFG, TIMEFRAMES, setup_logging
check_env()
setup_logging("market_scanner")

MAX_POSITIONS       = RISK_CFG.get("max_open_positions", 3)
FORCE_CLOSE_PCT     = TRADE_MGR_CFG.get("force_close_loss_pct", -10.0)
TOP_N_SYMBOLS       = SCANNER_CFG.get("top_n_symbols", 30)
MIN_VOLUME_USDT     = SCANNER_CFG.get("min_volume_usdt", 50_000_000)
MAX_PRICE_USDT      = SCANNER_CFG.get("max_price_usdt", 2.0)
MIN_SIGNAL_STRENGTH = TRADING_CFG.get("min_signal_strength", 7)
MIN_RR_RATIO        = TRADING_CFG.get("min_rr_ratio", 2.0)
_ANALYSIS_MODE      = ANALYSIS_CFG.get("mode", "text")
_SAVE_CHART_IN_TEXT = CHART_CFG.get("save_in_text_mode", False)

# 导入各模块
from fetch_kline import (
    fetch_hot_symbols,
    _load_fallback_symbols,
    fetch_multi_timeframe,
    calculate_support_resistance,
)
from generate_chart import generate_multi_chart
from ai_analysis import analyze_symbol, save_decision_log, passes_risk_filter
from risk_filter import check_daily_loss
from execute_trade import (
    create_exchange,
    get_open_positions,
    check_position_health,
    close_position,
    execute_from_decision
)

load_dotenv()

# 配置日志：同时输出到控制台和文件
logger = logging.getLogger(__name__)


from notifier import send_notification


def main():
    """主执行流程"""
    start_time = datetime.now()
    logger.info("🚀 Market Scanner 启动")
    
    # ── 第一步：初始化 & 动态获取合约列表 ──
    logger.info("=" * 50)
    logger.info("第一步：初始化 & 获取合约列表")
    logger.info("=" * 50)
    
    exchange = create_exchange()
    balance_cache = {}
    
    # 获取热门合约
    symbols = fetch_hot_symbols(
        exchange,
        top_n=TOP_N_SYMBOLS,
        min_volume_usdt=MIN_VOLUME_USDT,
        max_price_usdt=MAX_PRICE_USDT,
    )
    if not symbols:
        logger.warning("热门合约列表为空，使用兜底列表")
        symbols = _load_fallback_symbols()

    logger.info(f"本轮扫描合约数量：{len(symbols)}")
    logger.info(f"前5个合约：{symbols[:5]}")
    # 注：日线趋势过滤已由 indicator_engine.rule_engine_filter 在逐品种分析时处理
    
    # ── 第二步：日亏损预检 ──
    logger.info("=" * 50)
    logger.info("第二步：日亏损预检")
    logger.info("=" * 50)
    
    if not check_daily_loss(exchange, balance_cache):
        send_notification("今日亏损超限，已停止自动交易")
        logger.warning("日亏损超限，终止执行")
        return
    
    # ── 第三步：持仓数量预检 ──
    logger.info("=" * 50)
    logger.info("第三步：持仓数量预检")
    logger.info("=" * 50)
    
    positions = get_open_positions(exchange)
    current_position_count = len(positions)
    
    if current_position_count >= MAX_POSITIONS:
        logger.warning(f"当前持仓已达上限（{current_position_count}/{MAX_POSITIONS}），跳过本轮扫描")
        send_notification(f"持仓已满{MAX_POSITIONS}个，等待现有持仓触及止盈/止损后再开新仓")
    else:
        logger.info(f"当前持仓：{current_position_count}/{MAX_POSITIONS}")
    
    # ── 第四步：持仓健康检查 ──
    logger.info("=" * 50)
    logger.info("第四步：持仓健康检查")
    logger.info("=" * 50)
    
    unhealthy = check_position_health(exchange, max_loss_pct=FORCE_CLOSE_PCT)
    if unhealthy:
        logger.warning(f"发现 {len(unhealthy)} 个超亏持仓，强制平仓")
        for pos in unhealthy:
            close_position(exchange, pos)
        send_notification(f"已强制平仓 {len(unhealthy)} 个超亏持仓")
    
    # ── 第五步：逐个扫描合约 ──
    logger.info("=" * 50)
    logger.info("第五步：扫描合约")
    logger.info("=" * 50)
    
    scanned = 0
    rule_filtered = 0   # 规则引擎拒绝的数量（横盘/趋势不对齐）
    triggered_trades = 0
    rule_passed_symbols = []   # [(symbol, direction, signal_strength)]
    risk_failed_symbols = []   # [(symbol, failed_checks)]

    for idx, symbol in enumerate(symbols):
        # 黑名单检查（双重保险）
        base_name = symbol.split("/")[0]
        if BLACKLIST_CFG and (symbol in BLACKLIST_CFG or base_name in BLACKLIST_CFG):
            logger.info(f"⏭️  跳过黑名单合约：{symbol}")
            continue
        
        # 每次扫描前检查持仓数量和是否已持有该合约
        positions = get_open_positions(exchange)
        current_position_count = len(positions)
        
        if current_position_count >= MAX_POSITIONS:
            logger.warning(f"持仓已达上限（{current_position_count}/{MAX_POSITIONS}），终止本轮剩余品种扫描")
            break
        
        # 检查是否已持有该合约（避免重复开仓）
        symbol_base = symbol.split("/")[0]  # 如 "ASTER1"
        already_held = False
        for pos in positions:
            pos_symbol = pos.get("symbol", "")
            pos_base = pos_symbol.split("/")[0]
            if pos_base == symbol_base:
                logger.info(f"⏭️  已持有 {symbol}（持仓：{pos.get('contracts', 0)} 张 @ {pos.get('entry_price', 0)}），跳过开仓")
                already_held = True
                break
        if already_held:
            continue

        logger.info(f"\n--- 扫描 [{idx+1}/{len(symbols)}] {symbol} ---")
        scanned += 1

        try:
            # 5.1 获取多周期K线数据
            data = fetch_multi_timeframe(symbol, exchange=exchange)
            anchor_tf = TIMEFRAMES[0]  # 最高周期作为数据有效性校验
            if data[anchor_tf].empty:
                logger.warning(f"{symbol} 数据获取失败，跳过")
                continue

            # 计算支撑阻力（使用最高周期作为锚）
            anchor_tf = TIMEFRAMES[0]
            support, resistance = calculate_support_resistance(data[anchor_tf])

            # 5.2 生成图表（visual模式必须；text模式按配置决定是否存档）
            if _ANALYSIS_MODE == "visual" or _SAVE_CHART_IN_TEXT:
                chart_paths = generate_multi_chart(
                    multi_tf_data=data,
                    symbol=symbol,
                    support_levels=support,
                    resistance_levels=resistance
                )
            else:
                chart_paths = []

            # 5.3 统一分析入口（text模式=规则引擎+文本LLM；visual模式=视觉LLM）
            decision = analyze_symbol(
                multi_tf_data=data,
                symbol=symbol,
                support_levels=support,
                resistance_levels=resistance,
                image_paths=chart_paths,
            )
            logger.info(f"{symbol} 分析结果: signal={decision.get('signal')}, "
                        f"confidence={decision.get('confidence')}, "
                        f"strength={decision.get('signal_strength')}, "
                        f"mode={decision.get('_analysis_mode', 'visual')}")

            # 规则引擎拒绝的直接跳过，不再走风控
            if decision.get("_analysis_mode") == "rule_filter_rejected":
                rule_filtered += 1
                filter_reason = decision.get("_filter_reason", "未知原因")
                logger.info(f"{symbol} 规则过滤未通过: {filter_reason}")
                continue

            # 记录规则通过的合约及核心指标
            # 优先用规则引擎判断的方向（_rule_direction），LLM 可能返回 wait 覆盖掉
            direction = decision.get("_rule_direction") or decision.get("signal", "wait")
            signal_strength = decision.get("signal_strength", 0)

            # 提取核心指标信息（从 reason 字段解析 RSI、ADX）
            reason = decision.get("reason", "")
            rsi_val = None
            adx_val = None

            # 尝试从 reason 解析 RSI 和 ADX（rule_only 模式）
            rsi_match = re.search(r'RSI[=:]\s*(\d+\.?\d*)', reason)
            adx_match = re.search(r'ADX[=:]\s*(\d+\.?\d*)', reason)
            if rsi_match:
                rsi_val = float(rsi_match.group(1))
            if adx_match:
                adx_val = float(adx_match.group(1))

            # 如果 reason 里没有，尝试从 _anchor_adx 获取（text 模式）
            if adx_val is None and "_anchor_adx" in decision:
                adx_val = decision.get("_anchor_adx")

            indicators = {
                "strength": signal_strength,
                "rsi": rsi_val,
                "adx": adx_val,
                "volume_note": decision.get("volume_note", ""),
                "rr": decision.get("risk_reward", ""),
            }
            rule_passed_symbols.append((symbol, direction, indicators))

            risk_checks = {
                "信号方向明确": decision.get("signal") in ["long", "short"],
                "置信度为high": decision.get("confidence") == "high",
                f"信号强度>={MIN_SIGNAL_STRENGTH}": decision.get("signal_strength", 0) >= MIN_SIGNAL_STRENGTH,
                "成交量确认": decision.get("volume_confirmed", False) is True,
                "无背离风险": decision.get("divergence_risk", True) is False,
            }
            failed_checks = [k for k, v in risk_checks.items() if not v]

            logger.info(f"{symbol} 风控检查: {risk_checks}")

            if not passes_risk_filter(decision):
                logger.warning(f"{symbol} 风控未通过 | 失败项: {failed_checks} | signal={decision.get('signal')}, confidence={decision.get('confidence')}")
                # 记录风控失败的合约及原因
                risk_failed_symbols.append((symbol, direction, failed_checks))
                continue
            
            logger.info(f"{symbol} 风控通过 ✅")
            
            # 5.6 执行交易
            logger.info(f"{symbol} 准备执行交易: {decision.get('signal')} @ {decision.get('entry_price')}")
            result = execute_from_decision(exchange, symbol, decision)
            if result.get("status") == "success":
                triggered_trades += 1
                logger.info(f"{symbol} ✅ 开仓成功！方向: {decision.get('signal')}, 入场: {decision.get('entry_price')}, 止损: {decision.get('stop_loss')}, 止盈: {decision.get('take_profit')}")
                send_notification(
                    f"开仓成功：{symbol}\n"
                    f"方向：{decision.get('signal')}\n"
                    f"入场：{decision.get('entry_price')}\n"
                    f"止损：{decision.get('stop_loss')}\n"
                    f"止盈：{decision.get('take_profit')}\n"
                    f"持仓：{current_position_count + 1}/{MAX_POSITIONS}"
                )
            elif result.get("status") in ("error", "failed"):
                logger.warning(f"{symbol} ❌ 开仓失败：{result.get('reason')}")
                send_notification(
                    f"开仓失败：{symbol}\n原因：{result.get('reason', '未知错误')}"
                )
            
            # 5.7 保存决策日志
            save_decision_log(
                symbol=symbol,
                timeframe="multi",
                decision=decision,
                image_paths=chart_paths
            )
            
        except Exception as e:
            logger.error(f"{symbol} 扫描异常：{e}")
            continue
    
    # ── 第六步：扫描完成 ──
    logger.info("=" * 50)
    logger.info("第六步：扫描完成")
    logger.info("=" * 50)
    
    positions = get_open_positions(exchange)
    final_position_count = len(positions)
    
    summary = (
        f"扫描完成：{scanned}/{len(symbols)} | "
        f"规则过滤：{rule_filtered} | "
        f"触发交易：{triggered_trades} | "
        f"当前持仓：{final_position_count}/{MAX_POSITIONS}"
    )
    logger.info(summary)

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"总耗时：{elapsed:.1f}秒")

    # 构建详细通知
    lines = [summary]

    if rule_passed_symbols:
        lines.append(f"\n📋 规则通过【{len(rule_passed_symbols)}】：")
        for sym, direction, indicators in rule_passed_symbols:
            # 简化合约名称：BTC/USDT:USDT -> BTC
            short_name = sym.split("/")[0]
            arrow = "🔴多" if direction == "long" else ("🟢空" if direction == "short" else "⚪观望")

            # 构建指标信息
            parts = [f"{short_name} {arrow}"]
            parts.append(f"强度{indicators['strength']}")

            if indicators.get('rsi') is not None:
                parts.append(f"RSI{indicators['rsi']:.0f}")
            if indicators.get('adx') is not None:
                parts.append(f"ADX{indicators['adx']:.0f}")
            if indicators.get('volume_note'):
                parts.append(indicators['volume_note'])
            if indicators.get('rr'):
                parts.append(f"R:R={indicators['rr']}")

            lines.append(f"  {' '.join(parts)}")

    if risk_failed_symbols:
        lines.append(f"\n⚠️ 风控拒绝【{len(risk_failed_symbols)}】：")
        for sym, direction, failed in risk_failed_symbols:
            short_name = sym.split("/")[0]
            # 将正向检查项名称转换为负向描述
            label_map = {
                "信号方向明确": "方向不明",
                "置信度为high": "置信不足",
                "成交量确认": "量能不足",
                "无背离风险": "背离风险",
                "结构未打破": "结构已破",
            }
            simplified_failed = []
            for reason in failed:
                if "信号强度" in reason:
                    simplified_failed.append("信号强度不足")
                elif "风险回报比" in reason:
                    simplified_failed.append("R:R不足")
                else:
                    simplified_failed.append(label_map.get(reason, reason))
            reason_str = "、".join(simplified_failed)
            lines.append(f"  {short_name}（{reason_str}）")

    send_notification("\n".join(lines))


if __name__ == "__main__":
    main()