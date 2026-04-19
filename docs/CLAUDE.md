# CLAUDE.md - AI 助手知识库

> 本文档为 Claude Code 提供项目上下文。详细入门见 [README.md](README.md)

---

## 一、核心架构

```
market_scanner.py (主调度，每5分钟)
    ├── fetch_kline.py       # K线获取、热门合约筛选
    ├── indicator_engine.py  # 指标计算、规则预过滤、市场快照
    ├── ai_analysis.py       # LLM 文本分析（可选纯规则模式）
    ├── risk_filter.py       # 信号质量、账户风控、仓位计算
    ├── execute_trade.py     # 开仓/止损止盈/平仓
    └── notifier.py          # 飞书通知

trade_manager.py (持仓管理，每4分钟)
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
| [indicator_engine.py](scripts/indicator_engine.py) | `rule_engine_filter()`, `compute_timeframe_indicators()`, `detect_momentum_acceleration()`, `detect_momentum_decay()` | 规则引擎核心 |
| [ai_analysis.py](scripts/ai_analysis.py) | `analyze_symbol()`, `passes_risk_filter()` | AI 分析入口 |
| [risk_filter.py](scripts/risk_filter.py) | `calculate_position_size()`, `check_signal_quality()` | 风控过滤 |
| [execute_trade.py](scripts/execute_trade.py) | `execute_from_decision()`, `get_open_positions()` | 交易执行 |
| [trade_manager.py](scripts/trade_manager.py) | `main()`, `_update_trailing_stop()` | 持仓巡检 |
| [dynamic_stop_take_profit.py](scripts/dynamic_stop_take_profit.py) | `calculate_dynamic_stop_loss()`, `calculate_trailing_stop()` | 动态止损/跟踪止损计算 |
| [file_lock.py](scripts/file_lock.py) | `atomic_read_json()`, `atomic_write_json()` | 状态文件原子读写 |
| [circuit_breaker.py](scripts/circuit_breaker.py) | `CircuitBreaker`, `get_llm_circuit_breaker()` | LLM API 熔断器 |

---

## 三、核心算法

### 3.1 规则引擎预过滤 (`indicator_engine.py:rule_engine_filter`)

```
流程：锚周期方向 → 多周期对齐 → 动量加速检测 → RSI保护 → 量能确认 → 时段过滤 → 规则通过

1. 锚周期(1h)必须非横盘（ADX >= 30，R12从20提高到30，减少ADX20-25伪信号）
2. 至少2个小周期与锚周期方向一致
3. 动量加速检测（优化1）：锚周期实体放大 >= 1.5x 加分，衰减 < 0.8x 减分
4. RSI保护（多层，部分可被ADX豁免）：
   - 基础极值（>60禁多，<35禁空，R12做多上限从65降到60）
   - RSI中性偏弱区（40-60）禁止做空（Round 5 分析：7笔全亏）
   - 持续保护（连续超买/超卖，不可豁免）
   - 背离保护（底背离禁空，不可豁免）
5. 至少一个小周期量比 ≥ 0.8
6. 超卖反弹保护、趋势转折预警（小周期RSI连续回升+放量）
7. 时段过滤：全时段开放做空（R21重新开放欧洲时段）
8. 形态做多质量检查（非硬过滤）
9. 规则引擎通过

### 3.1.1 做空质量检查（R21重构：`detect_short_signal_quality`）
做空使用独立的确认逻辑（4层）：
1. 趋势确认：锚周期下跌 + 至少1个小周期也下跌 + ADX >= 40（R21新增）
2. 入场时机：RSI在50-65区间（非超买非超卖）；R21新增近超卖区拦截（RSI < 40）
3. 小周期RSI同步下降（无反弹信号）
4. 无bullish形态冲突（R21新增：检测到hammer/pin_bar_bull等则拒绝做空）
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
# multiplier 随 ADX 动态调整（需启用 stop_loss_adx_scaling.enabled）：
#   ADX < 40:  1.5x（基础）
#   ADX 40-60: 1.8x（强趋势）
#   ADX ≥ 60:  2.25x（极强趋势）

# 止损合理性检查（不强制收缩）：
# 实际上限 = max_stop_loss_pct × max_stop_loss_multiplier
# 默认 1.5% × 1.5 = 2.25%
# 止损距离 < 实际上限 → 使用完整 ATR 止损
# 止损距离 > 实际上限 → 拒绝信号（品种波动异常）

