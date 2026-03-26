# 回测系统架构设计方案

> 版本：v1.0
> 日期：2026-03-26
> 项目：crypto-trend-trader

---

## 一、设计目标

### 1.1 核心问题

当前交易系统已在模拟盘运行，但无法快速回答以下问题：

1. 当前参数组合（ADX≥20、信号强度≥7、RR≥2.0）的历史胜率和盈亏比是多少？
2. 调整时间框架组合（如 4h/1h/15m → 1h/30m/15m）会带来多大影响？
3. 移动止损触发点（trailing_stop_trigger_pct=15%）是否过于保守或激进？
4. 哪些合约品种表现最稳定，哪些应当剔除？

### 1.2 回测系统目标

- **高保真**：使用与生产代码相同的指标引擎（`indicator_engine.py`）和规则过滤逻辑（`risk_filter.py`），避免回测与实盘的逻辑漂移
- **快速迭代**：支持参数网格搜索，一次运行可对比数百种参数组合
- **可观测**：输出详细的逐笔交易记录、权益曲线、回撤分析、分品种统计
- **AI-Aware**：为 AI 分析层提供 Mock/Rule-based 替代，使回测不依赖真实 API 调用

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────┐
│                      回测入口 (CLI)                        │
│   backtest/run_backtest.py  [参数网格 / 单次回测]          │
└───────────────────┬─────────────────────────────────────┘
                    │
        ┌───────────▼────────────┐
        │   BacktestEngine       │  核心调度，逐K线驱动
        │   (engine.py)          │
        └───────┬────────────────┘
                │
   ┌────────────┼──────────────────┬─────────────────┐
   ▼            ▼                  ▼                 ▼
┌──────┐  ┌──────────┐  ┌──────────────────┐  ┌──────────┐
│ Data │  │ Signal   │  │ Position Manager │  │ Reporter │
│ Feed │  │ Pipeline │  │ (仓位/止损管理)   │  │ (报告)   │
│      │  │          │  │                  │  │          │
└──────┘  └──────────┘  └──────────────────┘  └──────────┘
```

### 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| Data Feed | `data/feed.py` | 加载/缓存历史K线，切片供引擎消费 |
| Signal Pipeline | `signal/pipeline.py` | 复用生产层指标引擎 + 规则过滤 + AI Mock |
| BacktestEngine | `engine.py` | 时间序列驱动，管理信号→开仓→持仓→平仓全流程 |
| Position Manager | `position/manager.py` | 复用移动止损/分批止盈逻辑（无交易所调用） |
| Reporter | `report/reporter.py` | 生成权益曲线、统计指标、HTML/CSV 报告 |
| Optimizer | `optimizer.py` | 参数网格搜索，多进程并行 |
| CLI | `run_backtest.py` | 命令行入口 |

---

## 三、数据层设计 (Data Feed)

### 3.1 数据来源策略

```
优先级：本地缓存 → OKX REST API（历史端点）→ 错误报警
```

- 历史数据通过 OKX `GET /api/v5/market/candles` 批量拉取并缓存到本地 Parquet 文件
- 缓存目录：`backtest/data/cache/{symbol}/{timeframe}.parquet`
- 首次运行自动下载，后续增量更新
- 支持指定回测区间（`start_date` / `end_date`）

### 3.2 数据格式

```python
# 标准 OHLCV DataFrame，列名与生产 fetch_kline.py 保持一致
columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
# timestamp: UTC Unix ms，索引为 DatetimeIndex(UTC)
```

### 3.3 多周期数据对齐

回测时以**最低周期K线**为时钟驱动（如 15m），高周期通过 resample 或预计算派生，确保：
- 不使用未来数据（bar 收盘后才可见）
- 高周期 K 线在其收盘时刻才更新

---

## 四、信号流水线 (Signal Pipeline)

### 4.1 复用生产逻辑

信号流水线直接 `import` 生产代码模块，不重写：

```
fetch_kline.py   → detect_trend_structure(), calculate_support_resistance()
indicator_engine.py → compute_indicators(), build_market_snapshot(), apply_rule_filter()
risk_filter.py   → check_signal_quality(), calculate_position_size()
```

### 4.2 AI 分析层替代方案

AI 调用在回测中有两种替代策略：

#### 方案 A：Rule-Only 模式（推荐用于参数优化）
- 完全跳过 AI 分析层
- 仅使用规则引擎（`indicator_engine.py`）的输出构造伪决策
- 以指标量化信号替代 AI 的主观判断：

```python
# 伪AI决策构造规则
signal_strength = adx_score + ema_align_score + volume_score  # 0-10
confidence = 'high' if signal_strength >= 7 and volume_confirmed else 'low'
risk_reward = (resistance - entry) / (entry - support)  # 多头示例
```

#### 方案 B：LLM Mock 模式（用于验证 AI 决策质量）
- 预先对历史快照批量调用真实 LLM，将结果缓存为 JSON
- 回测时读取缓存，模拟 AI 分析的实际效果
- 适合：评估 AI 分析层是否真的比纯规则更优

### 4.3 信号生成流程

```
每根 15m K线收盘后：
  1. 检查是否为扫描周期（每4根 = 1h触发一次，可配置）
  2. 构建多周期 DataFrame 切片（截至当前时刻的历史数据）
  3. 调用 indicator_engine.build_market_snapshot()
  4. 调用 apply_rule_filter()，不通过 → wait
  5. 通过 → 构造 AI 决策（Rule-Only or Mock）
  6. 调用 risk_filter.check_signal_quality()
  7. 返回最终信号：{signal, entry, stop_loss, take_profit, position_size}
