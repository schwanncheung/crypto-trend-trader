# 回测系统 (Backtest System)

本目录包含 crypto-trend-trader 回测系统的完整设计方案与开发计划。

## 文档索引

| 文档 | 说明 |
|------|------|
| [design.md](design.md) | 系统架构与模块设计方案 |
| [dev-plan.md](dev-plan.md) | 分阶段开发计划与任务拆解 |

## 快速目标

通过历史K线数据离线重放真实交易逻辑，量化验证并调优以下核心参数：

- 规则引擎过滤阈值（ADX、成交量比、RSI 极值）
- 仓位管理参数（移动止损触发点、分批止盈比例）
- 多周期组合（1h/30m/15m vs 4h/1h/15m 等）
- AI 信号质量阈值（min_signal_strength、min_rr_ratio）


# 目录说明
## Phase 1-3
backtest/data/downloader.py — OKX 历史数据下载，Parquet 增量缓存
backtest/data/feed.py — DataFeed，严格无前视偏差切片
backtest/engine/position.py — 持仓数据类
backtest/engine/position_manager.py — SL/TP/移动止损/分批止盈
backtest/engine/engine.py — bar-by-bar 主循环，多品种时间轴合并
backtest/signal/ai_mock.py — RuleOnlyMock 量化评分（替代 LLM）
backtest/signal/pipeline.py — 8步信号流水线，复用生产代码

## Phase 4-6
backtest/report/reporter.py — 12项统计指标 + CSV/JSON/HTML 报告
backtest/report/visualizer.py — Plotly 权益曲线/PnL柱图/月度热力图/饼图
backtest/report/templates/report.html — 独立 HTML 报告模板
backtest/optimizer.py — GridOptimizer 多进程网格搜索 + OOS 验证
backtest/config/param_grid.yaml — 参数搜索空间配置
backtest/run_backtest.py — CLI 入口（download / backtest / optimize）
backtest/config_loader.py — 配置合并与 override 支持


# 快速上手：
``` bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 下载数据
python backtest/run_backtest.py download --symbols BTC/USDT:USDT ETH/USDT:USDT --start 2025-01-01 --end 2026-03-28

# 3. 单次回测
python backtest/run_backtest.py backtest --start 2025-01-01 --end 2026-03-28

# 4. 参数优化
python backtest/run_backtest.py optimize --start 2025-01-01 --end 2026-03-28 --workers 4
```