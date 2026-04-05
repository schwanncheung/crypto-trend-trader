# 文档同步提醒

修改了以下关键文件时，请检查是否需要更新文档：

**修改文件 → 需检查的文档**

| 修改的文件 | 需检查更新 |
|-----------|-----------|
| `scripts/*.py` (新增/删除) | README.md、CLAUDE.md |
| `scripts/indicator_engine.py` | CLAUDE.md（核心算法） |
| `scripts/risk_filter.py` | CLAUDE.md（核心算法） |
| `scripts/dynamic_stop_take_profit.py` | CLAUDE.md（动态止损） |
| `config/settings.yaml` | CLAUDE.md（配置速查） |

**文档职责**：
- `README.md`：面向人类，快速入门、环境配置
- `CLAUDE.md`：面向 AI，架构细节、算法逻辑、代码约定
- `MEMORY.md`：面向 AI（自动加载），精简约定、关键入口

---

## 本地开发约束

**网络无法连接 OKX**，修改后仅需验证语法 + 分析链路影响：

```bash
python -m py_compile scripts/*.py
```

---

## 检查清单

- [ ] 代码中无新增硬编码阈值
- [ ] 新参数已添加到 `settings.yaml`
- [ ] 架构变更已更新 CLAUDE.md
- [ ] 全局约定变更已更新 MEMORY.md
- [ ] 语法检查通过