```

---

## 五、回测引擎 (BacktestEngine)

### 5.1 驱动方式

**逐K线（bar-by-bar）驱动**，以最低周期（15m）为步长：

```python
for timestamp, bar in feed.iter_bars('15m'):
    engine.on_bar(timestamp, bar)
```

`on_bar` 内部逻辑：
1. 更新所有时间框架的 OHLCV 缓冲区
2. 检查持仓止损/止盈是否触发（以 bar 的 high/low 判断）
3. 调用 PositionManager 检查移动止损和强制平仓
4. 触发信号检查（每N根bar一次）
5. 有信号且有仓位余额 → 开仓

### 5.2 成交价格模拟

| 类型 | 成交价 | 说明 |
|------|--------|------|
| 开仓 | 下一根 bar open | 信号K收盘后下一根开盘入场，贴近真实 |
| 止损 | stop_loss 价格 | 以 low 穿越止损时以止损价成交 |
| 止盈 | take_profit 价格 | 以 high 穿越止盈时以止盈价成交 |
| 强制平仓 | 当前 bar close | 模拟市价平仓 |
| 移动止损触发 | 更新后的 stop_loss | 以新止损价挂单 |

### 5.3 手续费与滑点

```yaml
# 在回测配置中设置
fee_rate: 0.0005      # taker 手续费 0.05%（OKX 合约）
slippage_pct: 0.001   # 滑点 0.1%（保守估计）
```

### 5.4 仓位约束

- 最大同时持仓数：`max_open_positions`（对应生产配置）
- 账户初始余额：可配置（如 10000 USDT）
- 日亏损上限：`max_daily_loss_pct`，触发后当日停止开仓
- 杠杆：与生产一致（默认 10x）

---

## 六、持仓管理 (Position Manager)

复用生产 `trade_manager.py` 的核心逻辑，去除所有交易所 API 调用，改为内存状态操作：

### 6.1 持仓状态机

```
OPEN → [止盈/止损/移动止损/强制平仓/结构破坏] → CLOSED
```

### 6.2 管理事件（每根 bar 检查）

| 事件 | 触发条件 | 动作 |
|------|----------|------|
| 止损触发 | bar.low ≤ stop_loss（多头） | 以止损价平仓 |
| 止盈触发 | bar.high ≥ take_profit（多头） | 以止盈价平仓 |
| 第一批分批止盈 | 浮盈 ≥ `partial_profit_trigger_pct_1` | 平仓 `partial_profit_ratio_1` 比例 |
| 第二批分批止盈 | 浮盈 ≥ `partial_profit_trigger_pct_2` | 再平仓 `partial_profit_ratio_2` 比例 |
| 移动止损 | 浮盈 ≥ `trailing_stop_trigger_pct` | 将止损上移至保本或指定位置 |
| 强制平仓 | 浮亏 ≤ `force_close_loss_pct` | 市价全平（兜底机制） |
| 结构破坏 | 收盘价跌破关键支撑/阻力缓冲区 | 市价全平 |

---

## 七、参数优化器 (Optimizer)

### 7.1 优化目标

默认优化目标函数（可选其一或加权组合）：

```python
# 夏普比率（推荐主指标）
objective = sharpe_ratio

