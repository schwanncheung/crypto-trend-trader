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
| [indicator_engine.py](scripts/indicator_engine.py) | `rule_engine_filter()`, `compute_timeframe_indicators()`, `evaluate_pattern_quality()`, `get_trend_phase()` | 规则引擎核心 + 形态质量/趋势阶段 |
| [ai_analysis.py](scripts/ai_analysis.py) | `analyze_symbol()`, `passes_risk_filter()` | AI 分析入口 |
| [risk_filter.py](scripts/risk_filter.py) | `calculate_position_size()`, `check_signal_quality()`, `check_retest_entry()` | 风控过滤 + 回踩入场 |
| [execute_trade.py](scripts/execute_trade.py) | `execute_from_decision()`, `get_open_positions()` | 交易执行 |
| [trade_manager.py](scripts/trade_manager.py) | `main()`, `_update_trailing_stop()` | 持仓巡检 |
| [dynamic_stop_take_profit.py](scripts/dynamic_stop_take_profit.py) | `calculate_dynamic_stop_loss()`, `calculate_trailing_stop()`, `calculate_structure_based_stop()` | 动态止损/跟踪止损/结构止损 |
| [file_lock.py](scripts/file_lock.py) | `atomic_read_json()`, `atomic_write_json()` | 状态文件原子读写 |
| [circuit_breaker.py](scripts/circuit_breaker.py) | `CircuitBreaker`, `get_llm_circuit_breaker()` | LLM API 熔断器 |

---

## 三、核心算法

### 3.1 规则引擎预过滤 (`indicator_engine.py:rule_engine_filter`)

```
流程：锚周期方向 → 多周期对齐 → 动量衰减硬性拒绝 → RSI保护 → 量能确认 → 时段过滤 → 规则通过

1. 锚周期(1h)必须非横盘（ADX >= 30）
2. 至少2个小周期与锚周期方向一致
3. 动量衰减硬性拒绝：锚周期实体缩小 < 0.8x → 直接拒绝，禁止在趋势末尾追单
4. RSI保护（多层，部分可被ADX豁免）：
   - 基础极值（>60禁多，<35禁空）
   - RSI中性区（40-60）禁止做空
   - 持续保护（连续超买/超卖，不可豁免）
   - 背离保护（底背离禁空，不可豁免）
5. 回调入场过滤（Price Action原则）：
   - 做多：RSI需在30-50区间（回调低位），不在55+追多
   - 做空：RSI需在50-70区间（回调高位），不在45-追空
6. 至少一个小周期量比 ≥ 0.8
7. 超卖反弹保护、趋势转折预警（小周期RSI连续回升+放量）
8. 时段过滤：全时段开放做空
9. 形态做多质量检查（非硬过滤）
10. 规则引擎通过

### 3.1.1 做空质量检查

做空使用独立的确认逻辑（4层）：
1. 趋势确认：锚周期下跌 + 至少1个小周期也下跌 + ADX >= 30
2. 入场时机：RSI在50-65区间（非超买非超卖）；近超卖区拦截（RSI < 45）
3. 小周期RSI同步下降（无反弹信号）
4. 无bullish形态冲突（检测到hammer/pin_bar_bull等则拒绝做空）
```

### 3.2 趋势判断 (`assess_trend_direction`)

```
评分项（满分5分，≥3.5分判趋势）：
- EMA排列（2分）：EMA21>55>200 或反之
- DI方向（1分）：+DI > -DI 或反之
- 价格位置（1分）：价格 vs EMA21
- 近期动能（1分）：最近24根K线方向一致性
```

### 3.3 动态止损

