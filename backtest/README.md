# 回测系统 (Backtest System)

离线重放真实交易逻辑、量化验证并调优核心参数的完整回测框架。

## 目录

- [系统目标](#系统目标)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [快速上手](#快速上手)
- [命令参考](#命令参考)
- [配置说明](#配置说明)
- [输出结果](#输出结果)
- [扩展阅读](#扩展阅读)

---

## 系统目标

通过历史K线数据离线重放真实交易逻辑，量化验证并调优以下核心参数：

| 类别 | 参数示例 |
|------|---------|
| 规则引擎过滤阈值 | ADX ≥ 20、成交量比 ≥ 1.1、RSI 极值 |
| 仓位管理参数 | 移动止损触发点、分批止盈比例 |
| 多周期组合 | 1h/30m/15m vs 4h/1h/15m 等 |
| AI 信号质量阈值 | min_signal_strength、min_rr_ratio |

---

## 项目结构

```
backtest/
├── install.sh                    # 依赖安装脚本
├── run_backtest.py               # CLI 入口（download / backtest / optimize）
├── config_loader.py              # 配置合并（settings.yaml + backtest.yaml + override）
├── optimizer.py                  # GridOptimizer 多进程网格搜索 + OOS 验证
│
├── config/
│   ├── backtest.yaml             # 回测专用参数（时间区间、账户、成本模型等）
│   └── param_grid.yaml           # 网格优化参数搜索空间定义
│
├── data/
│   ├── downloader.py             # OKX 历史K线下载，Parquet 增量缓存
│   ├── feed.py                   # DataFeed，严格无前视偏差（look-ahead free）切片
│   └── cache/                    # 自动生成，Parquet 数据文件（已 .gitignore）
│
├── engine/
│   ├── engine.py                 # bar-by-bar 主循环，多品种时间轴合并
│   ├── position.py               # Position 数据类，含移动止损/分批止盈状态
│   └── position_manager.py       # SL/TP/移动止损/分批止盈/强制平仓（纯计算，无IO）
│
├── signal/
│   ├── pipeline.py               # 8步信号流水线，复用生产代码（indicator_engine / risk_filter）
│   └── ai_mock.py                # RuleOnlyMock（量化评分）| LLMMockCache（预缓存LLM响应）
│
├── report/
│   ├── reporter.py               # 12项统计指标 + CSV / JSON / HTML 报告
│   ├── visualizer.py             # Plotly 交互图表：权益曲线、PnL柱图、月度热力图、饼图
│   └── templates/
│       └── report.html           # 独立 HTML 报告模板
│
└── results/                      # 自动生成，每次回测结果按时间戳归档
    └── YYYYMMDD_HHMMSS/
        ├── trades.csv
        ├── stats.json
        ├── report.html
        └── *.html                # Plotly 交互图表
```

---

## 环境要求

- **Python**：3.10 或以上
- **操作系统**：macOS / Linux（Windows 未测试）
- **网络**：下载数据时需访问 OKX 公开 API（无需 API Key）

### 依赖包

所有依赖均已列入项目根目录 `requirements.txt`：

| 包 | 用途 |
|----|------|
| `ccxt >= 4.0.0` | OKX 历史K线数据获取 |
| `pandas >= 2.0.0` | 数据处理 |
| `pyarrow >= 14.0.0` | Parquet 数据缓存 |
| `plotly >= 5.18.0` | 交互式回测图表 |
| `jinja2 >= 3.1.0` | HTML 报告模板渲染 |
| `tqdm >= 4.66.0` | 下载进度条 |
| `scipy >= 1.11.0` | 统计分析（夏普比率等） |
| `pyyaml >= 6.0` | YAML 配置文件解析 |
| `python-dotenv >= 1.0.0` | .env 环境变量 |
| `numpy >= 1.24.0` | 数值计算 |
| `mplfinance >= 0.12.9` | K线图��成（信号流水线复用） |
| `openai >= 1.0.0` | AI 接口（rule_only 模式不需要） |
| `httpx >= 0.25.0` | 异步 HTTP |

---

## 快速上手

### 第一步：安装依赖

```bash
# 推荐先创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# 方式一：使用安装脚本（自动验证）
bash backtest/install.sh

# 方式二：直接 pip 安装
pip install -r requirements.txt
```

### 第二步：下载历史数据

> 数据下载使用 OKX **公开接口**，无需配置 API Key。

```bash
# 从项目根目录运行
python backtest/run_backtest.py download \
    --symbols BTC/USDT:USDT ETH/USDT:USDT SOL/USDT:USDT \
    --timeframes 15m 1h 4h \
    --start 2024-01-01

# 仅下载到指定日期
python backtest/run_backtest.py download \
    --symbols BTC/USDT:USDT \
    --start 2024-01-01 --end 2025-01-01
```

数据以 Parquet 格式缓存到 `backtest/data/cache/`，支持**增量更新**（重复运行只下载缺失部分）。

### 第三步：运行回测

```bash
# 使用 backtest/config/backtest.yaml 中的默认时间区间
python backtest/run_backtest.py backtest

# 指定时间区间
python backtest/run_backtest.py backtest \
    --start 2024-01-01 --end 2025-01-01
```

回测结束后，终端会打印结果摘要，完整报告保存在 `backtest/results/<timestamp>/`。

### 第四步：参数优化（可选）

```bash
# 网格搜索，80% 训练 / 20% OOS 验证
python backtest/run_backtest.py optimize \
    --start 2024-01-01 --end 2025-01-01 \
    --workers 4 \
    --top-n 20
```

搜索空间在 `backtest/config/param_grid.yaml` 中定义，默认约 1,458 种参数组合。

---

## 命令参考

所有命令均在**项目根目录**下执行。

### `download` — 下载历史数据

```
python backtest/run_backtest.py download [选项]

选项：
  --symbols     合约列表，如 BTC/USDT:USDT ETH/USDT:USDT（空格分隔）
  --timeframes  周期列表，默认 15m 1h 4h（空格分隔）
  --start       起始日期，格式 YYYY-MM-DD
  --end         结束日期，格式 YYYY-MM-DD（默认：今天）
```

### `backtest` — 单次回测

```
python backtest/run_backtest.py backtest [选项]

选项：
  --start       回测起始日期（覆盖 backtest.yaml）
  --end         回测结束日期（覆盖 backtest.yaml）
  --config      回测配置文件路径（默认：backtest/config/backtest.yaml）
  --settings    生产配置文件路径（默认：config/settings.yaml）
  --log-level   日志级别：DEBUG / INFO / WARNING / ERROR（默认：INFO）
```

### `optimize` — 网格搜索优化

```
python backtest/run_backtest.py optimize [选项]

选项：
  --start       回测起始日期
  --end         回测结束日期
  --workers     并行进程数（默认：CPU 核心数 - 1）
  --top-n       训练集 Top-N 组参数进行 OOS 验证（默认：20）
  --config      回测配置文件路径
  --settings    生产配置文件路径
  --log-level   日志级别
```

---

## 配置说明

### `backtest/config/backtest.yaml` — 回测专用参数

```yaml
backtest:
  start_date: "2024-01-01"        # 回测起始日期
  end_date:   "2025-01-01"        # 回测结束日期
  initial_balance: 10000.0        # 初始资金 (USDT)
  leverage: 10                    # 杠杆倍数
  fee_rate: 0.0005                # taker 手续费率（OKX 合约 0.05%）
  slippage_pct: 0.001             # 滑点百分比（0.1%，保守估计）
  signal_interval_bars: 4         # 每隔多少根最低周期 bar 触发信号检查
  ai_mode: "rule_only"            # rule_only | llm_mock
  data_cache_dir: "backtest/data/cache"
  results_dir: "backtest/results"

override:                         # 覆盖 settings.yaml 对应值，用于单次调参
  # min_signal_strength: 7
  # min_rr_ratio: 2.0
```

**`ai_mode` 说明：**

| 模式 | 说明 |
|------|------|
| `rule_only` | 用量化规则构造伪AI决策，可重复执行，无 API 成本（推荐） |
| `llm_mock` | 读取预缓存的 LLM 响应 JSON，用于验证 AI 信号效果 |

### `backtest/config/param_grid.yaml` — 网格搜索空间

定义各参数的候选值列表，优化器取笛卡尔积遍历。默认目标指标：夏普比率。

约束条件：最少 20 笔交易、最大回撤 ≤ 30%、胜率 ≥ 35%。

---

## 输出结果

每次回测结果保存在 `backtest/results/<YYYYMMDD_HHMMSS>/`：

| 文件 | 内容 |
|------|------|
| `trades.csv` | 逐笔交易记录（开仓/平仓/PnL/持仓时长） |
| `stats.json` | 12项统计指标（夏普/Calmar/最大回撤/胜率等） |
| `report.html` | 独立 HTML 综合报告（含图表） |
| `equity_curve.html` | Plotly 交互式权益曲线 |
| `pnl_bars.html` | 逐笔 PnL 柱状图 |
| `monthly_heatmap.html` | 月度收益热力图 |
| `trade_dist.html` | 交易分布饼图 |

**终端摘要示例：**

```
====================================================
  回测结果摘要
====================================================
  净收益        :    1234.56 USDT  (12.35%)
  年化收益      :      24.70 %
  最大回撤      :      -8.23 %
  夏普比率      :       1.842
  Calmar 比率   :       3.001
  总交易次数    :         87
  胜率          :      54.0 %
  盈亏比        :       2.31
  期望值        :      14.19 USDT
  平均持仓      :        183 min
  最大连亏      :          4 笔
====================================================
```

---

## 扩展阅读

| 文档 | 说明 |
|------|------|
| [design.md](design.md) | 系统架构与各模块详细设计方案 |
| [dev-plan.md](dev-plan.md) | 分阶段开发计划与任务拆解 |
| [config/backtest.yaml](config/backtest.yaml) | 回测配置文件（含注释） |
| [config/param_grid.yaml](config/param_grid.yaml) | 网格搜索参数空间定义 |