# 止盈最小绝对距离检查：已关闭（2026-04-11）
#   原逻辑：min_take_profit_pct: 1.5% —— 止盈距离 < 1.5% 时拒绝信号
#   问题：低波动环境下有效信号被误杀，过于严格

# ATR 跟踪止损（第一批止盈后启用）：
# 跟踪止损 = 当前价 ± ATR × 1.5（多头减，空头加）
# 仅向有利方向移动，不回退
# 状态持久化：logs/trailing_stop_state.json
```

### 3.4 信号质量检查 (`risk_filter.py:check_signal_quality`)

```python
# 检查项（全部通过才允许交易）：
# 1. 信号方向有效（long/short）
# 2. 置信度 = high
# 3. 信号强度 >= min_signal_strength
# 4. 趋势强度 >= min_trend_strength
# 5. 成交量确认
# 6. 盈亏比 >= min_rr_ratio
# 7. 无背离风险
# 8. 结构未打破
# 9. RSI 极值保护：
#    做多：entry_rsi < rsi_overbought（默认 60，R12从65下调）
#    做空：entry_rsi > rsi_oversold（默认 35）
# 10. RSI 超卖严格模式（rsi_oversold_strict=true）：
#     RSI <= rsi_oversold 时禁止所有做空（裸K逻辑：超卖是反弹结构非趋势延续）
# 11. Bearish Engulfing + RSI 超卖阻断：
#     RSI 超卖区出现看跌吞没 = 强反弹结构，禁止做空
```

### 3.5 仓位计算

```python
contracts = risk_usdt / (止损点数 × 合约面值)
# risk_usdt = 余额 × max_position_pct(15%) × warning_reduction × pattern_boost
```

### 3.6 形态仓位倍数 & 信号强度加权（R20 重构）

```yaml
# R20 重构：配置驱动的双向冲突检测
# pattern_filter 下分 bullish_patterns 和 bearish_patterns 两大类
# bullish_patterns: 做多时加分加仓，做空时惩罚
# bearish_patterns: 做空时加分加仓，做多时惩罚

pattern_filter:
  bullish_patterns:
    patterns: [pin_bar_bull, hammer, bullish_engulfing, morning_star]
    position_boost: 1.2       # 默认仓位倍数
    signal_boost: 1.0         # 默认信号强度加分
    position_boost_per_pattern:
      pin_bar_bull: 2.0       # Pin Bar 多头：仓位+100%（Round 9：3笔全胜 +20.51 U）
      hammer: 1.2             # 锤子线：仓位+20%
      bullish_engulfing: 1.2  # 看涨吞没：仓位+20%
    signal_boost_per_pattern:
      pin_bar_bull: 2.5       # Pin Bar 多头：+2.5
      bullish_engulfing: 0.5
      hammer: 0.5
  bearish_patterns:
    patterns: [pin_bar_bear, bearish_engulfing]
    position_boost_per_pattern:
      pin_bar_bear: 2.0       # Pin Bar 空头：仓位+100%
      bearish_engulfing: 1.2
    signal_boost_per_pattern:
      pin_bar_bear: 2.5
      bearish_engulfing: 0.5
```

**冲突处理矩阵（R20 修复 bug）**：
| 形态方向 | 出现在做多信号 | 出现在做空信号 |
|----------|---------------|---------------|
| bullish | +score + position_boost + signal_boost | **惩罚**（score + penalty） |
| bearish | **惩罚**（score + penalty） | +score + position_boost + signal_boost |

# Bearish Engulfing + RSI 超卖阻断（risk_filter.py 硬过滤）
#   - RSI 超卖区出现看跌吞没 = 强反弹结构，禁止做空

# 做空结构位置要求（settings.yaml → trading.structure_filter）
#   - short_require_near_resistance: 做空需在阻力区 ±5% 以内
#   - short_require_structure_down: 做空需 LH/LL 空头结构满足其一
```

### 3.7 结构平仓 (`fetch_kline.py:detect_trend_structure`)

```
返回字段：
- structure_broken_long:  价格跌破前低 → 多头结构破坏，触发多头平仓
- structure_broken_short: 价格突破前高 → 空头结构破坏，触发空头平仓
- structure_broken:       两者任一为 True（向后兼容字段）

trade_manager 按持仓方向选择对应字段，避免多头创新高时被误平仓
```

### 3.8 持仓管理出场逻辑 (`trade_manager.py`)