```python
# 止损距离 = ATR × multiplier
# 基础倍数 2.0x，给予更多呼吸空间，减少正常波动被止损
# multiplier 随 ADX 动态调整（需启用 stop_loss_adx_scaling.enabled，2026-06-13 起默认关闭）：
#   ADX < 40:  2.0x（基础）
#   ADX 40-60: 1.2x（强趋势）
#   ADX ≥ 60:  1.5x（极强趋势）

# 止损合理性检查（不强制收缩）：
# 实际上限 = max_stop_loss_pct × max_stop_loss_multiplier
# 默认 2.0% × 1.0 = 2.0%
# 止损距离 < 实际上限 → 使用完整 ATR 止损
# 止损距离 > 实际上限 → 拒绝信号（品种波动异常）

# 止盈最小绝对距离检查：已关闭（2026-04-11）
#   原逻辑：min_take_profit_pct: 1.5% —— 止盈距离 < 1.5% 时拒绝信号
#   问题：低波动环境下有效信号被误杀，过于严格

# ATR 跟踪止损（第一批止盈后启用）：
# 跟踪止损 = 当前价 ± ATR × 2.0
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
#    做多：entry_rsi < rsi_overbought（默认 65）
#    做空：entry_rsi > rsi_oversold（默认 35）
# 10. RSI 超卖严格模式（rsi_oversold_strict=true）：
#     RSI <= rsi_oversold 时禁止所有做空（裸K逻辑：超卖是反弹结构非趋势延续）
# 11. Bearish Engulfing + RSI 超卖阻断：
#     RSI 超卖区出现看跌吞没 = 强反弹结构，禁止做空
# 12. RSI 中性区做空保护：从 rule_filter 配置读取区间（默认 35-60，2026-06-15 起消除硬编码）
#     - 配置项：rsi_neutral_short_ban_lower / rsi_neutral_short_ban_upper
#     - 读取位置：scripts/risk_filter.py → _RSI_NEUTRAL_SHORT_BAN_LOWER / _UPPER
#     - 与 check_pullback_entry 做空 45-70 区间存在重叠（多关卡保护）
# 13. 回调入场过滤（Price Action原则，check_pullback_entry）
#     - 做多最佳区间 RSI 30-55；RSI > 55 追多拦截，RSI < 30 下跌中继拦截
#     - 做空最佳区间 RSI 45-70；RSI < 45 追空拦截，RSI > 70 上涨中继拦截
#     - 调用方：check_signal_quality 末尾（不可被 ADX 强趋势豁免）
```

### 3.5 仓位计算

```python
contracts = risk_usdt / (止损点数 × 合约面值)
# risk_usdt = 余额 × max_position_pct(15%) × warning_reduction × pattern_boost
```

### 3.6 形态仓位倍数 & 信号强度加权

```yaml
# 配置驱动的双向冲突检测
# pattern_filter 下分 bullish_patterns 和 bearish_patterns 两大类
# bullish_patterns: 做多时加分加仓，做空时惩罚
# bearish_patterns: 做空时加分加仓，做多时惩罚

pattern_filter:
  bullish_patterns:
    patterns: [pin_bar_bull, hammer, bullish_engulfing, morning_star]
    position_boost: 1.2       # 默认仓位倍数
    signal_boost: 1.0         # 默认信号强度加分
    position_boost_per_pattern:
      pin_bar_bull: 0.5       # Pin Bar在强趋势中为假信号，降低仓位
      hammer: 0.5            # 锤子形态：最低仓位
      bullish_engulfing: 1.2 # 看涨吞没：仓位+20%
      morning_star: 1.3       # 晨星形态：值得加仓
      inverted_hammer: 0.3    # 倒锤形态：中等仓位
      none: 0.5               # 无形态：保留最低仓位避免无交易
    signal_boost_per_pattern:
      pin_bar_bull: 0.5
      bullish_engulfing: 0.5
      hammer: 0.5
  bearish_patterns:
    patterns: [pin_bar_bear, bearish_engulfing]
    position_boost_per_pattern:
      pin_bar_bear: 0.5
      bearish_engulfing: 1.2
    signal_boost_per_pattern:
      pin_bar_bear: 0.0
      bearish_engulfing: 0.5
```

**冲突处理矩阵**：
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
止盈流程（分两批止盈，锁定利润防止回吐）：
- 浮盈 >= 20%：第一批止盈 30%，剩余启用 ATR 跟踪止损
- 剩余70%仓位：跟踪止损，让利润奔跑
```

### 3.9 回踩入场逻辑 (`risk_filter.py:check_retest_entry`)

```
Price Action 核心原则：只在回撤中做多/反弹中做空。

做多入场时机：价格回撤到支撑或 EMA21 附近（距关键位 <= 1.5 ATR）
做空入场时机：价格反弹到阻力或 EMA21 附近（距关键位 <= 1.5 ATR）

配置（trading.retest_entry）：
- enabled: 是否启用
- max_distance_atr: 价格距关键位的最大距离（ATR 倍数）
- skip_if_not_retrace: 非回踩时是否跳过信号

