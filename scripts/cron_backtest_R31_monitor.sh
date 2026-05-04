#!/bin/bash
# R31 回测进度监控（2026-01-01 → 2026-05-03）
LOG_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/backtest_R31.log"
OUT_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/r31_progress.txt"
OPENCLAW_BIN="/root/.nvm/versions/node/v22.22.0/bin/openclaw"
NVM_BIN="/root/.nvm/versions/node/v22.22.0/bin"
FEISHU_TARGET="user:ou_0eb63fa8bfa4a131d128c08a559793a9"

export PATH="$NVM_BIN:$PATH"

if pgrep -f "run_backtest.py.*2026-01-01.*2026-05-03" > /dev/null 2>&1; then
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
        
        MSG="🤖 **R31 回测进度**
🗓️ $DT | 📊 $CUR/$TOT ($PCT%)
💰 权益: $EQ USDT | 📦 持仓: $POS | 🔢 已成交: $TRADES 笔"
        
        $OPENCLAW_BIN message send --channel feishu --target "$FEISHU_TARGET" --message "$MSG" 2>/dev/null
    fi
else
    # 回测结束，读取最终结果
    RESULT_FILE="/root/.openclaw/workspace/crypto-trend-trader/backtest/results/R31/stats.json"
    if [ -f "$RESULT_FILE" ]; then
        python3 -c "
import json
d=json.load(open('$RESULT_FILE'))
pct=d['net_pnl_usdt']/d['initial_balance']*100
win_r=d['win_count']/d['total_trades']*100 if d['total_trades']>0 else 0
mdd=d['max_drawdown_pct']
print('DONE|%d|%.4f|%.2f|%.1f|%d|%d|%.1f|%d' % (
    d['total_trades'], d['final_balance'], d['net_pnl_usdt'], pct, d['win_rate_pct'], d['win_count'], win_r, mdd))
" > "$OUT_FILE"
        
        FINAL=$(cat "$OUT_FILE")
        IFS='|' read -r STATUS TRADES FINAL_BAL PNL PNL_PCT WIN_RATE WINS LOSSSES MDD <<< "$FINAL"
        
        MSG="✅ **R31 回测完成**
📊 总交易: $TRADES 笔 | 胜率: $WIN_RATE%
💰 净收益: $PNL USDT ($PNL_PCT%) | 余额: $FINAL_BAL USDT
📉 最大回撤: $MDD%"
        
        $OPENCLAW_BIN message send --channel feishu --target "$FEISHU_TARGET" --message "$MSG" 2>/dev/null
        
        # 删除定时任务
        grep -v "cron_backtest_R31" /etc/cron.d/openclaw_tasks 2>/dev/null | sudo tee /etc/cron.d/openclaw_tasks > /dev/null
    fi
fi