# 其他可选
objective = calmar_ratio          # 年化收益 / 最大回撤
objective = profit_factor         # 总盈利 / 总亏损
objective = win_rate * avg_rr     # 胜率 × 平均RR
```

### 7.2 参数搜索空间（示例）

```python
param_grid = {
    # 规则引擎参数
    'adx_trending_threshold':     [15, 20, 25, 30],
    'volume_ratio_threshold':     [1.0, 1.2, 1.5],
    'min_trending_timeframes':    [1, 2],
    'require_anchor_aligned':     [True, False],

    # 信号质量阈值
    'min_signal_strength':        [6, 7, 8],
    'min_rr_ratio':               [1.5, 2.0, 2.5],

    # 仓位管理参数
    'trailing_stop_trigger_pct':  [10.0, 15.0, 20.0],
    'partial_profit_trigger_pct': [20.0, 25.0, 30.0],

    # 时间框架组合
    'timeframes':                 [
        ['1h', '30m', '15m'],
        ['4h', '1h', '15m'],
        ['4h', '1h', '30m'],
    ],
}
```

### 7.3 并行执行

- 使用 Python `multiprocessing.Pool` 并行跑参数组合
- 每个 worker 独立实例化 `BacktestEngine`，无共享状态
- 支持 `--workers N` 指定并发数（默认 CPU 核数 - 1）
- 结果汇总后按目标函数降序排列，输出 Top-N 组合

### 7.4 过拟合防护

- **样本外验证**：训练集（80%历史数据）优化参数，测试集（20%）验证
- **Walk-Forward 分析**：滚动窗口验证，防止参数对特定时期过拟合
- **参数稳定性检查**：Top 参数组合在相邻区间表现是否一致

---

## 八、报告模块 (Reporter)

### 8.1 输出文件结构

```
backtest/results/
├── {run_id}/
│   ├── summary.json          # 核心指标摘要
│   ├── trades.csv            # 逐笔交易记录
│   ├── equity_curve.csv      # 逐日权益曲线
│   ├── report.html           # 可视化 HTML 报告（含图表）
│   └── params.yaml           # 本次回测使用的参数
└── optimization/
    ├── grid_results.csv      # 所有参数组合的结果汇总
    └── top10_params.yaml     # Top-10 最优参数组合
```

### 8.2 核心统计指标

| 指标 | 说明 |
|------|------|
| 总收益率 | 回测期间账户权益变化 |
| 年化收益率 | 折算为年化 |
| 最大回撤 | 最大峰谷权益回撤幅度 |
| 夏普比率 | 超额收益 / 波动率（年化，无风险利率=0）|
| 卡玛比率 | 年化收益 / 最大回撤 |
| 总交易次数 | 开仓次数 |
| 胜率 | 盈利交易占比 |
| 盈亏比 | 平均盈利 / 平均亏损 |
| 盈利因子 | 总盈利 / 总亏损 |
| 平均持仓时间 | 单笔平均持仓 bar 数 |
| 最大连亏次数 | 最长连续亏损笔数 |
| 日均交易次数 | 策略交易频率参考 |

### 8.3 分品种统计

按合约分组输出上述指标，快速识别哪些品种应纳入/排除交易列表。

### 8.4 HTML 报告（可视化）

使用 `plotly` 生成交互式图表：

1. 权益曲线（含回撤区域标注）
2. 逐笔交易盈亏分布直方图
3. 月度收益热力图
4. 多空方向胜率对比
5. 参数优化热力图（优化模式下）

---

## 九、配置文件设计

回测系统使用独立配置文件，与生产 `settings.yaml` 解耦：

```yaml
# backtest/config/backtest.yaml

