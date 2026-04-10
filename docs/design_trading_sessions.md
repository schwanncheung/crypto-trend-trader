# 交易时段配置设计方案

## 一、配置结构设计

### 生产环境：`config/settings.yaml`

```yaml
# ── 交易时段配置 ─────────────────────────────────────────────────────
trading_sessions:
  enabled: true                    # 总开关：false = 不限制，全天候交易
  timezone: "UTC+8"               # 时段描述的时区（仅用于配置可读性，计算时转UTC）

  sessions:
    - name: "asia"
      label: "亚洲时段"
      start_hour: 9               # UTC+8 9:00
      end_hour: 16                # UTC+8 16:00（不含）

    - name: "europe"
      label: "欧洲时段"
      start_hour: 15              # UTC+8 15:00
      end_hour: 23                 # UTC+8 23:00（不含）

    - name: "americas"
      label: "美洲时段"
      start_hour: 22              # UTC+8 22:00
      end_hour: 5                  # UTC+8 次日 5:00（含跨日，end_hour < startHour 表示跨天）
```

### 回测环境：`backtest/config/backtest.yaml`

```yaml
override:
  trading_sessions:               # 回测覆盖交易时段配置
    enabled: true
    sessions:
      - { name: "asia", start_hour: 9, end_hour: 16 }
      - { name: "europe", start_hour: 15, end_hour: 23 }
```

---

## 二、新增核心模块：`scripts/trading_hours.py`

```python
#!/usr/bin/env python3
"""
交易时段判断模块
提供 is_trading_session() 判断当前是否处于交易时段内
支持跨天时段（美洲 22:00-05:00）
"""

import re
from datetime import datetime, timezone, timedelta
from typing import Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_tz_offset(tz: str) -> float:
    """
    将 'UTC+8' -> 8.0, 'UTC-5' -> -5.0
    """
    m = re.search(r'UTC([+-])(\d+)', tz)
    if not m:
        return 0.0
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2))


def _hour_in_session(current_hour: int, start: int, end: int) -> bool:
    """
    判断当前小时是否在给定时段内
    end < start 表示跨天时段（如 22-5）
    """
    if end < start:
        return current_hour >= start or current_hour < end
    return start <= current_hour < end


def is_trading_session(cfg: Optional[dict] = None) -> bool:
    """
    返回当前 UTC 时间是否处于任一交易时段内

    Args:
        cfg: 交易时段配置字典，传 None 则从 config_loader 读取

    Returns:
        True = 在交易时段内，允许交易
        False = 非交易时段，禁止交易
    """
    if cfg is None:
        try:
            from scripts.config_loader import SETTINGS
            cfg = SETTINGS.get("trading_sessions", {})
        except ImportError:
            return True  # 兜底：读不到配置时放行

    # 总开关关闭 = 全天候交易
    if not cfg.get("enabled", False):
        return True

    sessions = cfg.get("sessions", [])
    if not sessions:
        return True  # 未配置时段 = 全天候

    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    utc = _utc_now()
    local = utc + timedelta(hours=tz_offset)
    current_hour = local.hour

    for s in sessions:
        if _hour_in_session(current_hour, s["start_hour"], s["end_hour"]):
            return True

    return False


def get_current_session_label(cfg: Optional[dict] = None) -> str:
    """返回当前所处时段的 label，如 '亚洲时段'，无匹配返回 '非交易时段'"""
    if cfg is None:
        try:
            from scripts.config_loader import SETTINGS
            cfg = SETTINGS.get("trading_sessions", {})
        except ImportError:
            return "未知"

    if not cfg.get("enabled", False):
        return "全天候"

    sessions = cfg.get("sessions", [])
    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    utc = _utc_now()
    local = utc + timedelta(hours=tz_offset)
    current_hour = local.hour

    for s in sessions:
        if _hour_in_session(current_hour, s["start_hour"], s["end_hour"]):
            return s.get("label", s["name"])
    return "非交易时段"


def get_next_session_start(cfg: Optional[dict] = None) -> Optional[datetime]:
    """
    返回下一次交易时段开始的时间（UTC）
    用于 scanner 调度：非交易时段计算出下次开窗时间
    """
    if cfg is None:
        try:
            from scripts.config_loader import SETTINGS
            cfg = SETTINGS.get("trading_sessions", {})
        except ImportError:
            return None

    if not cfg.get("enabled", False):
        return None

    sessions = cfg.get("sessions", [])
    if not sessions:
        return None

    tz_offset = _parse_tz_offset(cfg.get("timezone", "UTC+8"))
    utc = _utc_now()
    local = utc + timedelta(hours=tz_offset)

    # 计算今天内下一个时段开始
    candidates = []
    for s in sessions:
        start_h = s["start_hour"]
        end_h = s["end_hour"]

        # 构造今天的目标时间
        target_today = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
        # 构造明天的时间（用于跨天场景）
        target_tomorrow = target_today + timedelta(days=1)

        if end_h < start_h:  # 跨天时段
            # 如 22-5，今天22点到则今天有效，否则下个明天的22点
            if local.hour < end_h:
                # 还在"昨天"的晚段（如凌晨3点，美洲时段"今天"22点未到）
                candidates.append(target_today)
            else:
                candidates.append(target_today)
        else:
            if local < target_today:
                candidates.append(target_today)
            else:
                candidates.append(target_today + timedelta(days=1))

    if not candidates:
        return None
    next_local = min(candidates)
    # 转回 UTC
    return (next_local - timedelta(hours=tz_offset)).replace(tzinfo=timezone.utc)
```

