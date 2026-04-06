# Crypto Trend Trader - Project Memory

OKX 永续合约自动交易系统。详细文档见 [CLAUDE.md](CLAUDE.md)

## 全局约定

1. **MEMORY.md 内容**：仅记录广泛适用的全局约定，保持简短
2. **配置优先**：所有阈值通过 `config/settings.yaml` 配置，禁止硬编码
3. **合约格式**：OKX 永续格式为 `BTC/USDT:USDT`
4. **止损单**：使用 `conditional` 类型，`slOrdPx: "-1"` 市价触发
5. **时区**：统一使用北京时间（CST），通过 `config_loader.now_cst()` 获取
6. **日志**：使用 `config_loader.setup_logging(module_name)` 初始化
7. **本地开发**：网络无法连接 OKX，修改后验证语法+分析链路影响
8. **提交代码**：提交代码前读取 `.github/*.md` 进行 check list 检查

## 关键文件

| 文件 | 职责 |
|------|------|
| `market_scanner.py` | 主调度（每15分钟） |
| `fetch_kline.py` | K线获取 |
| `indicator_engine.py` | 规则引擎预过滤 |
| `ai_analysis.py` | LLM 分析入口 |
| `trade_manager.py` | 持仓管理（每4分钟） |
| `stop_loss_tracker.py` | 止损止盈冷却记录 |
| `config/settings.yaml` | 主配置入口 |
| `config/symbols.yaml` | 白名单+黑名单配置 |

## 当前配置

- 时间框架：`["1h", "15m", "5m"]`
- 分析模式：`text`（规则 + LLM）
- 默认杠杆：10x
- 最大持仓：3