backtest:
  # 回测时间区间
  start_date: "2024-01-01"
  end_date:   "2025-12-31"

  # 回测账户
  initial_balance: 10000.0     # 初始 USDT
  leverage: 10                  # 杠杆倍数

  # 成本模型
  fee_rate: 0.0005              # taker 手续费率
  slippage_pct: 0.001           # 滑点百分比

  # 信号触发频率
  signal_interval_bars: 4       # 每N根最低周期bar触发一次信号检查

  # AI 替代模式
  ai_mode: "rule_only"          # rule_only | llm_mock

  # 数据缓存目录
  data_cache_dir: "backtest/data/cache"

# 以下参数继承自 config/settings.yaml，可覆盖用于优化
override:
  timeframes: ["1h", "30m", "15m"]
  adx_trending_threshold: 20
  min_signal_strength: 7
  min_rr_ratio: 2.0
  trailing_stop_trigger_pct: 15.0
```

---

## 十、目录结构

```
backtest/
├── README.md                  # 索引文档
├── design.md                  # 本文档：架构设计
├── dev-plan.md                # 开发计划
├── run_backtest.py            # CLI 入口
├── config/
│   └── backtest.yaml          # 回测配置
├── data/
│   ├── downloader.py          # 历史数据下载器
│   ├── feed.py                # 数据馈送（加载+切片+对齐）
│   └── cache/                 # Parquet 缓存（gitignore）
├── engine/
│   ├── __init__.py
│   ├── engine.py              # 核心回测引擎
│   ├── position.py            # Position 数据类
│   └── position_manager.py   # 持仓管理（复用生产逻辑）
├── signal/
│   ├── __init__.py
│   ├── pipeline.py            # 信号流水线（复用生产指标引擎）
│   └── ai_mock.py             # AI 分析替代实现
├── report/
│   ├── __init__.py
│   ├── reporter.py            # 统计指标计算
│   ├── visualizer.py          # Plotly 图表生成
│   └── templates/
│       └── report.html        # HTML 报告模板
├── optimizer.py               # 参数网格搜索优化器
└── results/                   # 回测结果输出（gitignore）
```

---

## 十一、与生产代码的耦合原则

### 可直接复用（零修改）
- `scripts/indicator_engine.py`：所有技术指标计算
- `scripts/fetch_kline.py`：`detect_trend_structure()`、`calculate_support_resistance()`
- `scripts/risk_filter.py`：`check_signal_quality()`、`calculate_position_size()`
- `scripts/config_loader.py`：配置读取

### 需要适配（去除 IO 依赖）
- `trade_manager.py`：移除 `exchange` 调用，保留纯计算逻辑
- `execute_trade.py`：仅复用价格计算部分

### 完全替代（不适合回测）
- `notifier.py`：回测中替换为 no-op
- `generate_chart.py`：回测中默认禁用（可选开启用于 debug）
- OKX API 调用：全部替换为本地缓存数据

---

## 十二、关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 驱动粒度 | 最低周期 bar（15m）| 与生产扫描频率一致，避免信号遗漏 |
| 成交价格 | 下一根 bar open | 最贴近真实，避免前视偏差 |
| AI 替代 | Rule-Only 为主 | 可重复执行，无 API 成本，适合大量参数搜索 |
| 数据存储 | Parquet | 读取速度快，压缩率高，与 Pandas 无缝集成 |
| 并行方案 | multiprocessing | 规避 Python GIL，每组参数独立进程 |
| 过拟合防护 | 样本外验证 + Walk-Forward | 业界标准做法 |
| 报告格式 | HTML + CSV | HTML 可交互查看，CSV 便于二次分析 |
