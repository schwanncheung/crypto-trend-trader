# CLAUDE.md - AI 助手知识库

> 本文档为 Claude Code 提供项目上下文。详细入门见 [README.md](README.md)

---

## 一、核心架构

```
market_scanner.py (主调度，每15分钟)
    ├── fetch_kline.py       # K线获取、热门合约筛选
    ├── indicator_engine.py  # 指标计算、规则预过滤、市场快照
    ├── ai_analysis.py       # LLM 文本分析（可选纯规则模式）
    ├── risk_filter.py       # 信号质量、账户风控、仓位计算
    ├── execute_trade.py     # 开仓/止损止盈/平仓
    └── notifier.py          # 飞书通知

trade_manager.py (持仓管理，每5分钟)
    ├── 移动止损（浮盈 >20% → 保本位）
    ├── 分批止盈（40%@+20%, 100%@+50%）
    ├── 强制平仓（浮亏 <-30%）
    └── 结构平仓（支撑/阻力突破）
```

---

## 二、关键文件速查

| 文件 | 核心函数/类 | 职责 |
|------|------------|------|
| [config_loader.py](scripts/config_loader.py) | `CFG`, `setup_logging()` | 配置加载入口 |
| [indicator_engine.py](scripts/indicator_engine.py) | `rule_engine_filter()`, `compute_timeframe_indicators()` | 规则引擎核心 |
| [ai_analysis.py](scripts/ai_analysis.py) | `analyze_symbol()`, `passes_risk_filter()` | AI 分析入口 |
| [risk_filter.py](scripts/risk_filter.py) | `calculate_position_size()`, `check_signal_quality()` | 风控过滤 |
| [execute_trade.py](scripts/execute_trade.py) | `execute_from_decision()`, `get_open_positions()` | 交易执行 |
| [trade_manager.py](scripts/trade_manager.py) | `main()` | 持仓巡检 |
| [dynamic_stop_take_profit.py](scripts/dynamic_stop_take_profit.py) | `calculate_dynamic_stop_loss()` | 动态止损计算 |
| [file_lock.py](scripts/file_lock.py) | `atomic_read_json()`, `atomic_write_json()` | 状态文件原子读写 |
| [circuit_breaker.py](scripts/circuit_breaker.py) | `CircuitBreaker`, `get_llm_circuit_breaker()` | LLM API 熔断器 |

---

## 三、核心算法

### 3.1 规则引擎预过滤 (`indicator_engine.py:rule_engine_filter`)

```
流程：锚周期方向 → 多周期对齐 → RSI保护 → 量能确认

1. 锚周期(1h)必须非横盘
2. 至少2个小周期与锚周期方向一致
3. RSI保护（多层，部分可被ADX豁免）：
   - 基础极值（>80禁多，<20禁空）
   - 持续保护（连续超买/超卖，不可豁免）
   - 背离保护（底背离禁空，不可豁免）
4. 至少一个小周期量比 ≥ 0.8
```

### 3.2 趋势判断 (`assess_trend_direction`)

```
评分项（满分5分，≥3分判趋势）：
- EMA排列（2分）：EMA21>55>200 或反之
- DI方向（1分）：+DI > -DI 或反之
- 价格位置（1分）：价格 vs EMA21
- 近期动能（1分）：最近24根K线方向一致性
```

### 3.3 动态止损

```python
# 止损距离 = ATR × multiplier
# multiplier 随 ADX 动态调整：
#   ADX < 40:  2.0x（基础）
#   ADX 40-60: 2.4x（强趋势）
#   ADX ≥ 60:  3.0x（极强趋势）
# 硬性上限：max_stop_loss_pct = 2.0%
```

### 3.4 仓位计算

```python
contracts = risk_usdt / (止损点数 × 合约面值)
# risk_usdt = 余额 × max_position_pct(15%) × warning_reduction
```

### 3.5 结构平仓 (`fetch_kline.py:detect_trend_structure`)

```
返回字段：
- structure_broken_long:  价格跌破前低 → 多头结构破坏，触发多头平仓
- structure_broken_short: 价格突破前高 → 空头结构破坏，触发空头平仓
- structure_broken:       两者任一为 True（向后兼容字段）

trade_manager 按持仓方向选择对应字段，避免多头创新高时被误平仓
```

--- (`config/settings.yaml`)