```
出场优先级（从高到低）：
1. 强制平仓：浮亏 < -30%（兜底）
2. 动量衰减出场（优化4）：浮盈 >= 5% 且 5m 周期实体连续缩小 + 反向影线
3. 结构平仓：1h 结构破坏 / 支撑阻力突破
止盈流程：
- 浮盈 >= 20%：第一批止盈 40%，剩余启用 ATR 跟踪止损（优化2）
- 浮盈 >= 50%：第二批止盈（全平剩余）
```

--- (`config/settings.yaml`)

```yaml
timeframes: ["1h", "15m", "5m"]

trading:
  enable_open_position: true    # 开仓总开关
  min_signal_strength: 7        # 最低信号强度
  min_rr_ratio: 2.0             # 最低盈亏比（Round 9：RR<1.5 的13笔亏损 -3.44 U，胜率 46%）
  target_rr_ratio: 2.5          # 止盈R倍数
  stop_loss_atr_multiplier: 1.5 # 止损ATR倍数
  max_stop_loss_pct: 1.5        # 止损上限(%)
  max_take_profit_pct: 6.0      # 止盈上限(%)
  trailing_stop_atr_multiplier: 1.5  # 跟踪止损 ATR 倍数

  # 形态仓位倍数
  pattern_position_boost:
    pin_bar_bull: 2.0            # Pin Bar 多头：仓位+100%（Round 9：3笔全胜 +20.51 U，100%胜率）
    hammer: 1.2                  # 锤子线：仓位+20%
    bullish_engulfing: 1.2      # 看涨吞没：仓位+20%
    none: 0.5                    # 无形态信号：仓位降至50%（R11：4笔none亏损-0.51U/笔）

  # 形态信号强度加权（可破格加分）
  pattern_signal_boost:
    pin_bar_bull: 1.5           # 信号强度 +1.5
    pin_bar_bear: 1.5
    bullish_engulfing: 0.5
    hammer: 0.5
    morning_star: 1.0

  # 形态过滤规则
  pattern_filter:
    inside_bar_require_trend: true   # Inside Bar 需趋势背景

  # R21新增：做空保护参数
  short_min_adx: 40              # 做空最低ADX要求（趋势必须够强）
  rsi_short_guard_zone: 40       # RSI低于此值禁止做空（近超卖区35-40）

  # 做空结构位置要求
  structure_filter:
    short_require_near_resistance: true   # 做空需在阻力区附近
    short_resistance_threshold_pct: 0.025
    short_require_structure_down: true     # 做空需 LH/LL 结构

risk:
  max_open_positions: 5         # 最大持仓
  max_loss_pct: -5.0            # 日亏损上限(%)
  stop_loss_cooldown_hours: 4   # 止损冷却期

analysis:
  mode: "text"                  # "text" / "rule_only"
  indicator:
    momentum_accel_ratio: 1.5        # 动量加速阈值（实体放大倍数）
    momentum_decay_lookback: 3       # 动量衰减检测窗口（根）
    momentum_decay_shadow_ratio: 1.0 # 反向影线/实体比阈值
  circuit_breaker:
    failure_threshold: 3        # LLM 连续失败次数熔断
    recovery_window_sec: 300    # 熔断后恢复窗口 (秒)
    fallback_mode: "rule_only"  # 降级模式

trade_manager:
  momentum_decay_exit_enabled: true   # 动量衰减出场开关
  momentum_decay_min_profit_pct: 5.0  # 触发动量衰减出场的最低浮盈(%)
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
| `trailing_stop_state.json` | ATR 跟踪止损激活状态和当前止损价 |
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
| 配置交易时段 | `settings.yaml → trading_sessions`（生产全时段，回测通过 `backtest.yaml → override.trading_sessions` 覆盖） |

---

## 八、回测系统

```bash
python backtest/run_backtest.py download --start 2024-01-01
python backtest/run_backtest.py backtest --start 2024-01-01 --end 2025-01-01
python backtest/run_backtest.py optimize --workers 4
```

**交易时段**：回测与生产共用 `trading_sessions` 配置（backtest override 完整替换，非合集）。
非交易时段的 bar 直接跳过信号生成，详见 `scripts/trading_hours.py`。

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
ln -sf $(pwd)/docs/MEMORY.md ~/.claude/projects/$(pwd | sed 's/\//-/g')/memory/MEMORY.md
```

---

*最后更新：2026-04-07（新增形态仓位倍数配置、收紧止损参数、提高信号质量门槛）*