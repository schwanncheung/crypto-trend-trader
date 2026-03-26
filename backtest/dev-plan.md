# 回测系统开发计划

> 版本：v1.0
> 日期：2026-03-26
> 项目：crypto-trend-trader

---

## 一、开发阶段总览

```
Phase 1 ── 数据基础设施          （数据下载、缓存、多周期对齐）
Phase 2 ── 核心回测引擎          （bar驱动、持仓管理、成交模拟）
Phase 3 ── 信号流水线            （复用生产指标引擎、规则过滤）
Phase 4 ── 报告模块              （统计指标、权益曲线、HTML报告）
Phase 5 ── 参数优化器            （网格搜索、并行、样本外验证）
Phase 6 ── 集成验证              （与生产参数对比、边界测试）
```

---

## 二、Phase 1：数据基础设施

### 目标
能够从 OKX 批量拉取历史K线并以 Parquet 格式缓存，支持增量更新和多周期对齐输出。

### 任务清单

#### 1.1 历史数据下载器 `backtest/data/downloader.py`

- [ ] 基于 `ccxt.okx` 调用 `fetch_ohlcv()`，分页循环拉取（每次最多300根，向前翻页）
- [ ] 支持参数：`symbol`、`timeframe`、`start_date`、`end_date`
- [ ] 将结果保存为 `backtest/data/cache/{symbol_safe}/{timeframe}.parquet`
- [ ] 增量更新：检查已有缓存的最新时间戳，仅拉取缺失部分
- [ ] 错误处理：网络超时重试3次，失败记录日志不中断整体下载
- [ ] CLI 用法：`python downloader.py --symbols BTC/USDT:USDT ETH/USDT:USDT --start 2024-01-01`

**验收标准**
- 能成功下载 BTC/USDT:USDT 的 15m/30m/1h 数据至少1年
- Parquet 文件可被 `pd.read_parquet()` 正确加载
- 增量运行不重复下载已有数据

#### 1.2 数据馈送 `backtest/data/feed.py`

- [ ] `class DataFeed`：加载指定区间内多个周期的 Parquet 数据
- [ ] `iter_bars(timeframe)` 生成器：按时间顺序逐根 yield `(timestamp, bar_dict)`
- [ ] `get_history(symbol, timeframe, end_ts, limit)` 方法：返回截至 `end_ts` 的最近 `limit` 根 K 线（用于指标计算，严格不含未来数据）
- [ ] 多周期对齐：以最低周期为时钟，高周期数据按时间戳对齐（bar收盘时刻才更新）
- [ ] 内存优化：对超大数据集使用分块加载

**验收标准**
- `get_history()` 在任意时间点返回的数据不包含该时间点之后的 bar
- 多周期对齐测试：1h bar 在 1h 收盘时刻才对外可见

---

## 三、Phase 2：核心回测引擎

### 目标
实现 bar-by-bar 驱动的回测主循环，处理开仓/持仓/平仓的完整生命周期。

### 任务清单

#### 2.1 Position 数据类 `backtest/engine/position.py`

- [ ] `@dataclass Position`：
  ```python
  symbol: str
  side: str              # 'long' | 'short'
  entry_price: float
  contracts: float       # 合约张数
  stop_loss: float
  take_profit: float
  open_time: datetime
  close_time: datetime | None
  close_price: float | None
  close_reason: str | None  # 'tp'|'sl'|'trailing_sl'|'partial_tp'|'force_close'|'structure'
  pnl_usdt: float
  pnl_pct: float
  # 移动止损跟踪
  trailing_active: bool = False
  peak_price: float | None = None   # 多头最高价 / 空头最低价
  partial_tp1_done: bool = False
  partial_tp2_done: bool = False
  ```

#### 2.2 持仓管理 `backtest/engine/position_manager.py`

- [ ] `check_stop_loss(position, bar)` → 返回是否触发，触发价格
- [ ] `check_take_profit(position, bar)` → 返回是否触发，触发价格
- [ ] `check_trailing_stop(position, bar, config)` → 更新 peak_price，必要时上移 stop_loss
- [ ] `check_partial_tp(position, bar, config)` → 检查两批分批止盈条件
- [ ] `check_force_close(position, bar, config)` → 检查强制平仓阈值
- [ ] `apply_fee_slippage(price, side, config)` → 返回含手续费和滑点的实际成交价

**注意**：所有函数为纯函数，不依赖任何 IO，便于单元测试。

#### 2.3 核心引擎 `backtest/engine/engine.py`

