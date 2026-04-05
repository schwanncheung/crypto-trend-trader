## 变更类型

- [ ] 🐛 Bug 修复
- [ ] ✨ 新功能
- [ ] 🔧 配置调整
- [ ] 📝 文档更新
- [ ] ♻️ 重构

## 变更说明

<!-- 简述本次修改的内容和原因 -->

## 文档同步检查

修改以下内容时需更新对应文档：

| 修改内容 | 需更新 | 已更新 |
|----------|--------|--------|
| 新增/删除脚本 | README.md、CLAUDE.md | [ ] |
| 修改核心算法 | CLAUDE.md（核心算法章节） | [ ] |
| 新增配置项 | CLAUDE.md（配置速查） | [ ] |
| 修改全局约定 | MEMORY.md | [ ] |
| 修改快速入门 | README.md | [ ] |

**本次是否涉及上述修改**：[ ] 是 [ ] 否

## 测试

**本地开发环境无法连接 OKX**，验证方式：

- [ ] 语法检查通过：`python -m py_compile scripts/*.py`
- [ ] 回测模块检查（如涉及）：`python -m py_compile backtest/**/*.py`

**链路影响分析**（修改以下文件需检查对应模块）：

| 修改文件 | 需检查链路 |
|----------|-----------|
| `indicator_engine.py` | `ai_analysis.py` → `market_scanner.py` |
| `risk_filter.py` | `execute_trade.py` 决策流程 |
| `execute_trade.py` | `trade_manager.py` 持仓管理 |
| `config_loader.py` | 所有导入配置的模块 |

## 风控确认

- [ ] 代码中无硬编码阈值（所有参数从 `settings.yaml` 读取）
- [ ] 新参数已添加到 `settings.yaml` 并写注释
- [ ] 涉及开仓/止损逻辑的修改已在模拟盘验证