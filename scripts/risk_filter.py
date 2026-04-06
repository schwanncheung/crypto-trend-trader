"""
risk_filter.py
多重风控过滤模块
在 AI 决策基础上，叠加账户级别风控检查

⚠️ 修改风控逻辑后，请同步更新 CLAUDE.md「核心算法」章节
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

import yaml

# 配置日志：同时输出到控制台和文件
from config_loader import check_env, RISK_CFG, TRADING_CFG, setup_logging, now_cst_str, CFG
check_env()
setup_logging("risk_filter")
logger = logging.getLogger(__name__)

# 模块级配置副本（可被 reload_config_from_dict 更新）
_TRADING_CFG = TRADING_CFG.copy() if TRADING_CFG else {}
_RISK_CFG = RISK_CFG.copy() if RISK_CFG else {}
_RULE_CFG = CFG.get("analysis", {}).get("rule_filter", {}).copy() if CFG else {}

# 全局变量，可被 reload 更新
_MIN_TREND_STRENGTH = _TRADING_CFG.get("min_trend_strength", 7)
_MIN_SIGNAL_STRENGTH = _TRADING_CFG.get("min_signal_strength", 7)
_MIN_RR_RATIO = _TRADING_CFG.get("min_rr_ratio", 2.0)
_RSI_OVERBOUGHT = _RULE_CFG.get("rsi_overbought", 70)
_RSI_OVERSOLD = _RULE_CFG.get("rsi_oversold", 30)


def reload_config_from_dict(config: dict) -> None:
    """
    从外部配置字典重新加载参数（回测系统 override 机制）。
    """
    global _MIN_TREND_STRENGTH, _MIN_SIGNAL_STRENGTH, _MIN_RR_RATIO, _TRADING_CFG, _RISK_CFG, _RULE_CFG
    global _RSI_OVERBOUGHT, _RSI_OVERSOLD

    trading_cfg = config.get("trading", {})
    risk_cfg = config.get("risk", {})
    rule_cfg = config.get("analysis", {}).get("rule_filter", {})

    _TRADING_CFG.update(trading_cfg)
    _RISK_CFG.update(risk_cfg)
    _RULE_CFG.update(rule_cfg)

    _MIN_TREND_STRENGTH = _TRADING_CFG.get("min_trend_strength", _MIN_TREND_STRENGTH)
    _MIN_SIGNAL_STRENGTH = _TRADING_CFG.get("min_signal_strength", _MIN_SIGNAL_STRENGTH)
    _MIN_RR_RATIO = _TRADING_CFG.get("min_rr_ratio", _MIN_RR_RATIO)
    _RSI_OVERBOUGHT = _RULE_CFG.get("rsi_overbought", _RSI_OVERBOUGHT)
    _RSI_OVERSOLD = _RULE_CFG.get("rsi_oversold", _RSI_OVERSOLD)

    logger.info(
        f"[risk_filter] 配置已重载："
        f"min_signal_strength={_MIN_SIGNAL_STRENGTH}, "
        f"min_rr_ratio={_MIN_RR_RATIO}, "
        f"rsi_overbought={_RSI_OVERBOUGHT}, rsi_oversold={_RSI_OVERSOLD}"
    )


# ── 单笔交易风控 ───────────────────────────────

def check_signal_quality(decision: dict) -> tuple[bool, str]:
    """
    检查AI信号质量
    返回 (是否通过, 原因)
    """
    signal = decision.get("signal", "wait")
    confidence = decision.get("confidence", "low")
    signal_strength = decision.get("signal_strength", 0)
    trend_strength = decision.get("trend_strength", 0)
    volume_confirmed = decision.get("volume_confirmed", False)
    rr = _parse_rr(decision.get("risk_reward", "1:0"))
    divergence = decision.get("divergence_risk", True)
    structure_broken = decision.get("structure_broken", True)
    entry_rsi = decision.get("entry_rsi")  # 新增：入场 RSI

    if signal not in ["long", "short"]:
        return False, f"信号方向无效：{signal}"

    if confidence not in _TRADING_CFG.get("allowed_confidence", ["high"]):
        return False, f"置信度不足：{confidence}"

    if signal_strength < _MIN_SIGNAL_STRENGTH:
        return False, f"信号强度不足：{signal_strength}/10"

    if trend_strength > 0 and trend_strength < _MIN_TREND_STRENGTH:
        return False, f"趋势强度不足：{trend_strength}/10（要求≥{_MIN_TREND_STRENGTH}）"
    if trend_strength == 0:
        logger.debug("trend_strength=0（ADX极低或未提供），跳过趋势强度检查")

    if not volume_confirmed:
        return False, "成交量未确认，可能为假突破"

    if rr < _MIN_RR_RATIO:
        return False, f"风险回报比不足：{decision.get('risk_reward')}"

    if divergence:
        return False, "存在顶底背离风险"

    if structure_broken:
        return False, "价格结构已被打破"

    # ── 新增：RSI 极值保护（防止在超买/超卖区追单）────────────
    if entry_rsi is not None:
        if signal == "long" and entry_rsi >= _RSI_OVERBOUGHT:
            return False, f"RSI={entry_rsi:.1f} 超买（>={_RSI_OVERBOUGHT}），禁止做多"
        if signal == "short" and entry_rsi <= _RSI_OVERSOLD:
            return False, f"RSI={entry_rsi:.1f} 超卖（<={_RSI_OVERSOLD}），禁止做空"

    return True, "信号质量检查通过"


def check_daily_loss(
    exchange,
    balance_cache: dict
) -> tuple[bool, str]:
    """
    检查当日亏损是否超过最大限制
    balance_cache: {"start_balance": 10000, "date": "2026-03-19"}
    """
    try:
        today = now_cst_str("%Y-%m-%d")
        balance = exchange.fetch_balance()

        # 兼容 OKX 多种余额格式
        current_equity = 0.0
        if isinstance(balance.get("USDT"), dict):
            current_equity = float(balance["USDT"].get("total") or balance["USDT"].get("free") or 0)
        elif isinstance(balance.get("total"), dict):
            current_equity = float(balance["total"].get("USDT", 0))
        elif isinstance(balance.get("free"), dict):
            current_equity = float(balance["free"].get("USDT", 0))

        if current_equity <= 0:
            logger.warning("日亏损检查：当前权益为0或获取失败，保守起见放行")
            return True, "当前权益获取失败，跳过亏损检查"

        # 首次运行记录初始余额
        if balance_cache.get("date") != today:
            balance_cache["start_balance"] = current_equity
            balance_cache["date"] = today
            logger.info(f"新的交易日，初始余额记录：{current_equity:.2f} USDT")
            return True, "新的交易日，余额已重置"

        start = balance_cache.get("start_balance", current_equity)
        if start <= 0:
            return True, "初始余额为0，跳过亏损检查"

        loss_pct = (start - current_equity) / start
        # 优先读 max_loss_pct（settings.yaml 实际字段），兼容 max_daily_loss_pct
        max_loss = _RISK_CFG.get("max_loss_pct", _RISK_CFG.get("max_daily_loss_pct", 5.0))
        # 支持正数百分比（5.0）和小数（0.05）两种写法
        if max_loss > 1:
            max_loss = max_loss / 100

        logger.info(
            f"日亏损检查 | 初始：{start:.2f} | 当前：{current_equity:.2f} | "
            f"亏损：{loss_pct:.1%} | 上限：{max_loss:.1%}"
        )

        if loss_pct >= max_loss:
            return False, (
                f"当日亏损 {loss_pct:.1%} 已超过限制 {max_loss:.1%}，"
                f"今日停止交易"
            )

        return True, f"当日亏损 {loss_pct:.1%}，未超限"

    except Exception as e:
        logger.error(f"日亏损检查异常（保守放行）：{e}")
        # 异常时放行而非阻断，避免因网络波动导致全天停止交易
        return True, f"日亏损检查异常，保守放行：{e}"


def _check_warning_reduction(warning: str) -> float:
    """
    检查 AI warning 是否包含高波动/低市值关键词，返回仓位折减比例。
    无 warning 或不含关键词时返回 1.0（不折减）。
    """
    if not warning or warning in ("null", "None", ""):
        return 1.0
    keywords = _TRADING_CFG.get("warning_keywords", ["低市值", "高波动", "插针", "流动性", "小市值", "波动大"])
    if any(kw in warning for kw in keywords):
        ratio = _TRADING_CFG.get("warning_position_ratio", 0.5)
        logger.warning(f"AI warning 触发仓位折减（×{ratio}）：{warning}")
        return ratio
    return 1.0


def calculate_position_size(
    balance_usdt: float,
    entry_price: float,
    stop_loss: float,
    leverage: int = None,
    warning: str = None,
    contract_size: float = 1.0,
    max_mkt_sz: float = None,
    pattern_boost: float = 1.0,  # 新增：形态仓位倍数（hammer=1.1）
) -> dict:
    """
    基于凯利准则计算仓位大小
    单笔最大风险 = 账户余额 * max_position_pct
    若 AI warning 含高波动/低市值关键词，自动折减仓位。

    参数：
        contract_size: 每张合约面值（单位：币），如 HMSTR 为 100
        max_mkt_sz:    交易所市价单最大张数限制（来自 market info）
        pattern_boost: 形态仓位倍数，hammer 形态为 1.1（增加10%仓位）

    返回：
    {
        "contracts": 0.01,       # 合约张数
        "margin_usdt": 100.0,    # 所需保证金
        "risk_usdt": 60.0,       # 实际风险金额
        "leverage": 10,          # 使用杠杆
        "warning_reduced": False # 是否触发了 warning 折减
    }
    """
    if leverage is None:
        leverage = _TRADING_CFG.get("default_leverage", 10)

    max_risk_pct = _TRADING_CFG.get("max_position_pct", 0.06)
    reduction = _check_warning_reduction(warning)
    max_risk_usdt = balance_usdt * max_risk_pct * reduction * pattern_boost  # 应用形态 boost

    # 单位价格变动的风险（每张合约）
    price_risk = abs(entry_price - stop_loss)
    if price_risk == 0:
        logger.error("止损位与入场价相同，无法计算仓位")
        return {}

    # 合约张数：风险金额 / (单张止损点数 × 合约面值)
    risk_per_contract = price_risk * contract_size
    contracts = max_risk_usdt / risk_per_contract
    contracts = int(contracts)  # OKX 永续合约张数必须为整数

    # 限制不超过交易所市价单最大张数
    if max_mkt_sz is not None and contracts > max_mkt_sz:
        logger.warning(
            f"计算张数 {contracts} 超过 maxMktSz={max_mkt_sz}，已截断至上限"
        )
        contracts = int(max_mkt_sz)

    if contracts <= 0:
        logger.warning("计算合约张数为0，仓位过小")
        return {}

    # 所需保证金 = 张数 × 合约面值 × 入场价 / 杠杆
    margin_usdt = (contracts * contract_size * entry_price) / leverage

    # 实际风险
    risk_usdt = contracts * risk_per_contract

    return {
        "contracts": contracts,
        "margin_usdt": round(margin_usdt, 2),
        "risk_usdt": round(risk_usdt, 2),
        "leverage": leverage,
        "warning_reduced": reduction < 1.0
    }


def _parse_rr(rr_str: str) -> float:
    try:
        parts = rr_str.split(":")
        return float(parts[1]) / float(parts[0])
    except Exception:
        return 0.0


# ── 测试入口 ──────────────────────────────────
if __name__ == "__main__":
    import json

    mock_decision = {
        "signal": "long",
        "confidence": "high",
        "signal_strength": 8,
        "volume_confirmed": True,
        "risk_reward": "1:2.5",
        "divergence_risk": False,
        "structure_broken": False,
        "entry_price": 60000,
        "stop_loss": 58500,
        "take_profit": 63750,
        "reason": "测试用mock数据"
    }

    passed, reason = check_signal_quality(mock_decision)
    print(f"信号质量检查：{'✅ 通过' if passed else '❌ 未通过'} - {reason}")

    position = calculate_position_size(
        balance_usdt=10000,
        entry_price=60000,
        stop_loss=58500
    )
    print(f"仓位计算结果：{json.dumps(position, indent=2)}")