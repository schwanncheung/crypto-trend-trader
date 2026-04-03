# 止损优化方案实施文档

## 问题分析

### CORE 合约反复开仓止损案例
- **时间**：2026-04-03 01:30 - 11:15（10小时内）
- **操作**：3次开仓，3次止损
- **亏损**：每次止损后立即重新开仓，形成"止损-开仓-止损"循环

### 根本原因
1. **止损距离过近**：3-6%，在 ADX 45-50 的强趋势中不够
2. **缺少冷却机制**：止损后立即重新扫描，只要趋势仍向下就再次开仓
3. **未考虑波动率**：CORE 当天跌幅 -12%，高波动环境下固定止损距离不合理

---

## 解决方案

### 方案一：止损冷却机制 ✅

#### 实现逻辑
1. **持仓快照追踪**：每轮扫描保存持仓快照
2. **止损检测**：对比前后快照，检测持仓消失事件
3. **智能判断**：
   - 亏损状态消失 → 推断为止损触发，记录冷却
   - 盈利状态消失 → 推断为止盈/手动平仓，不记录冷却
4. **冷却期拦截**：开仓前检查冷却期，未过期则拒绝开仓

#### 触发场景
- **自动检测**：交易所止损单触发（价格达到止损价）
- **手动记录**：
  - `trade_manager` 强制平仓（浮亏超限）
  - `trade_manager` 结构平仓（支撑/阻力突破）
  - `market_scanner` 紧急风控

#### 配置参数
```yaml
# config/settings.yaml
risk:
  stop_loss_cooldown_hours: 4  # 止损后冷却时间（小时）
```

#### 新增文件
- [scripts/stop_loss_tracker.py](scripts/stop_loss_tracker.py) - 冷却追踪模块

#### 修改文件
- [scripts/market_scanner.py:96-109](scripts/market_scanner.py#L96-L109) - 集成持仓快照和止损检测
- [scripts/risk_filter.py:65-101](scripts/risk_filter.py#L65-L101) - 增加冷却期检查
- [scripts/trade_manager.py:288-299](scripts/trade_manager.py#L288-L299) - 强制平仓时记录冷却
- [scripts/trade_manager.py:347-355](scripts/trade_manager.py#L347-L355) - 结构平仓时记录冷却

---

### 方案二：动态止损（ADX 自适应） ✅

#### 实现逻辑
根据 ADX 强度自动调整止损距离：
- **正常趋势**（ADX < 40）：基础倍数 2.5
- **强趋势**（40 ≤ ADX < 60）：基础倍数 × 1.5 = 3.75
- **极强趋势**（ADX ≥ 60）：基础倍数 × 2.0 = 5.0

#### 配置参数
```yaml
# config/settings.yaml
trading:
  stop_loss_atr_multiplier: 2.5  # ATR 基础倍数
  stop_loss_adx_scaling:
    enabled: true                # 是否启用 ADX 动态调整
    strong_trend_threshold: 40   # 强趋势阈值
    strong_trend_multiplier: 1.5 # 强趋势倍数
    extreme_trend_threshold: 60  # 极强趋势阈值
    extreme_trend_multiplier: 2.0  # 极强趋势倍数
```

#### 新增文件
- [scripts/dynamic_stop_loss.py](scripts/dynamic_stop_loss.py) - 动态止损计算模块

#### 修改文件
- [scripts/ai_analysis.py:124-130](scripts/ai_analysis.py#L124-L130) - 使用动态止损计算
- [config/settings.yaml:133-141](config/settings.yaml#L133-L141) - 新增动态止损配置

---

## 效果预期

### CORE 案例对比

#### 优化前
- 第1次：入场 0.02382，止损 0.02457（+3.1%），ADX=45
- 第2次：入场 0.02379，止损 0.02529（+6.3%），ADX=50
- 第3次：入场 0.02389，止损 0.02539（+6.3%），ADX=50

#### 优化后
- **第1次**：入场 0.02382，止损 0.02457 × 1.5 = 0.02495（+4.7%），ADX=45
  - 止损距离增加 50%，更能容忍强趋势波动
- **第2次**：❌ 被冷却机制拦截
  - 原因：距离第1次止损不足 4 小时
- **第3次**：❌ 被冷却机制拦截
  - 原因：距离第1次止损不足 4 小时

**结果**：避免了 2 次无效开仓，减少 66% 的交易成本

---

## 使用说明

### 查看冷却状态
```bash
cat logs/stop_loss_cooldown.json
```

### 手动清除冷却
```python
from scripts.stop_loss_tracker import clear_cooldown
clear_cooldown("CORE/USDT:USDT")
```

### 调整冷却时间
修改 [config/settings.yaml:140](config/settings.yaml#L140)：
```yaml
risk:
  stop_loss_cooldown_hours: 6  # 改为 6 小时
```

### 禁用动态止损
修改 [config/settings.yaml:136](config/settings.yaml#L136)：
```yaml
trading:
  stop_loss_adx_scaling:
    enabled: false  # 禁用，恢复固定倍数
```

---

## 注意事项

1. **冷却期不是万能的**：如果市场真的出现新的交易机会，冷却期可能会错过
2. **动态止损增加风险敞口**：极强趋势下止损距离翻倍，单笔最大亏损也会增加
3. **需要回测验证**：建议在模拟盘运行 1-2 周，观察效果后再上实盘
4. **持仓快照依赖**：如果系统重启，上次持仓快照会丢失，首次扫描无法检测止损

---

## 后续优化建议

1. **RSI 超卖保护加强**：提高 `rsi_adx_exemption_threshold` 从 40 到 50
2. **成交量确认加严**：在强趋势中要求更高的量比确认
3. **回测验证**：使用历史数据验证冷却期和动态止损的实际效果
4. **监控告警**：冷却期拦截次数过多时发送通知，可能需要调整参数
