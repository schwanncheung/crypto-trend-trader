#!/bin/bash
cd /root/.openclaw/workspace/crypto-trend-trader
python backtest/run_backtest.py backtest --start 2026-02-01 --end 2026-02-28 \
  > backtest/results/feb_full.log 2>&1
echo "EXIT_CODE=$?"
