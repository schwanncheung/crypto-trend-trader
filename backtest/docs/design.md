# 回测系统架构设计

## 一、设计目标

- **高保真**：复用生产代码的指标引擎和规则过滤，避免回测与实盘逻辑漂移
- **快速迭代**：支持参数网格搜索，一次运行对比数百种参数组合
- **可观测**：输出逐笔交易记录、权益曲线、回撤分析
- **AI-Aware**：提供 Mock/Rule-based/Real 三种 AI 替代方案

---

## 二、整体架构

```
run_backtest.py (CLI 入口)
        │
        ▼
┌───────────────┐
│ BacktestEngine│  bar-by-bar 驱动
└───────┬───────┘
        │
   ┌────┼────┬────────────┐
   ▼    ▼    ▼            ▼
DataFeed  Signal    Position   Reporter
          Pipeline  Manager
```

### 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| Data Feed | `data/feed.py` | 加载 Parquet 缓存，切片供引擎消费 |
| Signal Pipeline | `sig/pipeline.py` | 复用 `indicator_engine` + `risk_filter` |
| BacktestEngine | `engine/engine.py` | 时间序列驱动，管理信号→开仓→平仓 |
| Position Manager | `engine/position_manager.py` | 止损/止盈/移动止损（纯计算，无 IO） |
| Reporter | `report/reporter.py` | 统计指标 + HTML/CSV 报告 |
| Optimizer | `optimizer.py` | 参数网格搜索，多进程并行 |

---

## 三、数据层

### 3.1 数据来源

```
本地 Parquet 缓存 → OKX REST API → 错误报警
```

- OKX `GET /api/v5/market/candles` 批量拉取
- 缓存目录：`backtest/data/cache/{symbol}/{timeframe}.parquet`
- 支持增量更新

### 3.2 多周期对齐

以**最低周期**（如 15m）为时钟驱动：
- 不使用未来数据（bar 收盘后才可见）
- 高周期 K 线在其收盘时刻才更新

---

## 四、信号流水线

### 4.1 复用生产代码

```python
# sig/pipeline.py 直接导入生产模块
from scripts.indicator_engine import rule_engine_filter, compute_timeframe_indicators
from scripts.risk_filter import check_signal_quality, calculate_position_size
```

### 4.2 AI 替代方案

| 模式 | 类 | 说明 |
|------|----|------|
| `rule_only` | `RuleOnlyMock` | 量化规则构造伪决策，无 API 成本（**推荐**） |
| `llm_mock` | `LLMMockCache` | 读取预缓存的 LLM 响应 JSON |
| `llm_real` | `LLMRealAnalyzer` | 调用真实 LLM API（需 API Key） |

### 4.3 信号生成流程

```
每根 bar 收盘：
  1. 检查是否为扫描周期（每 N 根触发一次）
  2. 构建多周期 DataFrame 切片
  3. 调用 rule_engine_filter()
  4. 通过 → 构造决策（RuleOnly / Mock / Real）
  5. 调用 risk_filter.check_signal_quality()
  6. 返回 {signal, entry, stop_loss, take_profit, contracts}
```

---

## 五、回测引擎

### 5.1 驱动方式

```python
for timestamp, bar in feed.iter_bars('15m'):
    engine.on_bar(timestamp, bar)
```

`on_bar` 逻辑：
1. 更新 OHLCV 缓冲区
2. 检查止损/止盈触发
3. 调用 PositionManager 检查移动止损
4. 触发信号检查（每 N 根 bar）
5. 有信号且有余额 → 开仓

### 5.2 成交价格模拟

| 类型 | 成交价 |
|------|--------|
| 开仓 | 下一根 bar open |
| 止损 | stop_loss 价格 |
| 止盈 | take_profit 价格 |
| 强制平仓 | 当前 bar close |

### 5.3 成本模型

```yaml
fee_rate: 0.0005      # taker 手续费 0.05%
slippage_pct: 0.001   # 滑点 0.1%
```

---

## 六、持仓管理

复用生产 `trade_manager.py` 逻辑，去除 IO 依赖：

```
OPEN → [止盈/止损/移动止损/强制平仓] → CLOSED
```

| 事件 | 触发条件 |
|------|----------|
| 止损 | bar.low ≤ stop_loss |
| 止盈 | bar.high ≥ take_profit |
| 分批止盈 | 浮盈达到阈值 |
| 移动止损 | 浮盈达到阈值，止损移至保本 |
| 强制平仓 | 浮亏超过阈值 |

---

## 七、参数优化器

### 7.1 优化目标

默认：**夏普比率**

可选：`calmar_ratio` / `net_pnl_pct` / `expectancy_usdt`

### 7.2 约束条件

```yaml
constraints:
  min_trades: 20        # 最少交易笔数
  max_drawdown_pct: 30.0  # 最大回撤
  min_win_rate_pct: 35.0   # 最低胜率
```

### 7.3 过拟合防护

- **样本外验证**：80% 训练 / 20% OOS 验证
- 结果按训练集指标排序，取 Top-N 进行 OOS 验证

---

## 八、报告输出

`backtest/results/<run_id>/`：

| 文件 | 内容 |
|------|------|
| `trades.csv` | 逐笔交易记录 |
| `stats.json` | 12 项统计指标 |
| `report.html` | HTML 综合报告 |
| `equity_curve.html` | Plotly 权益曲线 |
| `pnl_bars.html` | PnL 柱状图 |
| `monthly_heatmap.html` | 月度热力图 |

### 核心指标

| 指标 | 说明 |
|------|------|
| 年化收益率 | 折算为年化 |
| 最大回撤 | 最大峰谷回撤 |
| 夏普比率 | 超额收益 / 波动率 |
| Calmar 比率 | 年化收益 / 最大回撤 |
| 胜率 | 盈利交易占比 |
| 盈亏比 | 平均盈利 / 平均亏损 |

---

## 九、目录结构

```
backtest/
├── run_backtest.py           # CLI 入口
├── config_loader.py          # 配置合并
├── optimizer.py              # 网格搜索
├── config/
│   ├── backtest.yaml         # 回测配置
│   └── param_grid.yaml       # 参数空间
├── data/
│   ├── downloader.py         # 数据下载
│   ├── feed.py               # 数据切片
│   └── cache/                # Parquet 缓存
├── engine/
│   ├── engine.py             # 回测引擎
│   ├── position.py           # Position 类
│   └── position_manager.py   # 持仓管理
├── sig/
│   ├── pipeline.py           # 信号流水线
│   └── ai_mock.py            # AI 替代实现
├── report/
│   ├── reporter.py           # 统计报告
│   ├── visualizer.py         # Plotly 图表
│   └── templates/            # HTML 模板
└── results/                  # 输出结果
```

---

## 十、与生产代码的耦合

### 直接复用

- `scripts/indicator_engine.py`
- `scripts/risk_filter.py`
- `scripts/config_loader.py`
- `scripts/fetch_kline.py`（部分函数）

### 完全替代

- OKX API 调用 → 本地 Parquet 数据
- `notifier.py` → no-op
- `generate_chart.py` → 禁用