- [ ] `class BacktestEngine`：
  - `__init__(config, data_feed, signal_pipeline)` 初始化
  - `run()` 主循环：遍历 feed.iter_bars()，调用 `on_bar()`
  - `on_bar(ts, bar)` 内部逻辑：
    1. 更新多周期数据缓冲区
    2. **先处理持仓**：遍历所有 open positions，按优先级检查 SL > TP > 移动止损 > 分批止盈 > 强制平仓 > 结构破坏
    3. **再检查信号**：每 `signal_interval_bars` 根触发一次，调用 signal_pipeline
    4. 有信号 + 有仓位余额 → 开仓（记录到 positions 列表）
  - `_open_position(signal)` → 创建 Position 对象，扣减账户余额
  - `_close_position(position, price, reason)` → 计算 PnL，释放余额，追加到 trades 列表
  - `get_results()` → 返回 trades 列表 + equity_curve

**验收标准**
- 跑通 BTC/USDT:USDT 一个月数据不报错
- 止损/止盈触发逻辑用单元测试覆盖（伪造 bar 数据测试边界条件）
- 账户余额始终 ≥ 0（不允许负权益开仓）

---

## 四、Phase 3：信号流水线

### 目标
将生产代码的指标引擎和规则过滤器接入回测，实现高保真信号生成。

### 任务清单

#### 3.1 AI Mock `backtest/signal/ai_mock.py`

- [ ] `class RuleOnlyMock`：基于指标量化分数构造伪 AI 决策
  ```python
  def analyze(self, snapshot: str, tf_indicators: dict,
               support_levels: list, resistance_levels: list,
               current_price: float) -> dict:
      # 从 tf_indicators 提取已计算的指标值
      # 构造符合 risk_filter.check_signal_quality() 期望格式的 decision dict
      # 自动计算 entry/stop_loss/take_profit
  ```
- [ ] `class LLMMockCache`：从预缓存 JSON 文件读取历史 AI 决策
  - 缓存 key：`{symbol}_{timeframe}_{timestamp_str}`
  - 未命中时降级为 RuleOnlyMock

#### 3.2 信号流水线 `backtest/signal/pipeline.py`

- [ ] `class SignalPipeline`：
  - `__init__(config, ai_mock)` 初始化，加载生产配置
  - `generate_signal(symbol, ts, data_feed)` → `dict | None`
    1. 从 data_feed 获取各周期历史数据切片
    2. 调用 `indicator_engine.build_market_snapshot()`
    3. 调用 `indicator_engine.apply_rule_filter()`，未通过返回 None
    4. 调用 `ai_mock.analyze()` 获取决策
    5. 调用 `risk_filter.check_signal_quality()`，未通过返回 None
    6. 调用 `risk_filter.calculate_position_size()`
    7. 返回最终信号 dict

- [ ] 路径处理：将 `scripts/` 目录动态加入 `sys.path`（不修改生产代码）

**验收标准**
- 在完整历史数据上运行一个月，信号输出格式与生产 `execute_from_decision()` 期望格式一致
- 规则过滤率（filtered/total）在合理范围（预期 60%-85% 被过滤）

---

## 五、Phase 4：报告模块

### 目标
将回测引擎输出的 trades 列表和 equity_curve 转换为可读的统计报告和图表。

### 任务清单

#### 5.1 统计计算 `backtest/report/reporter.py`

- [ ] `class BacktestReporter`：
  - `compute_stats(trades, equity_curve, initial_balance)` → `dict`
    - 计算设计文档第八节列出的全部12项指标
    - 按 symbol 分组计算分品种统计
  - `to_csv(trades, equity_curve, output_dir)` → 保存 trades.csv + equity_curve.csv
  - `to_json(stats, output_dir)` → 保存 summary.json

#### 5.2 可视化 `backtest/report/visualizer.py`

- [ ] 权益曲线图（折线 + 回撤区域阴影）
- [ ] 逐笔 PnL 分布直方图
- [ ] 月度收益热力图（12×N 矩阵）
- [ ] 多空方向胜率对比柱状图
- [ ] 参数优化热力图（优化模式专用）
- [ ] `generate_html_report(stats, charts, output_path)` → 生成独立 HTML 文件（内嵌 plotly JS）

**验收标准**
- HTML 报告在浏览器中可正常打开，图表可交互
- summary.json 包含所有12项指标
- trades.csv 包含每笔交易的完整字段（symbol/side/entry/exit/pnl/reason/duration）

---

## 六、Phase 5：参数优化器

### 目标
支持多参数网格搜索，并行运行，输出最优参数组合。

### 任务清单

#### 6.1 优化器 `backtest/optimizer.py`