---

## 三、生产环境：修改 `scripts/market_scanner.py`

在 main() 入口最顶部加时段检查：

```python
def main():
    from trading_hours import is_trading_session

    if not is_trading_session():
        current = datetime.now().strftime("%H:%M")
        logger.info(f"⏭️  当前({current})非交易时段，跳过本轮扫描")
        return

    # 以下为原有逻辑不变
    start_time = datetime.now()
    logger.info("🚀 Market Scanner 启动")
    ...
```

---

## 四、回测系统：修改 `backtest/sig/pipeline.py`

在信号检查前加时段过滤：

```python
# 文件顶部增加导入
from scripts.trading_hours import is_trading_session

# 在 _should_generate_signal() 或逐 bar 循环入口处
# 每个 bar 触发前判断是否在交易时段内
def on_bar(bar, cfg: dict, state: dict) -> Optional[dict]:
    """
    非交易时段的 bar 跳过，不产生信号
    """
    if not is_trading_session(cfg.get("trading_sessions", {})):
        return None  # 非交易时段，无信号

    # 原有信号生成逻辑...
```

或在 `backtest/run_backtest.py` 启动时全局检查：

```python
# run_backtest.py

from scripts.trading_hours import is_trading_session, get_current_session_label

def run(config_path: str, start_date: str, end_date: str):
    cfg = load_backtest_config(config_path)

    session_cfg = cfg.get("override", {}).get("trading_sessions") or cfg.get("trading_sessions", {})

    if not is_trading_session(session_cfg):
        print(f"交易时段未启用，跳过时段过滤")
    else:
        label = get_current_session_label(session_cfg)
        print(f"交易时段模式: {label}")

    # engine 内部每个 bar 都会检查 is_trading_session()
    engine = BacktestEngine(cfg)
    engine.run()
```

---

## 五、`scripts/config_loader.py` 修改

导出 `trading_sessions` 配置：

```python
# 在 load_config() 或现有导出逻辑中追加
SETTINGS = load_settings()
TRADING_SESSIONS_CFG = SETTINGS.get("trading_sessions", {})  # 新增
```

---

## 六、批量优化支持：`backtest/config/param_grid.yaml`

```yaml
# trading_sessions 可参与参数网格优化
param_grid:
  trading_sessions.enabled:
    - true
    - false

  trading_sessions.sessions:
    - [{ name: "asia", start_hour: 9, end_hour: 16 }]
    - [{ name: "asia", start_hour: 8, end_hour: 17 }]
    - [{ name: "europe", start_hour: 15, end_hour: 23 }]
    - [{ name: "americas", start_hour: 22, end_hour: 5 }]
```

---

## 七、涉及改动的文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `config/settings.yaml` | 修改 | 新增 `trading_sessions` 节点 |
| `backtest/config/backtest.yaml` | 修改 | `override.trading_sessions` |
| `scripts/trading_hours.py` | **新增** | 核心判断逻辑 |
| `scripts/market_scanner.py` | 修改 | 入口加 `is_trading_session()` 检查 |
| `scripts/config_loader.py` | 修改 | 导出 `TRADING_SESSIONS_CFG` |
| `backtest/sig/pipeline.py` | 修改 | 信号生成前加时段过滤 |
| `backtest/run_backtest.py` | 修改 | 启动信息打印当前时段 |
| `backtest/config/param_grid.yaml` | 修改 | 批量优化参数支持 |

---

## 八、核心优点

1. **一套配置，生产+回测同时生效** — backtest `override` 机制天然支持
2. **时段判断在 scanner 入口** — 改动最小，现有 cron 调度不变
3. **支持跨天时段** — `end_hour < start_hour` 触发跨天逻辑
4. **param_grid 天然支持** — 可批量测试"是否启用时段"及时段边界对策略的影响
5. **UTC 内部计算** — 避免夏令时等时区混乱
