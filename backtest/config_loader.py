"""
backtest/config_loader.py

加载并合并配置：
  1. 读取生产配置 config/settings.yaml
  2. 读取回测配置 backtest/config/backtest.yaml
  3. 将 backtest.override 节的值覆盖到对应生产参数
  4. 返回统一的 config dict 供所有模块使用
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent  # crypto-trend-trader/


def load_config(
    backtest_yaml: str | Path | None = None,
    settings_yaml: str | Path | None = None,
) -> dict:
    """
    加载并合并配置。

    Parameters
    ----------
    backtest_yaml : 回测配置路径，默认 backtest/config/backtest.yaml
    settings_yaml : 生产配置路径，默认 config/settings.yaml

    Returns
    -------
    dict — 合并后的完整配置
    """
    if settings_yaml is None:
        settings_yaml = _PROJECT_ROOT / "config" / "settings.yaml"
    if backtest_yaml is None:
        backtest_yaml = _PROJECT_ROOT / "backtest" / "config" / "backtest.yaml"

    production = _load_yaml(Path(settings_yaml), label="settings.yaml")
    backtest = _load_yaml(Path(backtest_yaml), label="backtest.yaml")

    # 合并：backtest 节全量保留；override 节覆盖生产参数
    config = dict(production)  # shallow copy of top-level
    config["backtest"] = backtest.get("backtest", {})

    overrides = config["backtest"].get("override", {})
    if overrides:
        logger.info("[config_loader] 应用 override 参数：%s", list(overrides.keys()))
        _deep_override(config, overrides)

    # 环境变量注入（优先级最高）
    _inject_env_vars(config)

    logger.debug("[config_loader] 配置加载完成")
    return config


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _load_yaml(path: Path, label: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{label} 不存在：{path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    logger.debug("[config_loader] 已加载 %s (%d 顶级键)", label, len(data))
    return data


def _deep_override(config: dict, overrides: dict) -> None:
    """将 overrides 中的键值递归覆盖到 config 各层级。"""
    for key, value in overrides.items():
        _set_nested(config, key, value)


def _set_nested(config: dict, key: str, value) -> None:
    """在 config 中查找 key（不区分层级），找到则覆盖；找不到则写入顶层。"""
    for section_key, section_val in config.items():
        if isinstance(section_val, dict) and key in section_val:
            old = section_val[key]
            section_val[key] = value
            logger.debug("[config_loader] override %s.%s: %s → %s", section_key, key, old, value)
            return
    # 未找到则写入顶层
    config[key] = value
    logger.debug("[config_loader] override (toplevel) %s = %s", key, value)


def _inject_env_vars(config: dict) -> None:
    """从环境变量注入 OKX API 密钥等敏感配置。"""
    exchange_cfg = config.setdefault("exchange", {})
    for env_key, cfg_key in [
        ("OKX_API_KEY", "api_key"),
        ("OKX_SECRET_KEY", "secret_key"),
        ("OKX_PASSPHRASE", "passphrase"),
    ]:
        val = os.getenv(env_key)
        if val:
            exchange_cfg[cfg_key] = val
