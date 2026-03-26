# crypto-trend-trader — 项目全局记忆文档

> 供 AI 模型快速理解本项目的架构、流程、配置和关键约定。

---

## 项目概述

OKX 永续合约（SWAP）自动交易系统。策略核心：**裸K趋势追踪 + AI分析**，每15分钟运行一次扫描循环。

- 交易所：OKX（ccxt 接入，支持模拟盘/实盘切换）
- 语言：Python 3
- 配置中心：`config/settings.yaml`（业务参数）+ `.env`（API密钥）

---

## 目录结构

```
crypto-trend-trader/
├── config/
│   ├── settings.yaml       # 所有业务参数（唯一配置入口）
│   └── symbols.yaml        # 兜底合约列表（热门扫描失败时使用）
├── scripts/
│   ├── market_scanner.py   # 主调度脚本（入口）
│   ├── fetch_kline.py      # K线获取、热门合约筛选、趋势结构判断
│   ├── indicator_engine.py # 规则引擎：技术指标计算 + 裸K形态 + 市场快照
│   ├── generate_chart.py   # mplfinance 生成多周期K线图 PNG（visual模式）
│   ├── ai_analysis.py      # AI分析：visual模式=视觉LLM，text模式=文本LLM
│   ├── risk_filter.py      # 信号质量过滤 + 账户风控 + 仓位计算
│   ├── execute_trade.py    # OKX 开仓/平仓/止盈止损设置
│   ├── trade_manager.py    # 持仓管理（移动止损、部分止盈、强制平仓）
│   ├── config_loader.py    # 配置加载入口，导出所有 CFG 变量
│   ├── notifier.py         # 飞书 Webhook 通知
│   ├── daily_report.py     # 每日报告
│   └── trade_report.py     # 单笔平仓报告生成
└── logs/
    ├── decisions/          # 决策日志 + K线图存档
    └── trades/             # 交易记录 JSON
```

---

## 主流程（market_scanner.py）

每15分钟执行一次 `main()`，6个步骤：

1. **初始化**：创建 exchange 实例，检查 API 连通性
2. **获取合约列表**：`fetch_hot_symbols()` 按24h成交量降序取前N个，失败则用 `symbols.yaml` 兜底
3. **持仓管理**：`trade_manager.main()` 对已有持仓执行移动止损/部分止盈/强制平仓
4. **日损检查**：`check_daily_loss()` 超过日亏损上限则跳过新开仓
5. **逐标的扫描**：
   - 已有持仓则跳过
   - 已达最大持仓数则停止
   - 拉取多周期K线（`fetch_multi_timeframe`）
   - **text模式**：`indicator_engine` 计算指标快照 → 规则预过滤 → 文本LLM分析
   - **visual模式**：生成K线图PNG → 视觉LLM分析
   - `passes_risk_filter()` 信号质量检查（二次确认）
   - `check_account_risk()` 账户风控
   - `execute_from_decision()` 执行开仓
   - 保存决策日志
6. **扫描完成**：汇总统计，飞书通知

---

## 分析模式

`config/settings.yaml` → `analysis.mode` 控制：

| 模式 | 流程 | 特点 |
|------|------|------|
| `text` | 规则引擎 → 文本LLM | 省token、速度快（**当前默认**） |
| `visual` | 生成K线图PNG → 视觉LLM | 精度更高，消耗更多token |

### text模式 LLM 降级链
- 主力：`glm-5`（阿里云百炼 Coding Plan 接口）
- 兜底：`kimi-k2.5`

### visual模式 LLM 降级链
- 主力：`qwen3-vl-flash`
- 次选：`qwen3-vl-plus`（开启 thinking_mode）
- 兜底：`qwen-vl-max`

---

## 规则引擎（indicator_engine.py）

规则预过滤作为进入LLM分析前的门卫，不通过直接返回 `wait`（节省token）。

**计算指标：**
- EMA(21, 55, 200)
- ADX(14) / +DI / -DI
- RSI(14)
- 成交量MA(5)
- 波段高低点（Swing High/Low）识别

