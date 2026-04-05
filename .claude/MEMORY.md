# Crypto Trend Trader - Project Memory

OKX 永续合约自动交易系统。详细文档见 [CLAUDE.md](CLAUDE.md)

## 全局约定

1. **配置优先**：所有阈值通过 `config/settings.yaml` 配置，禁止硬编码
2. **合约格式**：OKX 永续格式为 `BTC/USDT:USDT`
3. **止损单**：使用 `conditional` 类型，`slOrdPx: "-1"` 市价触发
4. **时区**：统一使用北京时间（CST），通过 `config_loader.now_cst()` 获取
5. **日志**：使用 `config_loader.setup_logging(module_name)` 初始化
6. **本地开发**：网络无法连接 OKX，修改后验证语法 + 分析链路影响

## 关键文件

| 文件 | 职责 |
|------|------|
| `market_scanner.py` | 主调度（每15分钟） |
| `indicator_engine.py` | 规则引擎预过滤 |
| `ai_analysis.py` | LLM 分析入口 |
| `trade_manager.py` | 持仓管理（每5分钟） |
| `config/settings.yaml` | 唯一配置入口 |

## 当前配置

- 时间框架：`["1h", "30m", "15m"]`
- 分析模式：`text`（规则 + LLM）
- 默认杠杆：10x
- 最大持仓：5
