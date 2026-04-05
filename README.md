# 加密货币自动交易系统

> 目标：OKX 永续合约 | 核心算法：裸K趋势追踪 + AI 辅助决策

## 快速开始

### 1. 安装

```bash
cd crypto-trend-trader
pip install -r requirements.txt
```

### 2. 配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 填入 API 密钥
# - OKX: EXCHANGE_API_KEY, EXCHANGE_API_SECRET, EXCHANGE_PASSPHRASE
# - 阿里云: DASHSCOPE_API_KEY
# - 飞书: FEISHU_WEBHOOK_URL（可选）
```

### 3. 运行

```bash
# 单次扫描
python scripts/market_scanner.py

# 持仓管理
python scripts/trade_manager.py

# 日报
python scripts/daily_report.py
```

### 4. 定时任务

```bash
# crontab -e
*/5 * * * * cd /path/to/crypto-trend-trader && python scripts/market_scanner.py
*/4  * * * * cd /path/to/crypto-trend-trader && python scripts/trade_manager.py
0 8  * * * cd /path/to/crypto-trend-trader && python scripts/daily_report.py
```

## 配置文件

| 文件 | 用途 |
|------|------|
| `config/settings.yaml` | 主配置：交易参数、风控阈值、分析模式 |
| `config/symbols.yaml` | 优先扫描合约列表 |
| `.env` | API 密钥（不提交 Git） |

## 回测

```bash
# 下载历史数据
python backtest/run_backtest.py download --start 2024-01-01

# 运行回测
python backtest/run_backtest.py backtest --start 2024-01-01 --end 2025-01-01

# 参数优化
python backtest/run_backtest.py optimize --workers 4
```

## 核心特性

- **多周期共振**：1h/15m/5m 三周期趋势确认
- **规则引擎预过滤**：EMA 排列 + ADX 趋势 + RSI 保护
- **AI 辅助决策**：规则通过后，LLM 文本分析（可切换纯规则模式）
- **动态止损**：根据 ADX 强度自动调整止损距离
- **多层风控**：日亏损上限、持仓上限、止损冷却期

## 文档

- **[CLAUDE.md](docs/CLAUDE.md)** — AI 助手知识库（架构细节、算法逻辑、代码约定）
- **[backtest/docs/design.md](backtest/docs/design.md)** — 回测系统架构设计

## 免责声明

本系统仅供学习研究，请勿用于实际投资。加密货币合约交易风险极高，可能导致本金全部损失。

---

*配置详见 `config/settings.yaml`，修改前请仔细阅读注释*