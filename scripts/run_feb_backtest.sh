#!/bin/bash
# 2 月回测任务脚本 - 每 10 分钟通知进展，完成后推送通知

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"
PID_FILE="$LOG_DIR/backtest_feb.pid"
PROGRESS_FILE="$LOG_DIR/backtest_feb_progress.json"
NOTIFIED_FILE="$LOG_DIR/backtest_feb_notified.flag"
WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/8726748b-6a35-4fd0-a487-64c7a37b6455"

# 初始化进度记录
echo '{"start_time": "'$(date -Iseconds)'", "last_notify": "", "lines": 0}' > "$PROGRESS_FILE"

cd "$SCRIPT_DIR/.."

# 启动回测（后台运行）
nohup python3 backtest/run_backtest.py backtest \
    --start 2026-02-01 \
    --end 2026-02-28 \
    > "$LOG_DIR/backtest_feb_$(date +%Y%m%d_%H%M%S).log" 2>&1 &

BACKTEST_PID=$!
echo $BACKTEST_PID > "$PID_FILE"

echo "回测已启动，PID: $BACKTEST_PID"

# 监控循环（每 10 分钟通知进展）
while kill -0 $BACKTEST_PID 2>/dev/null; do
    sleep 600  # 10 分钟
    
    # 检查是否已发送过进度通知
    if [ ! -f "$NOTIFIED_FILE" ]; then
        LOG_FILE=$(ls -t logs/backtest_feb_*.log 2>/dev/null | head -1)
        if [ -n "$LOG_FILE" ] && [ -f "$LOG_FILE" ]; then
            TOTAL_LINES=$(wc -l < "$LOG_FILE")
            LAST_LINE=$(tail -1 "$LOG_FILE")
            
            curl -s -X POST "$WEBHOOK_URL" \
                -H "Content-Type: application/json" \
                -d "{
                    \"msg_type\": \"text\",
                    \"content\": {
                        \"text\": \"📊 2 月回测进度通知\\n\\n运行时间：$(date -Iseconds)\\n日志行数：$TOTAL_LINES\\n最新进展：$LAST_LINE\\n\\n任务仍在运行中...\"
                    }
                }"
            
            echo "进度通知已发送：$TOTAL_LINES 行"
            touch "$NOTIFIED_FILE"
        fi
    fi
done

# 等待进程结束并获取退出码
wait $BACKTEST_PID
EXIT_CODE=$?

# 清理 PID 文件
rm -f "$PID_FILE"

# 发送完成通知
if [ $EXIT_CODE -eq 0 ]; then
    LOG_FILE=$(ls -t logs/backtest_feb_*.log 2>/dev/null | head -1)
    LAST_LINES=$(tail -20 "$LOG_FILE" 2>/dev/null | grep -E "回测完成|已恢复原配置" | tail -5)
    
    curl -s -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"msg_type\": \"text\",
            \"content\": {
                \"text\": \"✅ 2 月回测已完成\\n\\n结束时间：$(date -Iseconds)\\n退出码：$EXIT_CODE\\n\\n最后日志:\\n$LAST_LINES\\n\\n请查看日志文件获取详细报告。\"
            }
        }"
    
    echo "完成通知已发送"
else
    curl -s -X POST "$WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"msg_type\": \"text\",
            \"content\": {
                \"text\": \"❌ 2 月回测失败\\n\\n结束时间：$(date -Iseconds)\\n退出码：$EXIT_CODE\\n\\n请检查日志文件排查错误。\"
            }
        }"
    
    echo "失败通知已发送"
fi

exit $EXIT_CODE
