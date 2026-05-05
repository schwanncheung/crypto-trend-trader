#!/bin/bash
# R30 回测进度记录（供主动查询）
LOG_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/cron_backtest_R30.log"
OUT_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/r30_progress.txt"

if pgrep -f "run_backtest.py.*R30" > /dev/null 2>&1; then
    PROG_LINE=$(grep "进度 " "$LOG_FILE" 2>/dev/null | tail -1)
    if [ -n "$PROG_LINE" ]; then
        CUR=$(echo "$PROG_LINE" | grep -oP '进度 \K\d+')
        TOT=$(echo "$PROG_LINE" | grep -oP '/\K\d+')
        DT=$(echo "$PROG_LINE" | grep -oP '日期=\K\d{4}-\d{2}-\d{2}')
        EQ=$(echo "$PROG_LINE" | grep -oP '权益=\K[0-9.]+')
        POS=$(echo "$PROG_LINE" | grep -oP '持仓=\K\d+')
        TRADES=$(echo "$PROG_LINE" | grep -oP '已成交=\K\d+')
        PCT=$(python3 -c "print('%.1f' % ($CUR/$TOT*100))")
        echo "RUNNING|$DT|$PCT|$CUR|$TOT|$EQ|$POS|$TRADES" > "$OUT_FILE"
    fi
else
    # 回测结束
    RESULT_FILE="/root/.openclaw/workspace/crypto-trend-trader/backtest/results/R30/stats.json"
    if [ -f "$RESULT_FILE" ]; then
        python3 -c "
import json
d=json.load(open('$RESULT_FILE'))
pct=d['net_pnl_usdt']/d['initial_balance']*100
print('DONE|%.4f|%.4f|%.2f|%d|%d|%.1f' % (d['final_balance'], d['net_pnl_usdt'], pct, d['total_trades'], int(d.get('win_rate',0)*d['total_trades']), d.get('win_rate',0)*100))
" > "$OUT_FILE"
    fi
fi