#!/bin/bash
# 2 月回测进度监控脚本 (每 10 分钟检测一次，主动飞书通知)

LOG_DIR="/root/.openclaw/workspace/crypto-trend-trader/logs"
PROGRESS_FILE="$LOG_DIR/backtest_feb_progress.json"
NOTIFIED_FILE="$LOG_DIR/backtest_feb_notified.flag"
SESSION_FILE="$LOG_DIR/backtest_session.key"

# 检查是否已通知过（完成通知）
if [ -f "$NOTIFIED_FILE" ]; then
    echo "已通知过，退出监控"
    exit 0
fi

# 查找最新的进度文件
LATEST_PROGRESS=$(ls -t "$LOG_DIR"/backtest_feb_progress.json 2>/dev/null | head -1)

if [ -z "$LATEST_PROGRESS" ] || [ ! -f "$LATEST_PROGRESS" ]; then
    echo "未找到进度文件，任务可能未启动"
    exit 0
fi

# 读取状态
STATUS=$(cat "$LATEST_PROGRESS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" 2>/dev/null)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] 检测时间：$TIMESTAMP"
echo "[$TIMESTAMP] 任务状态：$STATUS"

# 查找最新的日志文件
LATEST_LOG=$(ls -t "$LOG_DIR"/backtest_feb_*.log 2>/dev/null | grep -v "monitor" | head -1)

# 读取上次检测的日志行数（用于检测进展）
LAST_LINE_FILE="$LOG_DIR/backtest_last_line.txt"
if [ -f "$LAST_LINE_FILE" ]; then
    LAST_LINE=$(cat "$LAST_LINE_FILE")
else
    LAST_LINE=0
fi

# 获取当前日志行数
if [ -n "$LATEST_LOG" ] && [ -f "$LATEST_LOG" ]; then
    CURRENT_LINE=$(wc -l < "$LATEST_LOG")
    NEW_LINES=$((CURRENT_LINE - LAST_LINE))
    echo "$CURRENT_LINE" > "$LAST_LINE_FILE"
else
    CURRENT_LINE=0
    NEW_LINES=0
fi

# 计算日志大小
if [ -n "$LATEST_LOG" ] && [ -f "$LATEST_LOG" ]; then
    LOG_SIZE=$(du -h "$LATEST_LOG" | cut -f1)
else
    LOG_SIZE="N/A"
fi

# 估算进度 (基于日志行数，假设 200 万行完成)
ESTIMATED_PROGRESS=$((CURRENT_LINE * 100 / 2000000))
if [ $ESTIMATED_PROGRESS -gt 100 ]; then
    ESTIMATED_PROGRESS=100
fi

echo "[$TIMESTAMP] 日志行数：$CURRENT_LINE (新增：$NEW_LINES 行)"
echo "[$TIMESTAMP] 日志大小：$LOG_SIZE"
echo "[$TIMESTAMP] 估算进度：${ESTIMATED_PROGRESS}%"

