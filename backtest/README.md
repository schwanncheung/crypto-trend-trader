# 回测系统 (Backtest System)

离线重放真实交易逻辑、量化验证并调优核心参数的完整回测框架。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 下载历史数据

```bash
python backtest/run_backtest.py download \
    --symbols BTC/USDT:USDT ETH/USDT:USDT \
    --timeframes 15m 30m 1h \
    --start 2024-01-01
```

### 3. 运行回测

```bash
python backtest/run_backtest.py backtest \
    --start 2024-01-01 --end 2025-01-01
```

### 4. 参数优化

```bash
python backtest/run_backtest.py optimize \
    --start 2024-01-01 --end 2025-01-01 \
    --workers 4
```

---

## 目录结构

```
backtest/
├── run_backtest.py           # CLI 入口（download / backtest / optimize）
├── config_loader.py          # 配置合并（settings.yaml + backtest.yaml）
├── optimizer.py              # 网格搜索 + OOS 验证
│
├── config/
│   ├── backtest.yaml         # 回测专用参数
│   └── param_grid.yaml       # 网格搜索参数空间
│
├── data/
│   ├── downloader.py         # OKX 历史K线下载，Parquet 缓存
│   ├── feed.py               # DataFeed，无前视偏差切片
│   └── cache/                # Parquet 数据文件（.gitignore）
│
├── engine/
│   ├── engine.py             # bar-by-bar 主循环
│   ├── position.py           # Position 数据类
│   └── position_manager.py   # 止损/止盈/移动止损（纯计算）
│
├── sig/
│   ├── pipeline.py           # 信号流水线，复用生产代码
│   └── ai_mock.py            # RuleOnlyMock / LLMMockCache / LLMRealAnalyzer
│
├── report/
│   ├── reporter.py           # 统计指标 + CSV/JSON/HTML 报告
│   ├── visualizer.py         # Plotly 交互图表
│   └── templates/            # HTML 报告模板
│
└── results/                  # 回测结果（.gitignore）
```

---

## 命令参考

### `download` — 下载历史数据

```bash
python backtest/run_backtest.py download \
    --symbols BTC/USDT:USDT ETH/USDT:USDT \
    --timeframes 15m 30m 1h 4h \
    --start 2024-01-01
```

数据使用 OKX 公开接口，无需 API Key。缓存为 Parquet 格式，支持增量更新。

### `backtest` — 单次回测

```bash
python backtest/run_backtest.py backtest \
    --start 2024-01-01 --end 2025-01-01
```

### `optimize` — 网格搜索

```bash
python backtest/run_backtest.py optimize \
    --workers 4 --top-n 20
```

---

## 配置文件

### `config/backtest.yaml`

| 参数 | 说明 |
|------|------|
| `start_date` / `end_date` | 回测时间区间 |
| `initial_balance` | 初始资金（USDT） |
| `leverage` | 杠杆倍数 |
| `fee_rate` | 手续费率（默认 0.05%） |
| `slippage_pct` | 滑点（默认 0.1%） |
| `signal_interval_bars` | 信号检查间隔（bar 数） |
| `ai_mode` | `rule_only` / `llm_mock` / `llm_real` |

### `config/param_grid.yaml`

定义搜索空间，优化器取笛卡尔积遍历。

---

## AI 替代模式

| 模式 | 说明 |
|------|------|
| `rule_only` | 纯规则引擎，可重复执行，无 API 成本（**推荐**） |
| `llm_mock` | 读取预缓存的 LLM 响应 JSON |
| `llm_real` | 调用真实 LLM API（需 API Key） |

---

## 输出结果

`backtest/results/<YYYYMMDD_HHMMSS>/`：

| 文件 | 内容 |
|------|------|
| `trades.csv` | 逐笔交易记录 |
| `stats.json` | 统计指标 |
| `report.html` | HTML 综合报告 |
| `equity_curve.html` | 权益曲线图 |
| `pnl_bars.html` | PnL 柱状图 |
| `monthly_heatmap.html` | 月度热力图 |

---

## 扩展阅读

| 文档 | 说明 |
|------|------|
| [docs/design.md](docs/design.md) | 系统架构设计 |
| [config/backtest.yaml](config/backtest.yaml) | 回测配置 |
| [config/param_grid.yaml](config/param_grid.yaml) | 参数搜索空间 |