**过滤规则：**
- 锚周期（默认 `timeframes[0]`，当前为 `1h`）趋势必须非横盘
- `require_anchor_aligned: true` → 锚周期方向必须与信号一致
- 至少2个周期趋势方向对齐（`min_trending_timeframes: 2`）
- ADX ≥ 20（趋势明确）
- 成交量 / MA5 ≥ 1.2（量能确认）
- RSI 不在超买(75)/超卖(25)区间

---

## 风控体系（risk_filter.py）

**信号质量检查（`check_signal_quality`）：**
- `confidence` 必须为 `high`
- `signal_strength` ≥ 7/10
- `trend_strength` ≥ 7/10（如AI返回了此字段）
- `volume_confirmed: true`
- R:R ≥ 2.0
- `divergence_risk: false`（无顶底背离）
- `structure_broken: false`（结构未被打破）

**账户风控（`check_account_risk`）：**
- 最大持仓数：3
- 同标的不重复开仓
- 日亏损上限：-5%（`check_daily_loss`）
- 可用余额不足时降低仓位或拒绝

**仓位计算（`calculate_position_size`）：**
- 按固定风险比例（账户余额的百分比）计算合约张数
- 默认杠杆：10x

---

## 持仓管理（trade_manager.py）

每轮扫描时对所有持仓执行：

| 条件 | 动作 |
|------|------|
| 浮盈 ≥ 15% | 移动止损到保本价 |
| 浮盈 ≥ 25% | 部分止盈：平掉30%仓位 |
| 浮盈 ≥ 50% | 再次部分止盈：再平掉50%仓位 |
| 浮亏 ≤ -15% | 强制平仓（兜底，正常止损靠交易所挂单） |
| 支撑/阻力突破 | 趋势反转检测，触发平仓 |

---

## 关键配置（settings.yaml 摘要）

```yaml
timeframes: ["1h", "30m", "15m"]  # 当前配置（高→低，第一个为锚周期）

exchange:
  testnet: true   # 模拟盘（上线前必须先验证全流程）

analysis:
  mode: "text"    # text / visual

trading:
  min_signal_strength: 7
  min_rr_ratio: 2.0

risk:
  max_open_positions: 3
  max_daily_loss_pct: -5.0
  risk_per_trade_pct: 1.0    # 每笔风险占账户比例
  leverage: 10

scanner:
  top_n_symbols: 50
  min_volume_usdt: 40_000_000
  max_price_usdt: 10.0
```

---

## 环境变量（.env）

| 变量名 | 用途 |
|--------|------|
| `OKX_API_KEY` | OKX API Key |
| `OKX_API_SECRET` | OKX Secret |
| `OKX_PASSPHRASE` | OKX Passphrase |
| `DASHSCOPE_API_KEY` | 阿里云百炼（Qwen视觉 + 文本LLM）|
| `OPENAI_API_KEY` | GPT-4o 兜底（visual模式）|
| `FEISHU_WEBHOOK_URL` | 飞书机器人通知（可选）|

---

## 开发注意事项

1. **配置唯一入口**：所有参数改 `config/settings.yaml`，不要在代码里硬编码值。`config_loader.py` 统一导出所有 `*_CFG` 变量供各模块 import。
2. **时间框架自动适配**：修改 `timeframes` 配置后，规则引擎、K线获取、图表生成、Prompt 均自动适配，无需改代码。
3. **时区统一**：所有日志时间戳使用北京时间（CST/UTC+8）。
4. **LLM 输出格式**：AI 必须返回 JSON，包含 `action`（buy/sell/wait）、`signal_strength`（1-10）、`confidence`（high/medium/low）、`entry`、`stop_loss`、`take_profit`、`volume_confirmed`、`divergence_risk`、`structure_broken` 等字段。
5. **模拟盘优先**：`exchange.testnet: true` 为默认，上线实盘前务必完整验证。
6. **decisions 目录**：每笔分析决策保存为 JSON + 可选 PNG，用于复盘和调试。
