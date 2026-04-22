#!/bin/bash
NVM_BIN="/root/.nvm/versions/node/v22.22.0/bin"
PATH="$NVM_BIN:$PATH"
OPENCLAW="$NVM_BIN/openclaw"
cd /root/.openclaw/workspace/crypto-trend-trader
PID=828996
LOG="logs/backtest_r28_20250801_20260421.log"
RESULT_DIR="backtest/results"
TOTAL=3105504

if ! kill -0 $PID 2>/dev/null; then
    # 查找最新结果目录
    LATEST=$(ls -t $RESULT_DIR/ | grep "^2026" | head -1)
    STATS="$RESULT_DIR/$LATEST/stats.json"
    if [ -f "$STATS" ]; then
        BALANCE=$(grep '"final_balance"' "$STATS" | grep -oP '\d+\.\d+')
        TRADES=$(grep '"total_trades"' "$STATS" | grep -oP '\d+')
        MSG="【R28完成】最终权益: ${BALANCE} USDT | 成交笔数: ${TRADES}"
    else
        MSG="【R28完成】结果文件未找到"
    fi
    $OPENCLAW message send --channel feishu --target user:ou_0eb63fa8bfa4a131d128c08a559793a9 --message "$MSG"
    crontab -l | grep -v "r28_monitor.sh" | crontab -
    exit 0
fi

PROGRESS=$(grep '进度' "$LOG" 2>/dev/null | tail -1 | grep -oP '\d+/3105504')
DONE=$(echo $PROGRESS | cut -d'/' -f1)
PCT=$(awk "BEGIN {printf \"%.1f\", $DONE/3105504*100}")
EQUITY=$(grep '权益=' "$LOG" 2>/dev/null | tail -1 | grep -oP '权益=\K[0-9.]+')
TRADES=$(grep '已成交=' "$LOG" 2>/dev/null | tail -1 | grep -oP '已成交=\K[0-9]+')
DATE=$(grep '进度' "$LOG" 2>/dev/null | tail -1 | grep -oP '日期=\K[0-9-]+')
MSG="【R28监控】${PCT}% | ${DATE} | 权益=${EQUITY}U | ${TRADES}笔"
$OPENCLAW message send --channel feishu --target user:ou_0eb63fa8bfa4a131d128c08a559793a9 --message "$MSG"
