#!/usr/bin/env python3
"""
回测进度通知脚本
读取进度文件并发送飞书通知
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

LOG_DIR = Path("/root/.openclaw/workspace/crypto-trend-trader/logs")
PROGRESS_FILE = LOG_DIR / "backtest_feb_progress.json"
NOTIFICATION_FILE = LOG_DIR / "backtest_notification.txt"

def get_latest_log():
    """获取最新的日志文件"""
    logs = sorted(LOG_DIR.glob("backtest_feb_*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    return logs[0] if logs else None

def extract_stats(log_file):
    """从日志中提取统计信息"""
    stats = {}
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
            # 提取关键指标
            import re
            
            pnl_match = re.search(r'净收益.*?[-\d.]+\s*USDT|净盈亏.*?[-\d.]+\s*USDT', content)
            if pnl_match:
                stats['pnl'] = pnl_match.group()
            
            winrate_match = re.search(r'胜率.*?\d+\.?\d*\s*%', content)
            if winrate_match:
                stats['winrate'] = winrate_match.group()
            
            trades_match = re.search(r'总交易.*?\d+', content)
            if trades_match:
                stats['trades'] = trades_match.group()
            
            mdd_match = re.search(r'最大回撤.*?\d+\.?\d*\s*%', content)
            if mdd_match:
                stats['mdd'] = mdd_match.group()
            
            balance_match = re.search(r'最终余额.*?\d+\.?\d*\s*USDT|最终资金.*?\d+\.?\d*\s*USDT', content)
            if balance_match:
                stats['balance'] = balance_match.group()
    except Exception as e:
        print(f"提取统计信息失败：{e}")
    
    return stats

def send_notification(message):
    """发送通知 (写入文件供主程序读取)"""
    with open(NOTIFICATION_FILE, 'w', encoding='utf-8') as f:
        f.write(message)
    print(f"通知已写入：{NOTIFICATION_FILE}")
    return True

def main():
    if not PROGRESS_FILE.exists():
        print("进度文件不存在")
        return
    
    # 读取进度
    with open(PROGRESS_FILE, 'r') as f:
        progress = json.load(f)
    
    status = progress.get('status', 'unknown')
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    print(f"[{timestamp}] 状态：{status}")
    
    if status == 'running':
        # 获取日志信息
        log_file = get_latest_log()
        log_size = "N/A"
        log_lines = 0
        
        if log_file and log_file.exists():
            log_size = f"{log_file.stat().st_size / 1024 / 1024:.1f} MB"
            with open(log_file, 'r', encoding='utf-8') as f:
                log_lines = sum(1 for _ in f)
        
        # 估算进度
        estimated_progress = min(100, int(log_lines * 100 / 2000000))
        
        message = f"""🔄 2 月回测进行中

📈 进度
━━━━━━━━━━━━━━━━
估算进度：{estimated_progress}%
日志大小：{log_size}
日志行数：{log_lines:,}

📝 最新动态
检查日志文件获取最新信息...

下次检测：10 分钟后"""
        
        send_notification(message)
        print(f"估算进度：{estimated_progress}%")
        
    elif status == 'completed':
        log_file = get_latest_log()
        stats = extract_stats(log_file) if log_file else {}
        
        # 获取结果目录
        results_dir = Path("/root/.openclaw/workspace/crypto-trend-trader/backtest/results")
        latest_result = max(results_dir.glob("*/"), key=lambda x: x.stat().st_mtime, default=None)
        result_name = latest_result.name if latest_result else "N/A"
        
        message = f"""🎉 2 月回测完成

📊 核心指标
━━━━━━━━━━━━━━━━
总交易：{stats.get('trades', 'N/A')}
净盈亏：{stats.get('pnl', 'N/A')}
胜率：{stats.get('winrate', 'N/A')}
最大回撤：{stats.get('mdd', 'N/A')}
最终余额：{stats.get('balance', 'N/A')}

📁 结果目录：{result_name}

详细分析报告生成中..."""
        
        send_notification(message)
        print("完成通知已发送")
        
    elif status == 'failed':
        fail_msg = progress.get('message', '未知错误')
        
        message = f"""⚠️ 2 月回测失败

错误信息：{fail_msg}

请检查日志文件排查问题。"""
        
        send_notification(message)
        print("失败通知已发送")
    
    else:
        print(f"未知状态：{status}")

if __name__ == '__main__':
    main()
