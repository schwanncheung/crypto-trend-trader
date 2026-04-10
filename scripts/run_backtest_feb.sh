#!/bin/bash
# 2 月回测任务脚本 (feature/3.1 最新配置)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR/.."
LOG_DIR="$PROJECT_ROOT/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# 日志文件
LOG_FILE="$LOG_DIR/backtest_feb_${TIMESTAMP}.log"
PID_FILE="$LOG_DIR/backtest_feb_${TIMESTAMP}.pid"
PROGRESS_FILE="$LOG_DIR/backtest_feb_progress.json"

# 确保日志目录存在
mkdir -p "$LOG_DIR"

echo "========================================" | tee -a "$LOG_FILE"
echo "2 月回测任务启动" | tee -a "$LOG_FILE"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "日志文件：$LOG_FILE" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

# 保存 PID
echo $$ > "$PID_FILE"

# 初始化进度文件
echo '{"status": "running", "start_time": "'$(date -Iseconds)'", "progress": 0, "current_symbol": "", "message": "准备中..."}' > "$PROGRESS_FILE"

cd "$PROJECT_ROOT"

# 执行回测 (2 月完整月份，rule_only 模式)
# 通过临时修改 backtest.yaml 配置实现
BACKTEST_CONFIG="$PROJECT_ROOT/backtest/config/backtest.yaml"
cp "$BACKTEST_CONFIG" "$BACKTEST_CONFIG.bak"
sed -i 's/ai_mode: "text"/ai_mode: "rule_only"/' "$BACKTEST_CONFIG"
sed -i 's/start_date: ".*"/start_date: "2026-02-01"/' "$BACKTEST_CONFIG"
sed -i 's/end_date: ".*"/end_date: "2026-02-28"/' "$BACKTEST_CONFIG"

echo "已修改配置：ai_mode=rule_only, start_date=2026-02-01, end_date=2026-02-28" | tee -a "$LOG_FILE"

python3 backtest/run_backtest.py backtest 2>&1 | tee -a "$LOG_FILE"

# 恢复原配置
mv "$BACKTEST_CONFIG.bak" "$BACKTEST_CONFIG"
echo "已恢复原配置" | tee -a "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

# 更新进度文件
if [ $EXIT_CODE -eq 0 ]; then
    echo '{"status": "completed", "end_time": "'$(date -Iseconds)'", "progress": 100, "message": "回测完成"}' > "$PROGRESS_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
    echo "回测完成" | tee -a "$LOG_FILE"
    echo "结束时间：$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
else
    echo '{"status": "failed", "end_time": "'$(date -Iseconds)'", "progress": 0, "message": "回测失败，退出码:'$EXIT_CODE'"}' > "$PROGRESS_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
    echo "回测失败，退出码：$EXIT_CODE" | tee -a "$LOG_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
fi

# 清理 PID 文件
rm -f "$PID_FILE"

exit $EXIT_CODE