# 构建通知消息
if [ "$STATUS" = "completed" ]; then
    echo "[$TIMESTAMP] 回测已完成，发送通知..."
    
    if [ -n "$LATEST_LOG" ] && [ -f "$LATEST_LOG" ]; then
        # 提取关键结果
        PNL=$(grep -E "净收益|净盈亏" "$LATEST_LOG" | tail -1 | sed 's/.*[:：]//' | xargs)
        WINRATE=$(grep -E "胜率" "$LATEST_LOG" | tail -1 | sed 's/.*[:：]//' | xargs)
        TRADES=$(grep -E "总交易" "$LATEST_LOG" | tail -1 | sed 's/.*[:：]//' | xargs)
        MDD=$(grep -E "最大回撤" "$LATEST_LOG" | tail -1 | sed 's/.*[:：]//' | xargs)
        FINAL_BALANCE=$(grep -E "最终余额 | 最终资金" "$LATEST_LOG" | tail -1 | sed 's/.*[:：]//' | xargs)
        
        # 查找最新的结果目录
        RESULT_DIR=$(ls -td /root/.openclaw/workspace/crypto-trend-trader/backtest/results/*/ 2>/dev/null | head -1 | xargs basename)
        
        # 构建通知消息
        MESSAGE="🎉 2 月回测完成

📊 核心指标
━━━━━━━━━━━━━━━━
总交易数：${TRADES:-N/A}
净盈亏：${PNL:-N/A}
胜率：${WINRATE:-N/A}
最大回撤：${MDD:-N/A}
最终余额：${FINAL_BALANCE:-N/A}

📁 结果目录：$RESULT_DIR

详细分析报告生成中..."
    else
        MESSAGE="🎉 2 月回测完成

日志文件未找到，请稍后查看结果目录。"
    fi
    
    # 写入通知文件
    echo "$MESSAGE" > "$LOG_DIR/backtest_feb_notification.txt"
    
    # 尝试通过 sessions_send 发送到主会话
    if [ -f "$SESSION_FILE" ]; then
        SESSION_KEY=$(cat "$SESSION_FILE")
        # 这里通过 Python 调用 sessions_send
        python3 << PYEOF
import subprocess
import sys

message = """$MESSAGE"""

# 尝试调用 sessions_send
try:
    result = subprocess.run(
        ['openclaw', 'sessions', 'send', '--session', '$SESSION_KEY', '--message', message],
        capture_output=True,
        text=True,
        timeout=30
    )
    if result.returncode == 0:
        print("通知已发送到会话：$SESSION_KEY")
    else:
        print(f"发送失败：{result.stderr}")
except Exception as e:
    print(f"发送异常：{e}")
PYEOF
    fi
    
    # 标记已通知
    touch "$NOTIFIED_FILE"
    echo "[$TIMESTAMP] 完成通知已发送"
    
    # 清理定时任务
    (crontab -l 2>/dev/null | grep -v "monitor_backtest_feb.sh"; ) | crontab -
    echo "[$TIMESTAMP] 定时任务已清理"
    
elif [ "$STATUS" = "failed" ]; then
    echo "[$TIMESTAMP] 回测失败，发送失败通知..."
    
    # 读取失败信息
    FAIL_MSG=$(cat "$LATEST_PROGRESS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message','未知错误'))" 2>/dev/null)
    
    MESSAGE="⚠️ 2 月回测失败

错误信息：$FAIL_MSG

请检查日志文件排查问题。"
    
    echo "$MESSAGE" > "$LOG_DIR/backtest_feb_notification.txt"
    
    if [ -f "$SESSION_FILE" ]; then
        python3 << PYEOF
import subprocess
message = """$MESSAGE"""
try:
    subprocess.run(
        ['openclaw', 'sessions', 'send', '--session', '$SESSION_KEY', '--message', message],
        capture_output=True,
        text=True,
        timeout=30
    )
except:
    pass
PYEOF
    fi
    
    touch "$NOTIFIED_FILE"
    echo "[$TIMESTAMP] 失败通知已发送"
    
elif [ "$STATUS" = "running" ]; then
    echo "[$TIMESTAMP] 回测进行中..."
    
    # 读取进度信息
    PROGRESS=$(cat "$LATEST_PROGRESS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('progress',0))" 2>/dev/null)
    CURRENT_SYMBOL=$(cat "$LATEST_PROGRESS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('current_symbol',''))" 2>/dev/null)
    MSG=$(cat "$LATEST_PROGRESS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null)
    
    # 估算剩余时间 (基于日志增长速度)
    if [ $NEW_LINES -gt 0 ] && [ $ESTIMATED_PROGRESS -gt 0 ]; then
        REMAINING=$((100 - ESTIMATED_PROGRESS))
        RATE=$((NEW_LINES / 10))  # 每分钟行数
        if [ $RATE -gt 0 ]; then
            REMAINING_MINUTES=$((REMAINING * 20000 / RATE / 60))
            ETA_INFO="预计剩余：~${REMAINING_MINUTES}分钟"
        else
            ETA_INFO="计算中..."
        fi
    else
        ETA_INFO="等待更多数据..."
    fi
    
    # 显示日志最后 5 行
    if [ -n "$LATEST_LOG" ] && [ -f "$LATEST_LOG" ]; then
        LAST_LINES=$(tail -5 "$LATEST_LOG" | grep -E "INFO|WARN|ERROR" | tail -3)
    else
        LAST_LINES="无日志内容"
    fi
    
    MESSAGE="🔄 2 月回测进行中

📈 进度
━━━━━━━━━━━━━━━━
估算进度：${ESTIMATED_PROGRESS}%
日志大小：$LOG_SIZE
日志行数：$CURRENT_LINE (新增：$NEW_LINES)

$ETA_INFO

📝 最新动态
\`\`\`
$LAST_LINES
\`\`\`

下次检测：10 分钟后"
    
    echo "$MESSAGE" > "$LOG_DIR/backtest_feb_progress_notification.txt"
    echo "[$TIMESTAMP] 进度通知已写入：$LOG_DIR/backtest_feb_progress_notification.txt"
    
else
    echo "[$TIMESTAMP] 未知状态：$STATUS"
fi

echo "[$TIMESTAMP] ----------------------------------------"
