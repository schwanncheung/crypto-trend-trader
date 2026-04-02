# OpenClaw Skills

这些脚本专为 OpenClaw 调用设计，提供快速执行常见交易操作的能力。

## 可用技能

### 1. 市场扫描 (scan_market.py)
执行一次完整的市场扫描，包括热门合约筛选、AI分析、风控、开仓。

```bash
python skills/scan_market.py
```

**功能**:
- 获取 OKX 热门合约（成交量 > 5000万 USDT，价格 < 2 USDT）
- 多周期趋势分析（4h/1h/15m）
- AI 信号生成 + 风控过滤
- 自动开仓（最多3个持仓）
- 飞书通知

**适用场景**: 定时任务、手动触发扫描

---

### 2. 指定合约扫描 (scan_symbol.py)
对单个合约进行深度分析并尝试开仓。

```bash
python skills/scan_symbol.py <SYMBOL>
```

**示例**:
```bash
python skills/scan_symbol.py BTC/USDT:USDT
python skills/scan_symbol.py ETH/USDT:USDT
```

**功能**:
- 检查是否已持有该合约
- 获取多周期K线数据
- AI 分析 + 风控
- 自动开仓
- 飞书通知

**适用场景**: 
- 用户指定合约分析
- 补充扫描遗漏的合约
- 测试特定品种

---

### 3. 生成交易报告 (generate_report.py)
生成今日交易统计报告。

```bash
python skills/generate_report.py
```

**功能**:
- 统计今日交易次数、胜率、盈亏
- AI 模型表现分析
- 当前持仓状态
- 保存报告到 `logs/reports/`
- 飞书通知

**适用场景**: 每日定时报告、手动查看当日表现

---

## OpenClaw 集成示例

在 OpenClaw 配置中添加这些技能：

```yaml
skills:
  - name: "扫描市场"
    command: "cd /Users/schwann/Projects/crypto-trend-trader && python skills/scan_market.py"
    description: "执行一次完整的合约市场扫描"
  
  - name: "扫描合约"
    command: "cd /Users/schwann/Projects/crypto-trend-trader && python skills/scan_symbol.py {symbol}"
    description: "扫描指定合约，参数: symbol (如 BTC/USDT:USDT)"
  
  - name: "生成报告"
    command: "cd /Users/schwann/Projects/crypto-trend-trader && python skills/generate_report.py"
    description: "生成今日交易报告"
```

## 注意事项

1. **环境变量**: 确保 `.env` 文件配置正确（OKX API、通义千问 API、飞书 Webhook）
2. **权限**: 脚本需要可执行权限 `chmod +x skills/*.py`
3. **日志**: 所有日志输出到 `logs/` 目录
4. **通知**: 执行结果会通过飞书 Webhook 发送通知
5. **风控**: 自动遵守 `config/settings.yaml` 中的风控规则

## 退出码

- `0`: 执行成功
- `1`: 执行失败（异常或错误）

OpenClaw 可以根据退出码判断执行状态。
