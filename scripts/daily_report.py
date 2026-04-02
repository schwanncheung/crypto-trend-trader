#!/usr/bin/env python3
"""
daily_report.py
每日交易报告生成 - 基于真实交易记录和账户余额变化
"""

import sys
import json
import logging
from datetime import datetime, timezone
from config_loader import now_cst_str
from pathlib import Path
from collections import defaultdict

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

from config_loader import check_env
check_env()

from execute_trade import create_exchange, get_open_positions
from notifier import send_notification


def main():
    """主执行流程"""
    logger.info("🚀 Daily Report 启动")

    today = now_cst_str("%Y%m%d")
    log_dir = Path("logs/trades")
    decisions_dir = Path("logs/decisions")

    # ── 第一步：读取当日真实交易记录（开仓/平仓）──
    # 排除 position_*.json（持仓快照），只统计真实交易
    all_files = list(log_dir.glob(f"*{today}*.json"))
    trade_files = [f for f in all_files if not f.name.startswith("position_")]
    decision_files = list(decisions_dir.glob(f"*{today}*.json"))

    logger.info(f"今日交易记录：{len(trade_files)} 个")
    logger.info(f"今日决策记录：{len(decision_files)} 个")

    # 读取交易记录
    trades = []
    for f in trade_files:
        try:
            with open(f, "r") as fp:
                trade = json.load(fp)
                trades.append(trade)
        except Exception as e:
            logger.warning(f"读取失败 {f}: {e}")

    # 读取决策记录
    decisions = []
    for f in decision_files:
        try:
            with open(f, "r") as fp:
                decisions.append(json.load(fp))
        except Exception as e:
            logger.warning(f"读取失败 {f}: {e}")

    # ── 第二步：统计交易数据 ──
    open_trades = [t for t in trades if t.get("type") == "open"]
    close_trades = [t for t in trades if t.get("type") == "close"]

    total_opens = len(open_trades)
    total_closes = len(close_trades)

    # 已实现盈亏（仅统计平仓交易）
    realized_pnl = 0.0
    wins = 0
    losses = 0
    max_win = 0.0
    max_loss = 0.0

    for close_trade in close_trades:
        # 从平仓记录中提取盈亏（如果有的话）
        pnl = 0.0
        orders = close_trade.get("orders", [])
        for order in orders:
            order_pnl = order.get("realized_pnl", 0) or order.get("pnl", 0)
            pnl += float(order_pnl) if order_pnl else 0

        realized_pnl += pnl
        if pnl > 0:
            wins += 1
            max_win = max(max_win, pnl)
        elif pnl < 0:
            losses += 1
            max_loss = min(max_loss, pnl)

    win_rate = (wins / total_closes * 100) if total_closes > 0 else 0

    # AI模型统计（基于决策记录）
    model_stats = defaultdict(lambda: {"count": 0, "signals": 0})
    for d in decisions:
        dec = d.get("decision", {})
        model = dec.get("_model_used", "rule_only")
        signal = dec.get("signal", "wait")

        model_stats[model]["count"] += 1
        if signal in ["long", "short"]:
            model_stats[model]["signals"] += 1

    # 平均信号强度
    signal_strengths = [
        d.get("decision", {}).get("signal_strength", 0)
        for d in decisions
        if d.get("decision", {}).get("signal_strength")
    ]
    avg_strength = sum(signal_strengths) / len(signal_strengths) if signal_strengths else 0

    # ── 第三步：获取账户状态 ──
    try:
        exchange = create_exchange()
        balance = exchange.fetch_balance()
        end_balance = float(balance.get("USDT", {}).get("total", 0))
    except Exception as e:
        logger.warning(f"余额获取失败：{e}")
        end_balance = 0

    # 计算期初余额（期末 - 已实现盈亏）
    start_balance = end_balance - realized_pnl
    pnl_pct = (realized_pnl / start_balance * 100) if start_balance > 0 else 0

    # 当前持仓
    try:
        positions = get_open_positions(exchange)
    except:
        positions = []

    # 未实现盈亏（当前持仓）
    unrealized_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)

    # ── 第四步：生成报告文本 ──
    report = f"""📊 每日交易报告 {today}
━━━━━━━━━━━━━━━━━
💼 账户状态
  期初余额：{start_balance:.2f} USDT
  期末余额：{end_balance:.2f} USDT
  已实现盈亏：{realized_pnl:+.2f} USDT（{pnl_pct:+.2f}%）
  未实现盈亏：{unrealized_pnl:+.2f} USDT

📈 交易统计
  开仓次数：{total_opens}
  平仓次数：{total_closes}
  盈利/亏损：{wins}/{losses}
  胜率：{win_rate:.1f}%
  最大盈利：{max_win:+.2f} USDT
  最大亏损：{max_loss:.2f} USDT

🤖 AI 分析统计
  决策次数：{len(decisions)}"""

    if model_stats:
        for model, stats in model_stats.items():
            count = stats["count"]
            signals = stats["signals"]
            signal_rate = (signals / count * 100) if count > 0 else 0
            report += f"\n  {model}：{count}次分析，{signals}次信号（{signal_rate:.1f}%）"

    report += f"\n  平均信号强度：{avg_strength:.1f}/10"

    report += f"\n\n📌 当前持仓（{len(positions)}个）\n"

    if positions:
        for p in positions:
            symbol = p.get("symbol", "UNKNOWN").split("/")[0]  # 简化显示
            side = "🔴多" if p.get("side") == "long" else "🟢空"
            pnl = p.get("unrealized_pnl", 0)
            pnl_pct = p.get("percentage", 0)
            report += f"  {symbol} {side} | {pnl:+.2f} USDT（{pnl_pct:+.1f}%）\n"
    else:
        report += "  无持仓\n"

    report += "━━━━━━━━━━━━━━━━━"

    # ── 第五步：保存报告 ──
    report_dir = Path("logs/reports")
    report_dir.mkdir(exist_ok=True)
    report_txt = report_dir / f"daily_report_{today}.txt"
    with open(report_txt, "w", encoding="utf-8") as f:
        f.write(report)

    report_json = report_dir / f"daily_report_{today}.json"
    with open(report_json, "w", encoding="utf-8") as f:
        json.dump({
            "date": today,
            "account": {
                "start_balance": start_balance,
                "end_balance": end_balance,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "pnl_pct": pnl_pct,
            },
            "trades": {
                "total_opens": total_opens,
                "total_closes": total_closes,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "max_win": max_win,
                "max_loss": max_loss,
            },
            "analysis": {
                "total_decisions": len(decisions),
                "model_stats": {k: dict(v) for k, v in model_stats.items()},
                "avg_signal_strength": avg_strength,
            },
            "positions": [
                {
                    "symbol": p.get("symbol"),
                    "side": p.get("side"),
                    "unrealized_pnl": p.get("unrealized_pnl"),
                    "percentage": p.get("percentage"),
                }
                for p in positions
            ],
        }, f, ensure_ascii=False, indent=2)

    logger.info(f"报告已保存：{report_txt}")

    # ── 第六步：发送通知 ──
    send_notification(report)

    logger.info("✅ Daily Report 完成")


if __name__ == "__main__":
    main()
