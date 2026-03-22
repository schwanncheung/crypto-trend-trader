# Crypto Trend Trader
基于裸K趋势追踪策略的加密货币合约自动交易系统（OKX 合约）

> 最后更新：2026-03-22｜完成 P0/P1/P2 全量修复与功能迭代

## 核心策略
- 多周期共振确认（日线/4H/1H/15M）
- 裸K形态识别（吞没线/锤子线/内包线等）
- 成交量确认过滤假突破
- 日线趋势预过滤（横盘合约不进入AI分析）
- AI视觉分析（qwen3-vl-flash 首选，qwen3-vl-plus 降级，qwen-vl-max 兜底）
- 严格风控（凯利准则仓位 + 动态止损 + 已用保证金扣除）
- 飞书 Webhook 实时通知

## 项目结构
```
crypto-trend-trader/
├── scripts/
│   ├── config_loader.py     # 统一配置加载（settings.yaml + .env）
│   ├── notifier.py          # 飞书通知模块
│   ├── fetch_kline.py       # K线数据获取（OKX swap）
│   ├── generate_chart.py    # K线图生成（mplfinance，中文字体已修复）
│   ├── ai_analysis.py       # AI视觉分析（三级降级）
│   ├── risk_filter.py       # 风控过滤
│   ├── execute_trade.py     # 交易执行（OKX conditional algo止损止盈）
│   ├── trade_manager.py     # 持仓管理（纯价格结构判断，无AI重分析）
│   ├── market_scanner.py    # 主调度扫描器
│   └── daily_report.py      # 每日报告
├── config/
│   ├── settings.yaml        # 全局配置（含trading/risk/ai/exchange节点）
│   └── symbols.yaml         # 监控合约列表（兜底用）
├── logs/
│   ├── decisions/           # AI决策日志 + K线图（PNG）
│   └── trades/              # 交易记录（JSON）
└── .env                     # API密钥（不提交git）
```

## 环境变量配置（.env）
```env
# OKX 合约 API
EXCHANGE_API_KEY=你的OKX_APIKey
EXCHANGE_API_SECRET=你的OKX_APISecret
EXCHANGE_PASSPHRASE=你的OKX_Passphrase

# 阿里云灵积（Qwen VL）
DASHSCOPE_API_KEY=你的阿里云APIKey

# 飞书 Webhook 通知
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# AI分析开关（false=AI模式，true=规则引擎模式）
SKIP_AI_ANALYSIS=false
```

## 快速开始

### 1. 安装依赖
```bash
cd crypto-trend-trader
pip install -r requirements.txt
```

### 2. 配置 .env
按上方模板填写所有必要环境变量

### 3. 检查 settings.yaml
- `exchange.testnet: true` → 测试网模式（建议先用）
- `exchange.testnet: false` → 实盘模式
- `trading.default_leverage` → 默认杠杆倍数
- `risk.max_loss_pct` → 日亏损上限（正数，如 5.0 = 5%）

### 4. 单步测试
```bash
# 测试K线获取（OKX）
python scripts/fetch_kline.py

# 测试图表生成
python scripts/generate_chart.py

# 测试AI分析
python scripts/ai_analysis.py

# 测试风控
python scripts/risk_filter.py

# 测试交易所连接与余额
python scripts/execute_trade.py

# 手动触发一次完整扫描
python scripts/market_scanner.py
```

### 5. 自动运行（cron）
```bash
# 每15分钟扫描市场
*/15 * * * * cd /path/to/crypto-trend-trader && python scripts/market_scanner.py

# 每15分钟管理持仓
*/15 * * * * cd /path/to/crypto-trend-trader && python scripts/trade_manager.py

# 每天00:05生成日报
5 0 * * * cd /path/to/crypto-trend-trader && python scripts/daily_report.py
```

## 运行逻辑（market_scanner.py）
```
第一步：OKX 动态获取成交量TOP20热门合约
第二步：日线趋势预过滤（剔除横盘合约，节省AI调用）
第三步：日亏损预检（超5%停止当日交易）
第四步：持仓数量预检（已满3个跳过开新仓）
第五步：持仓健康检查（浮亏超10%强制平仓）
第六步：逐合约扫描
  → 多周期K线获取（15m/1h/4h/1d）
  → K线图生成（mplfinance）
  → AI视觉分析（qwen3-vl-flash → plus → max）
  → 风控过滤（信号强度/置信度/RR/成交量/结构）
  → 仓位计算（扣除已用保证金，50%安全上限）
  → OKX 市价开仓 + conditional algo 止损止盈
第七步：飞书通知发送扫描汇总
```

## 持仓管理逻辑（trade_manager.py）
```
纯价格结构判断（无AI调用，响应快）：
- 浮盈 >15%：止损移至保本位
- 浮盈 >25%：平掉50%仓位部分止盈
- 浮亏 >10%：强制全平
- 1H结构破坏 / 跌破支撑（做多）/ 突破阻力（做空）：结构平仓
```

## 风控参数
| 规则 | 参数 | 配置位置 |
|------|------|----------|
| 单仓最大风险 | 1% 账户余额 | risk.risk_per_trade_pct |
| 保证金安全上限 | 可用余额50% | execute_trade.py 硬编码 |
| 最低风险回报比 | 1:2 | trading.min_rr_ratio |
| 最低信号强度 | 7/10 | trading.min_signal_strength |
| 最大同时持仓 | 3个 | risk.max_positions |
| 日亏损上限 | 5% | risk.max_loss_pct |
| 强制平仓线 | -10% | trade_manager 硬编码 |
| 移动止损触发 | +15% | trade_manager 硬编码 |
| 部分止盈触发 | +25% | trade_manager 硬编码 |

## AI模型调用链（2026-03-22调整）
```
qwen3-vl-flash（首选，快速，无thinking）
    ↓ 失败/超时
qwen3-vl-plus（降级，thinking模式，精准）
    ↓ 失败/超时
qwen-vl-max（兜底，稳定）
```

## 已完成修复（2026-03-22）
| 级别 | 内容 |
|------|------|
| P0 | 修复 TRADING_CFG 未定义（NameError） |
| P0 | 修复 fetch_kline 错用 Binance 交易所，统一改为 OKX |
| P0 | 修复 OKX 止损止盈下单方式（stop_market → conditional algo） |
| P1 | 趋势预过滤前置，AI分析前剔除横盘合约 |
| P1 | 持仓管理去掉AI重分析，改为纯价格结构判断 |
| P1 | 仓位计算扣除已用保证金，加50%安全上限 |
| P2 | 日亏损检查增加异常保护，兼容OKX余额格式 |
| P2 | 接入飞书 Webhook 实时通知（notifier.py） |
| P2 | 图表中文字体彻底修复（mplfinance rc字典注入） |
| P2 | AI降级顺序调整：flash → plus → max |

## 注意事项
- 实盘前务必在测试网充值并验证开仓/止损/止盈全流程
- OKX 合约止损止盈使用 `conditional` algo 订单，非标准 `stop_market`
- 飞书 Webhook 地址配置在 `.env` 的 `FEISHU_WEBHOOK_URL`，勿硬编码
- `SKIP_AI_ANALYSIS=true` 可切换为规则引擎模式（不调用AI，适合调试）
- 日志目录：`logs/decisions/`（图表+决策JSON）、`logs/trades/`（交易记录）