调用方：market_scanner 风控通过后、执行交易前
- 距关键位 > 1.5 ATR 则跳过该信号（不调仓）
- RSI 回调检查由 check_pullback_entry 独立负责（见 3.4 #13）
```

### 3.10 结构止损 (`dynamic_stop_take_profit.py:calculate_structure_based_stop`)

```
将止损放在结构之外，避免被机构"狩猎"：
- 多头止损：放在 HH（更高高点）外侧 + 缓冲
- 空头止损：放在 LL（更低低点）外侧 + 缓冲
- ATR 止损作为兜底（取两者中较远者）

配置：
- buffer_pct: 止损缓冲（默认5‰）
- prefer_structure: 优先使用结构止损

调用方：execute_from_decision 解析 AI 决策后、仓位计算前
- 需 market_scanner 提前注入 _swing_high / _swing_low / _atr 到 decision
- 验证止损方向（多头SL<入场价、空头SL>入场价）后才覆写原 SL
- 无 swing 数据时返回 None，保持原 ATR 止损
```

### 3.11 形态位置质量评分 (`indicator_engine.py:evaluate_pattern_quality`)

```
Price Action 原则：形态在支撑/阻力位出现 = 高质量信号

质量等级：
- high：形态触及支撑/阻力（5‰内）→ 仓位加成 1.2x
- medium：形态在 EMA21 附近（1%内）→ 仓位不变
- low：形态在趋势中途（远离关键位）→ 仓位折减 0.7x

调用方：execute_trade._calculate_position
- 在原有 pattern_boost 之上叠乘 quality_boost（1.2/1.0/0.7）
- 需 market_scanner 提前注入 _support / _ema21 到 decision
- 无支撑/EMA21 数据时默认为 medium（不调整 boost）
```

### 3.12 趋势阶段判断 (`indicator_engine.py:get_trend_phase`)

```
避免在趋势末期追单，导致止损：
- early_trend：趋势早期，可以入场
- pullback：回撤中（最佳入场机会）
- late_trend：趋势末期，禁止追单
- reversal：可能反转

过滤规则：
- 趋势末期禁止做多（allow_long_in_late_trend=false）
- 回撤中禁止做空（allow_short_in_pullback=false）
```

--- (`config/settings.yaml`)

```yaml
timeframes: ["1h", "15m", "5m"]

trading:
  enable_open_position: true    # 开仓总开关
  min_signal_strength: 7        # 最低信号强度
  min_rr_ratio: 1.8             # 最低盈亏比（2026-05-09 调回 1.8）
  target_rr_ratio: 1.8          # 止盈设置：止盈距离 = 止损距离 × 此倍数
  stop_loss_atr_multiplier: 2.0 # 止损ATR倍数
  max_stop_loss_pct: 2.0        # 止损上限(%)
  max_take_profit_pct: 4.0      # 止盈上限(%)
  trailing_stop_atr_multiplier: 2.0  # 跟踪止损 ATR 倍数

  # 形态仓位倍数
  pattern_position_boost:
    pin_bar_bull: 0.5            # Pin Bar在强趋势中为假信号，降低仓位
    hammer: 0.5                  # 锤子形态：最低仓位
    bullish_engulfing: 1.2      # 看涨吞没：仓位+20%
    none: 0.5                    # 无形态：保留最低仓位避免无交易
    inverted_hammer: 0.3        # 倒锤形态：中等仓位
    morning_star: 1.3           # 晨星形态：值得加仓

  # 形态信号强度加权（可破格加分）
  pattern_signal_boost:
    pin_bar_bull: 0.5
    pin_bar_bear: 0.0
    bullish_engulfing: 0.5
    hammer: 0.5
    morning_star: 1.0

  # 形态过滤规则
  pattern_filter:
    inside_bar_require_trend: true   # Inside Bar 需趋势背景

  # 做空保护参数
  short_min_adx: 30              # 做空最低ADX要求
  rsi_short_guard_zone: 50       # RSI低于此值禁止做空（扩大保护范围）

  # 做空结构位置要求
  structure_filter:
    short_require_near_resistance: true   # 做空需在阻力区附近
    short_resistance_threshold_pct: 0.025
    short_require_structure_down: true     # 做空需 LH/LL 结构

  # 分批止盈
  partial_profit_enabled: true       # 分批止盈开关
  partial_profit_trigger_pct: 20.0   # 止盈触发浮盈(%)
  partial_profit_ratio_1: 0.3        # 止盈比例（30%仓位）
  # 剩余70%仓位：跟踪止损

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
  rule_filter:
    adx_threshold: 30              # ADX趋势判断阈值
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
7. **注释规范**：注释只描述功能含义，禁止包含 P0/P1/P2/R2x 等优先级轮次标记

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
| 添加黑名单 | `symbols.yaml → blacklist`（配置区） + `settings.yaml → trading.pattern_filter`（形态过滤） |
| 切换纯规则模式 | `settings.yaml → analysis.mode: "rule_only"` |
| 紧急关闭开仓 | `settings.yaml → trading.enable_open_position: false` |
| 修改时间框架 | `settings.yaml → timeframes`（同时更新 `analysis.rule_filter.anchor_timeframe`） |
| 配置交易时段 | `settings.yaml → trading_sessions`（生产全时段，回测通过 `backtest.yaml → override.trading_sessions` 覆盖） |
| 配置扫描通知冷却 | `settings.yaml → scanner.scan_summary_cooldown_minutes`（默认60分钟，减少通知轰炸） |

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