- [ ] `class GridOptimizer`：
  - `__init__(base_config, param_grid, data_feed)` 初始化
  - `run(n_workers)` → 展开参数网格，用 `multiprocessing.Pool` 并行执行每组参数的完整回测
  - 结果排序：默认按夏普比率降序，支持 `--sort-by` 切换指标
  - 输出 `grid_results.csv`（所有组合结果）+ `top10_params.yaml`
- [ ] **样本外验证**：自动将历史数据按 80/20 切分，优化在前80%，最优参数在后20%验证
- [ ] **Walk-Forward 分析**（可选）：滚动窗口（训练3个月+验证1个月）重复优化验证
- [ ] 参数组合数量预估提示（超过1000组时提示预计耗时）

**验收标准**
- 4核机器上 100 组参数在 1 年 BTC 数据上完成搜索
- 样本外验证报告中最优参数的样本外夏普比率 ≥ 0（不亏损）

---

## 七、Phase 6：集成验证

### 目标
端到端验证回测系统正确性，确保与生产逻辑一致。

### 任务清单

#### 7.1 逻辑一致性验证

- [ ] 选取生产系统已执行的真实交易记录（来自 `logs/trades/`）
- [ ] 用相同时段的历史数据运行回测，对比信号触发时机和入场价格（允许±0.1%偏差）

#### 7.2 边界条件测试

- [ ] 日亏损上限触发后当日不再开仓
- [ ] 最大持仓数满仓时不再开仓
- [ ] 数据缺口（某时段无成交量）不引发除零错误

#### 7.3 与当前配置的基准回测

- [ ] 使用 `config/settings.yaml` 当前参数运行1年基准回测
- [ ] 记录基准指标：胜率、夏普比率、最大回撤、盈利因子
- [ ] 生成基准报告存入 `backtest/results/baseline/`

---

## 八、CLI 入口设计

### `backtest/run_backtest.py` 命令行接口

```bash
# 单次回测（使用当前生产配置）
python backtest/run_backtest.py \
  --symbols BTC/USDT:USDT ETH/USDT:USDT \
  --start 2024-01-01 --end 2024-12-31

# 覆盖部分参数
python backtest/run_backtest.py \
  --start 2024-01-01 --end 2024-12-31 \
  --override adx_trending_threshold=25 min_rr_ratio=2.5

# 参数网格优化
python backtest/run_backtest.py --optimize \
  --start 2024-01-01 --end 2025-06-30 \
  --param-grid backtest/config/param_grid.yaml \
  --workers 4 --sort-by sharpe_ratio

# 仅下载历史数据
python backtest/data/downloader.py \
  --symbols BTC/USDT:USDT ETH/USDT:USDT \
  --timeframes 15m 30m 1h --start 2024-01-01
```

---

## 九、依赖项

新增依赖（需加入 `requirements.txt`）：

```
pyarrow>=14.0        # Parquet 读写
plotly>=5.0          # 交互式图表
jinja2>=3.0          # HTML 报告模板渲染
tqdm>=4.0            # 进度条
```

---

## 十、文件交付清单

### Phase 1
- [ ] `backtest/config/backtest.yaml`
- [ ] `backtest/data/__init__.py`
- [ ] `backtest/data/downloader.py`
- [ ] `backtest/data/feed.py`

### Phase 2
- [ ] `backtest/engine/__init__.py`
- [ ] `backtest/engine/position.py`
- [ ] `backtest/engine/position_manager.py`
- [ ] `backtest/engine/engine.py`

### Phase 3
- [ ] `backtest/signal/__init__.py`
- [ ] `backtest/signal/ai_mock.py`
- [ ] `backtest/signal/pipeline.py`

### Phase 4
- [ ] `backtest/report/__init__.py`
- [ ] `backtest/report/reporter.py`
- [ ] `backtest/report/visualizer.py`
- [ ] `backtest/report/templates/report.html`

### Phase 5
- [ ] `backtest/optimizer.py`
- [ ] `backtest/config/param_grid.yaml`

### Phase 6
- [ ] `backtest/run_backtest.py`

---

## 十一、风险与注意事项

| 风险 | 说明 | 缓解措施 |
|------|------|----------|
| 前视偏差 | 回测中意外使用了未来数据 | `get_history()` 严格以 `end_ts` 为边界，单元测试验证 |
| 过拟合 | 参数在样本内表现好但样本外失效 | 强制样本外验证，关注参数稳定性而非极值 |
| 回测与实盘逻辑漂移 | 生产代码更新后回测未同步 | 回测直接 import 生产模块，不复制代码 |
| OKX 历史数据限制 | 部分合约历史数据不足1年 | 下载时记录实际可用区间，跳过数据不足的合约 |
| 手续费低估 | 实际手续费高于估算 | 默认使用保守 taker 费率 0.05%，可配置调整 |