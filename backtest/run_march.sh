#!/bin/bash
cd /root/.openclaw/workspace/crypto-trend-trader
python backtest/run_backtest.py backtest --start 2026-03-01 --end 2026-03-31 \
  > backtest/results/march_full.log 2>&1
echo "EXIT_CODE=$?"
