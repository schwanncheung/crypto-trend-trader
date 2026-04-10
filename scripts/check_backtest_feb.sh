#!/bin/bash
# 2 月回测进度检测脚本
# 每 2 分钟检测一次，完成后发送飞书通知

LOG_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/backtest_feb_20260407_1548.log"
TASK_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/backtest_task_20260407_1548.pid"
NOTIFIED_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/backtest_feb_notified.flag"

# 检查是否已通知过
if [ -f "$NOTIFIED_FILE" ]; then
    echo "已通知过，退出"
    exit 0
fi

# 检查任务是否完成
if grep -q "回测完成" "$LOG_FILE" 2>/dev/null; then
    echo "回测已完成，发送通知..."
    
    # 提取关键结果
    PNL=$(grep "净盈亏" "$LOG_FILE" | tail -1 | awk '{print $NF}')
    WINRATE=$(grep "胜率" "$LOG_FILE" | tail -1 | awk '{print $NF}')
    TRADES=$(grep "总交易" "$LOG_FILE" | tail -1 | awk '{print $NF}' | tr -d ',')
    MDD=$(grep "最大回撤" "$LOG_FILE" | tail -1 | awk '{print $NF}')
    
    # 发送飞书通知
    curl -X POST "https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook" \
      -H "Content-Type: application/json" \
      -d "{
        \"msg_type\": \"text\",
        \"content\": {
          \"text\": \"🎉 2 月回测完成\\n\\n总交易：${TRADES}笔\\n净盈亏：${PNL}\\n胜率：${WINRATE}\\n最大回撤：${MDD}\\n\\n详细报告：backtest/results/\"
        }
      }" 2>/dev/null
    
    # 标记已通知
    touch "$NOTIFIED_FILE"
    echo "通知已发送"
    
    # 清理定时任务
    crontab -l | grep -v "check_backtest_feb.sh" | crontab -
    echo "定时任务已清理"
else
    echo "回测进行中..."
    tail -5 "$LOG_FILE" 2>/dev/null
fi
