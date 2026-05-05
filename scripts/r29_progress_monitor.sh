#!/bin/bash
# R29回测进度监控脚本
# 用途：读取最新进度和权益，发送飞书通知

LOG_FILE="/root/.openclaw/workspace/crypto-trend-trader/logs/cron_backtest_R29.log"
PID=341301

# 检查进程是否存活
if ! kill -0 $PID 2>/dev/null; then
    echo "[$(date)] R29进程已结束，监控终止"
    exit 0
fi

# 读取最新进度行
PROGRESS=$(grep "进度" "$LOG_FILE" 2>/dev/null | tail -1)
TRADE_COUNT=$(echo "$PROGRESS" | grep -oP '已成交=\K\d+')
EQUITY=$(echo "$PROGRESS" | grep -oP '权益=\K[0-9.]+')
DATE=$(echo "$PROGRESS" | grep -oP '日期=\K[0-9-]+')
TOTAL_BARS=$(echo "$PROGRESS" | grep -oP '进度 \d+/(\d+)' | grep -oP '\d+$')
CURRENT_BAR=$(echo "$PROGRESS" | grep -oP '进度 \d+' | grep -oP '\d+')

if [ -z "$CURRENT_BAR" ] || [ -z "$TOTAL_BARS" ]; then
    echo "[$(date)] 无法读取进度"
    exit 1
fi

PCT=$(awk "BEGIN {printf \"%.1f\", $CURRENT_BAR/$TOTAL_BARS*100}")
OPEN_POS=$(echo "$PROGRESS" | grep -oP '持仓=\K\d+')

MSG="【R29进度监控】
⏱ $(date '+%H:%M')
📊 进度: ${PCT}% (${CURRENT_BAR}/${TOTAL_BARS})
📅 当前日期: ${DATE}
💰 权益: ${EQUITY} USDT
📝 已成交: ${TRADE_COUNT} 笔
🔒 持仓: ${OPEN_POS}"

OPENCLAW_BIN="/root/.nvm/versions/node/v22.22.0/bin/openclaw"
NVM_BIN="/root/.nvm/versions/node/v22.22.0/bin"
export PATH="$NVM_BIN:$PATH"

# 发送飞书（通过webhook，兼容cron环境）
python3 - << 'PYEOF'
import os, re, requests

LOG_FILE = "/root/.openclaw/workspace/crypto-trend-trader/logs/cron_backtest_R29.log"
WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/8726748b-6a35-4fd0-a487-64c7a37b6455")

try:
    with open(LOG_FILE) as f:
        lines = f.readlines()
    progress_lines = [l for l in lines if "进度" in l]
    if not progress_lines:
        print("无可用进度数据")
        exit(0)
    last = progress_lines[-1]
    
    current_bar = int(re.search(r"进度 (\d+)/", last).group(1))
    total_bars = int(re.search(r"/(\d+) \|", last).group(1))
    equity = re.search(r"权益=([0-9.]+)", last).group(1)
    date = re.search(r"日期=([0-9-]+)", last).group(1)
    trades = re.search(r"已成交=(\d+)", last).group(1)
    positions = re.search(r"持仓=(\d+)", last).group(1)
    pct = current_bar / total_bars * 100
    
    import datetime
    now = datetime.datetime.now().strftime("%H:%M")
    
    msg = f"""【R29进度监控】
⏱ {now}
📊 进度: {pct:.1f}% ({current_bar}/{total_bars})
📅 当前日期: {date}
💰 权益: {equity} USDT
📝 已成交: {trades} 笔
🔒 持仓: {positions}"""
    
    payload = {"msg_type": "text", "content": {"text": msg}}
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    print(f"发送成功: {resp.status_code}")
except Exception as e:
    print(f"发送失败: {e}")
PYEOF
