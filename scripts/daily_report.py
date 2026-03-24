#!/usr/bin/env python3
"""
daily_report.py
每日交易报告生成
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
    
    # ── 第一步：读取当日交易记录 ──
    trade_files = list(log_dir.glob(f"position_*{today}*.json"))
    decision_files = list(decisions_dir.glob(f"*{today}*.json"))
    
    logger.info(f"今日交易记录：{len(trade_files)} 个")
    logger.info(f"今日决策记录：{len(decision_files)} 个")
    
    trades = []
    for f in trade_files:
        try:
            with open(f, "r") as fp:
                trades.append(json.load(fp))
        except Exception as e:
            logger.warning(f"读取失败 {f}: {e}")
    
    decisions = []
    for f in decision_files:
        try:
            with open(f, "r") as fp:
                decisions.append(json.load(fp))
        except Exception as e:
            logger.warning(f"读取失败 {f}: {e}")
    
    # ── 第二步：统计交易数据 ──
    total = len(trade_files)
    wins = sum(1 for t in trades if t.get("unrealized_pnl", 0) > 0)
    losses = total - wins
    win_rate = (wins / total * 100) if total > 0 else 0
    
    total_pnl = sum(t.get("unrealized_pnl", 0) for t in trades)
    max_win = max((t.get("unrealized_pnl", 0) for t in trades), default=0)
    max_loss = min((t.get("unrealized_pnl", 0) for t in trades), default=0)
    
    # AI模型统计
    model_stats = defaultdict(lambda: {"count": 0, "wins": 0})
    for d in decisions:
        dec = d.get("decision", {})
        model = dec.get("_model_used", "unknown")
        signal = dec.get("signal", "wait")
        pnl = dec.get("unrealized_pnl", 0)

        model_stats[model]["count"] += 1
        if signal in ["long", "short"] and pnl > 0:
            model_stats[model]["wins"] += 1

    # 计算各模型准确率
    model_acc = {}
    for model, stats in model_stats.items():
        if stats["count"] > 0:
            model_acc[model] = stats["wins"] / stats["count"] * 100
        else:
            model_acc[model] = 0

    # 平均信号强度
    signal_strengths = [d.get("decision", {}).get("signal_strength", 0) for d in decisions if d.get("decision", {}).get("signal_strength")]
    avg_strength = sum(signal_strengths) / len(signal_strengths) if signal_strengths else 0
    
    # ── 第三步：获取账户状态 ──
    try:
        exchange = create_exchange()
        balance = exchange.fetch_balance()
        end_balance = float(balance.get("USDT", {}).get("total", 0))
    except Exception as e:
        logger.warning(f"余额获取失败：{e}")
        end_balance = 0
    
    # 假设期初余额（实际应从缓存读取）
    start_balance = end_balance - total_pnl
    pnl_pct = (total_pnl / start_balance * 100) if start_balance > 0 else 0
    
    # 当前持仓
    try:
        positions = get_open_positions(exchange)
    except:
        positions = []
    
    # ── 第四步：生成报告文本 ──
    report = f"""📊 每日交易报告 {today}
━━━━━━━━━━━━━━━━━
💼 账户状态
  期初余额：{start_balance:.2f} USDT
  期末余额：{end_balance:.2f} USDT
  当日盈亏：{total_pnl:+.2f} USDT（{pnl_pct:+.2f}%）

📈 交易统计
  总交易次数：{total}
  盈利/亏损：{wins}/{losses}
  胜率：{win_rate:.1f}%
  最大盈利：+{max_win:.2f} USDT
  最大亏损：{max_loss:.2f} USDT

🤖 AI模型表现"""
    
    for model, acc in model_acc.items():
        count = model_stats[model]["count"]
        report += f"\n  {model}：{count}次，准确率{acc:.1f}%"
    
    report += f"""
  平均信号强度：{avg_strength:.1f}/10

📌 今日持仓
"""
    
    if positions:
        for p in positions:
            symbol = p.get("symbol", "UNKNOWN")
            side = p.get("side", "")
            pnl = p.get("unrealized_pnl", 0)
            report += f"  {symbol} | {side} | 盈亏：{pnl:+.2f} USDT\n"
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
            "trades": trades,
            "decisions": decisions,
            "stats": {
                "total": total,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "max_win": max_win,
                "max_loss": max_loss,
                "start_balance": start_balance,
                "end_balance": end_balance,
                "model_stats": dict(model_stats),
                "model_acc": model_acc,
            }
        }, f, ensure_ascii=False, indent=2)
    
    logger.info(f"报告已保存：{report_txt}")
    
    # ── 第六步：发送通知 ──
    send_notification(report)
    
    logger.info("✅ Daily Report 完成")


if __name__ == "__main__":
    main()