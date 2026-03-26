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