```yaml
timeframes: ["1h", "30m", "15m"]

trading:
  enable_open_position: true    # 开仓总开关
  min_signal_strength: 6        # 最低信号强度
  min_rr_ratio: 1.5             # 最低盈亏比

risk:
  max_open_positions: 5         # 最大持仓
  max_loss_pct: -5.0            # 日亏损上限(%)
  stop_loss_cooldown_hours: 4   # 止损冷却期

analysis:
  mode: "text"                  # "text" / "rule_only"
  circuit_breaker:
    failure_threshold: 3        # LLM 连续失败次数熔断
    recovery_window_sec: 300    # 熔断后恢复窗口 (秒)
    fallback_mode: "rule_only"  # 降级模式

trading:
  max_slippage_pct: 5.0         # 滑点超过此值重新计算止损止盈
  max_margin_usage_ratio: 0.5   # 保证金占可用余额上限
```

---

## 五、代码约定

1. **配置优先**：所有阈值通过 `settings.yaml`，禁止硬编码
2. **合约格式**：OKX 永续为 `BTC/USDT:USDT`
3. **止损单**：`conditional` 类型，`slOrdPx: "-1"` 市价触发
4. **时区**：使用 `config_loader.now_cst()` 获取北京时间
5. **日志**：使用 `setup_logging(module_name)` 初始化
6. **本地开发**：网络无法连接 OKX，修改后验证语法 + 分析链路影响

### 本地开发验证流程

```bash
# 1. 语法检查
python -m py_compile scripts/*.py

# 2. 如涉及回测模块
python -m py_compile backtest/**/*.py
```

**链路影响分析清单**：
- 修改 `indicator_engine.py` → 检查 `ai_analysis.py`、`market_scanner.py`
- 修改 `risk_filter.py` → 检查 `execute_trade.py` 决策流程
- 修改 `execute_trade.py` → 检查 `trade_manager.py` 持仓管理
- 新增配置项 → 检查 `config_loader.py` 导出变量

---

## 六、状态文件 (`logs/`)

| 文件 | 用途 |
|------|------|
| `stop_loss_cooldown.json` | 止损冷却记录 |
| `breakeven_state.json` | 保本位状态 |
| `partial_profit_state.json` | 分批止盈状态 |
| `position_snapshot.json` | 持仓快照（检测止损触发） |

**状态文件读写**：使用 `file_lock.py` 中的 `atomic_read_json()`、`atomic_write_json()`、`atomic_update_json()` 保证原子性

---

## 七、常见修改场景

| 场景 | 修改位置 |
|------|----------|
| 调整信号强度阈值 | `settings.yaml → trading.min_signal_strength` |
| 添加黑名单 | `settings.yaml → blacklist` |
| 切换纯规则模式 | `settings.yaml → analysis.mode: "rule_only"` |
| 紧急关闭开仓 | `settings.yaml → trading.enable_open_position: false` |
| 修改时间框架 | `settings.yaml → timeframes`（同时更新 `analysis.rule_filter.anchor_timeframe`） |

---

## 八、回测系统

```bash
python backtest/run_backtest.py download --start 2024-01-01
python backtest/run_backtest.py backtest --start 2024-01-01 --end 2025-01-01
python backtest/run_backtest.py optimize --workers 4
```

详见 [backtest/docs/design.md](backtest/docs/design.md)

---

## 九、文档维护检查清单

修改以下内容时，需同步更新对应文档：

| 修改内容 | 需更新 |
|----------|--------|
| 新增/删除脚本 | README.md（目录结构）、CLAUDE.md（关键文件表） |
| 修改核心算法 | CLAUDE.md（核心算法章节） |
| 新增配置项 | CLAUDE.md（配置速查） |
| 修改全局约定 | MEMORY.md |
| 修改快速入门流程 | README.md |
| 新增常见场景 | CLAUDE.md（常见修改场景） |

**提交前检查**：
- [ ] 代码中无硬编码阈值
- [ ] 新参数已添加到 `settings.yaml` 并写注释
- [ ] 涉及架构变更已更新 CLAUDE.md

---

## 十、开发环境设置

**手动创建符号链接以启用 auto memory**：

```bash
cd /path/to/crypto-trend-trader
mkdir -p ~/.claude/projects/$(pwd | sed 's/\//-/g')/memory
ln -sf $(pwd)/.claude/MEMORY.md ~/.claude/projects/$(pwd | sed 's/\//-/g')/memory/MEMORY.md
```

---

*最后更新：2026-04-05（新增 3.5 结构平仓说明）*