### 9.1 一致性检查铁律（2026-06-15 确立）

**核心原则**：代码、策略配置（settings.yaml / symbols.yaml）、CLAUDE.md 文档三者必须时刻保持一致。任何一项变更，必须同步另外两项。

**适用范围**：所有代码、配置、文档修改（无论大小）。

**一致性自检步骤（修改后必走）**：

1. **代码 → 配置**：检查改动的阈值/区间/参数，是否都从配置文件读取（`grep "= [0-9]\+"` 扫描硬编码数字）
2. **配置 → 文档**：检查 `settings.yaml` 新增/修改的字段，CLAUDE.md 对应章节是否同步更新
3. **文档 → 代码**：检查 CLAUDE.md 描述的函数名/字段名/默认参数，是否与代码实际一致
4. **回测 vs 生产配置**：回测通过 `backtest/config/backtest.yaml → override` 覆盖生产配置时，覆盖后的值必须在 CLAUDE.md 有说明

**提交前必跑命令**（必带在 commit 前）：

```bash
# 1. 硬编码扫描
grep -nE "if .*<=|>= [0-9]+(\\.[0-9]+)?\\)" scripts/risk_filter.py scripts/ai_analysis.py scripts/indicator_engine.py

# 2. 配置项与文档字段对比
diff <(grep -E "^[a-z_]+:" config/settings.yaml | sort) <(grep -oE "[a-z_]+" docs/CLAUDE.md | sort -u)

# 3. 关键函数存在性检查
python -c "from scripts.risk_filter import check_signal_quality; print('OK')"
```

**典型反例（2026-06-14 体检发现）**：

| 反例 | 后果 |
|------|------|
| `risk_filter.py:144` 硬编码 `40 <= entry_rsi <= 60`，但 `settings.yaml` 是 35-60 | 配置项形同虚设，参数调整不生效 |
| CLAUDE.md 写 `min_rr_ratio: 2.0`，但 `settings.yaml` 是 1.8 | 文档误导决策 |
| R31 调整（pin_bar_bull 3.5x / none 0.0x）被多次 commit 回滚，文档未同步 | 决策意图 vs 实际执行脱节 |

---

## 十、开发环境设置

**手动创建符号链接以启用 auto memory**：

```bash
cd /path/to/crypto-trend-trader
mkdir -p ~/.claude/projects/$(pwd | sed 's/\//-/g')/memory
ln -sf $(pwd)/docs/MEMORY.md ~/.claude/projects/$(pwd | sed 's/\//-/g')/memory/MEMORY.md
```

---

*最后更新：2026-06-15（修复 RSI 中性区做空保护硬编码 40-60 改读配置，确立一致性检查铁律 9.1）*

*最后更新：2026-06-14（接线 check_pullback_entry / evaluate_pattern_quality / calculate_structure_based_stop，修结构止损函数 min/max 语义反掉 bug）*

*最后更新：2026-06-13（ADX30、ATR2.0x、PinBar0.5x、两批止盈）*

*最后更新：2026-05-11（detect_and_record_stop_loss去重检查移到_save_close_trade_log之前，修复零发送bug）*