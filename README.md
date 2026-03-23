# Crypto Trend Trader
基于裸K趋势追踪策略的加密货币合约自动交易系统（OKX 永续合约）

> 最后更新：2026-03-22

## 核心策略
- 多周期共振确认（4H/1H/15M，可配置）
- 裸K形态识别（吞没线/锤子线/内包线等）
- 成交量确认过滤假突破
- **规则引擎预过滤**：4H锚周期+小周期趋势不一致时直接拒绝，不消耗 token
- **LLM 文本分析**：规则通过后，将结构化指标快照发给文本大模型做最终决策
- 严格风控（凯利准则仓位 + 动态止损 + 已用保证金扣除）
- 飞书 Webhook 实时通知

## 分析模式

```
text 模式（默认，推荐）：
  K线数据 → indicator_engine 计算指标
             → rule_engine_filter 单边趋势过滤
                 ❌ 不通过 → signal=wait，跳过（节省token）
                 ✅ 通过   → 文本快照 → qwen3.5-plus
                                              ↓ 失败
                                          kimi-k2.5（兜底）

visual 模式（可选）：
  K线数据 → 生成各周期PNG图表 → qwen3-vl-flash
                                   ↓ 失败
                               qwen3-vl-plus
                                   ↓ 失败
                               qwen-vl-max
```

模式在 `config/settings.yaml` 的 `analysis.mode` 切换。

## 项目结构
```
crypto-trend-trader/
├── scripts/
│   ├── config_loader.py     # 统一配置加载（settings.yaml + .env）
│   ├── indicator_engine.py  # 规则引擎（指标计算 + 裸K形态 + 趋势过滤 + 快照生成）
│   ├── ai_analysis.py       # 分析入口（text/visual 两种模式统一路由）
│   ├── fetch_kline.py       # OKX K线获取 + 趋势结构判断
│   ├── generate_chart.py    # K线图生成（mplfinance，日志存档用）
│   ├── risk_filter.py       # 账户风控 + 仓位计算
│   ├── execute_trade.py     # 交易执行（OKX conditional algo 止损止盈）
│   ├── trade_manager.py     # 持仓管理（移动止损 / 部分止盈 / 强制平仓）
│   ├── market_scanner.py    # 主调度扫描器（每15分钟）
│   ├── notifier.py          # 飞书 Webhook 通知
│   └── daily_report.py      # 每日报告
├── config/
│   ├── settings.yaml        # 全局配置（所有阈值均可配置，无硬编码）
│   └── symbols.yaml         # 监控合约兜底列表
├── logs/
│   ├── decisions/           # 决策日志 + K线图（PNG）
│   └── trades/              # 交易记录（JSON）
└── .env                     # API密钥（不提交git）
```

## 环境变量配置（.env）
```env
# OKX 合约 API
EXCHANGE_API_KEY=你的OKX_APIKey
EXCHANGE_API_SECRET=你的OKX_APISecret
EXCHANGE_PASSPHRASE=你的OKX_Passphrase

# 阿里云百炼 Coding Plan（文本LLM + 视觉LLM 共用同一个Key）
DASHSCOPE_API_KEY=你的阿里云APIKey

# 飞书 Webhook 通知
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

## 快速开始

### 1. 安装依赖
```bash
cd crypto-trend-trader
pip install -r requirements.txt
# 或使用安装脚本
bash scripts/install_deps.sh
```

### 2. 配置 .env
按上方模板填写所有必要环境变量。

### 3. 检查 settings.yaml
关键配置项：
```yaml
exchange:
  testnet: true          # true=测试网，false=实盘

analysis:
  mode: "text"           # "text"=规则+LLM文本（默认）/ "visual"=视觉LLM

trading:
  default_leverage: 10   # 默认杠杆
  min_signal_strength: 7 # 最低信号强度
  min_rr_ratio: 2.0      # 最低盈亏比

risk:
  max_open_positions: 3  # 最大同时持仓
  max_loss_pct: -5.0     # 日亏损上限（%）
```

### 4. 单步测试
```bash
# 测试K线获取
python scripts/fetch_kline.py

# 测试规则引擎
python scripts/indicator_engine.py

# 测试图表生成
python scripts/generate_chart.py

# 测试AI分析（text模式）
python scripts/ai_analysis.py

# 测试交易执行（仅在测试网）
python scripts/execute_trade.py
```

### 5. 运行主扫描器
```bash
python scripts/market_scanner.py
```

### 6. 定时任务（每15分钟）
```bash
# crontab -e
*/15 * * * * cd /path/to/crypto-trend-trader && python scripts/market_scanner.py
*/5 * * * * cd /path/to/crypto-trend-trader && python scripts/trade_manager.py
0 8  * * * cd /path/to/crypto-trend-trader && python scripts/daily_report.py
```

## 关键参数说明

| 参数 | 默认值 | 配置路径 |
|------|--------|----------|
| 默认杠杆 | 10x | `trading.default_leverage` |
| 最大同时持仓 | 3个 | `risk.max_open_positions` |
| 日亏损上限 | -5% | `risk.max_loss_pct` |
| 强制平仓线 | -10% | `trade_manager.force_close_loss_pct` |
| 移动止损触发 | +15% | `trade_manager.trailing_stop_trigger_pct` |
| 部分止盈触发 | +25% | `trade_manager.partial_profit_trigger_pct` |
| ADX趋势阈值 | 20 | `analysis.rule_filter.adx_trending_threshold` |
| 量比确认阈值 | 1.2x | `analysis.rule_filter.volume_ratio_threshold` |

## 注意事项
- 实盘前务必在测试网充值并验证开仓/止损/止盈全流程
- OKX 合约止损止盈使用 `conditional` algo 订单，非标准 `stop_market`
- 飞书 Webhook 地址配置在 `.env` 的 `FEISHU_WEBHOOK_URL`，勿硬编码
- 日志目录：`logs/decisions/`（图表+决策JSON）、`logs/trades/`（交易记录）
- text 模式下默认不生成图表（`chart.save_in_text_mode: false`），设为 true 可存档 PNG 用于日志回溯
