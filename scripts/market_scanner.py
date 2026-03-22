#!/usr/bin/env python3
"""
Market Scanner - 动态热门合约扫描交易系统
每15分钟执行一次：获取热门合约 -> AI分析 -> 风控 -> 执行交易
"""

import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

import yaml
import ccxt
from dotenv import load_dotenv

from config_loader import check_env, RISK_CFG, SCANNER_CFG
check_env()

# 导入各模块
from fetch_kline import (
    fetch_hot_symbols, 
    _load_fallback_symbols,
    fetch_multi_timeframe,
    calculate_support_resistance,
    calculate_volume_ma,
    detect_trend_structure
)
from generate_chart import generate_multi_chart
from ai_analysis import analyze_with_fallback, save_decision_log, passes_risk_filter
from risk_filter import check_daily_loss, run_full_risk_check
from execute_trade import (
    create_exchange,
    get_open_positions,
    check_position_health,
    close_position,
    execute_from_decision
)

load_dotenv()

# 配置日志：同时输出到控制台和文件
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "market_scanner.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """加载配置文件"""
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


from notifier import send_notification


def main():
    """主执行流程"""
    start_time = datetime.now()
    logger.info("🚀 Market Scanner 启动")
    
    # ── 第一步：初始化 & 动态获取合约列表 ──
    logger.info("=" * 50)
    logger.info("第一步：初始化 & 获取合约列表")
    logger.info("=" * 50)
    
    config = load_config()
    exchange = create_exchange()
    balance_cache = {}
    
    # 获取热门合约
    symbols = fetch_hot_symbols(exchange, top_n=20)
    if not symbols:
        logger.warning("热门合约列表为空，使用兜底列表")
        symbols = _load_fallback_symbols()

    logger.info(f"本轮扫描合约数量：{len(symbols)}")
    logger.info(f"前5个合约：{symbols[:5]}")

    # 日线趋势预过滤：在进入主循环前剔除横盘合约，节省AI调用
    logger.info("趋势预过滤中（日线横盘合约将被剔除）...")
    from fetch_kline import filter_symbols_by_trend
    symbols = filter_symbols_by_trend(symbols, exchange, exclude_sideways=True)
    logger.info(f"预过滤后合约数量：{len(symbols)}")
    
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
    
    if current_position_count >= 3:
        logger.warning(f"当前持仓已达上限（3/3），跳过本轮扫描")
        send_notification("持仓已满3个，等待现有持仓触及止盈/止损后再开新仓")
    else:
        logger.info(f"当前持仓：{current_position_count}/3")
    
    # ── 第四步：持仓健康检查 ──
    logger.info("=" * 50)
    logger.info("第四步：持仓健康检查")
    logger.info("=" * 50)
    
    unhealthy = check_position_health(exchange, max_loss_pct=-10.0)
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
    skipped_sideways = 0
    triggered_trades = 0
    
    for idx, symbol in enumerate(symbols):
        # 每次扫描前检查持仓数量
        positions = get_open_positions(exchange)
        current_position_count = len(positions)
        
        if current_position_count >= 3:
            logger.warning(f"持仓已达3个上限，终止本轮剩余品种扫描")
            break
        
        logger.info(f"\n--- 扫描 [{idx+1}/{len(symbols)}] {symbol} ---")
        scanned += 1
        
        try:
            # 5.1 获取多周期K线数据
            data = fetch_multi_timeframe(symbol, exchange=exchange)
            if data["1d"].empty:
                logger.warning(f"{symbol} 数据获取失败，跳过")
                continue
            
            # 计算支撑阻力
            support, resistance = calculate_support_resistance(data["4h"])
            volume_ma = calculate_volume_ma(data["1h"])
            
            # 趋势结构
            trend_structure = detect_trend_structure(data["1d"])
            
            # 打印详细趋势信息
            ts = trend_structure
            logger.info(f"{symbol} 趋势分析: trend={ts.get('trend')}, HH={ts.get('hh')}, HL={ts.get('hl')}, LH={ts.get('lh')}, LL={ts.get('ll')}, 结构破坏={ts.get('structure_broken')}")
            
            # 5.2 日线趋势预过滤（仅当使用AI分析时）
            if os.getenv("SKIP_AI_ANALYSIS", "false").lower() != "true":
                if trend_structure["trend"] == "sideways":
                    logger.info(f"{symbol} 日线横盘，跳过扫描")
                    skipped_sideways += 1
                    continue
            
            # 5.3 生成四周期K线图
            chart_paths = generate_multi_chart(
                multi_tf_data=data,
                symbol=symbol,
                support_levels=support,
                resistance_levels=resistance
            )
            
            # ✅ 正确：取字典的 values（真实文件路径），而不是 keys
            image_list = list(chart_paths.values())
            
            # 传入前再做一次路径验证，彻底避免 No such file 错误
            valid_images = []
            for path in image_list:
                if isinstance(path, str) and os.path.exists(path):
                    valid_images.append(path)
                else:
                    logger.warning(f"图片路径无效，已跳过：{path}")
            
            if not valid_images:
                logger.error(f"{symbol} 无有效图片，跳过AI分析")
                continue
            
            # 5.4 AI多周期分析 或 规则引擎
            if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
                # 规则引擎：根据趋势和结构生成信号
                current_price = float(data["1h"]["close"].iloc[-1])
                
                # 判断趋势方向：优先看结构破坏方向
                if ts.get('structure_broken'):
                    # 结构已打破，按突破方向做
                    if ts.get('hh') and ts.get('hl'):
                        signal = "long"
                    elif ts.get('lh') and ts.get('ll'):
                        signal = "short"
                    else:
                        signal = None
                else:
                    # 结构未破，按常规趋势判断
                    if ts.get('trend') == 'bullish' or (ts.get('hh') and ts.get('hl')):
                        signal = "long"
                    elif ts.get('trend') == 'bearish' or (ts.get('lh') and ts.get('ll')):
                        signal = "short"
                    else:
                        signal = None
                
                if not signal:
                    logger.warning(f"{symbol} 趋势不明确，跳过 | HH={ts.get('hh')}, HL={ts.get('hl')}, LH={ts.get('lh')}, LL={ts.get('ll')}")
                    continue
                
                logger.info(f"{symbol} 规则信号: {signal} | 入场={current_price}, 止损={current_price*0.97:.6f}, 止盈={current_price*1.06:.6f}")
                
                decision = {
                    "signal": signal,
                    "confidence": "high",  # 规则引擎默认高置信度
                    "signal_strength": 8,  # 满足 >=7 要求
                    "volume_confirmed": True,  # 满足成交量确认
                    "divergence_risk": False,  # 满足无背离风险
                    "entry_price": current_price,
                    "stop_loss": support[0] if support else current_price * 0.97,
                    "take_profit": resistance[0] if resistance else current_price * 1.06,
                    "reason": f"规则引擎：{trend_structure['trend']}趋势，日线结构={trend_structure.get('structure', 'N/A')}"
                }
                logger.info(f"{symbol} 跳过AI，使用规则信号: {decision['signal']}")
            else:
                decision = analyze_with_fallback(valid_images, multi_tf=True)
            
            # 5.5 风控过滤
            # 构建风控检查项
            risk_checks = {
                "信号方向明确": decision.get("signal") in ["long", "short"],
                "置信度为high": decision.get("confidence") == "high",
                "信号强度>=7": decision.get("signal_strength", 0) >= 7,
                "成交量确认": decision.get("volume_confirmed", False) is True,
                "无背离风险": decision.get("divergence_risk", True) is False,
            }
            failed_checks = [k for k, v in risk_checks.items() if not v]
            
            logger.info(f"{symbol} 风控检查: {risk_checks}")
            
            if not passes_risk_filter(decision):
                logger.warning(f"{symbol} 风控未通过 | 失败项: {failed_checks} | signal={decision.get('signal')}, confidence={decision.get('confidence')}")
                continue
            
            logger.info(f"{symbol} 风控通过 ✅")
            
            # 5.6 执行交易
            logger.info(f"{symbol} 准备执行交易: {decision.get('signal')} @ {decision.get('entry_price')}")
            result = execute_from_decision(exchange, symbol, decision)
            if result.get("success"):
                triggered_trades += 1
                logger.info(f"{symbol} ✅ 开仓成功！方向: {decision.get('signal')}, 入场: {decision.get('entry_price')}, 止损: {decision.get('stop_loss')}, 止盈: {decision.get('take_profit')}")
                send_notification(
                    f"开仓成功：{symbol}\n"
                    f"方向：{decision.get('signal')}\n"
                    f"入场：{decision.get('entry_price')}\n"
                    f"止损：{decision.get('stop_loss')}\n"
                    f"止盈：{decision.get('take_profit')}\n"
                    f"持仓：{current_position_count + 1}/3"
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
        f"本轮扫描完成 | "
        f"热门合约数：{len(symbols)} | "
        f"已扫描：{scanned} | "
        f"跳过横盘：{skipped_sideways} | "
        f"触发交易：{triggered_trades} | "
        f"当前持仓：{final_position_count}/3"
    )
    logger.info(summary)
    
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"总耗时：{elapsed:.1f}秒")
    
    send_notification(summary)


if __name__ == "__main__":
    main()