"""
risk_filter.py
多重风控过滤模块
在 AI 决策基础上，叠加账户级别风控检查
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

import yaml

# 配置日志：同时输出到控制台和文件
from config_loader import check_env, RISK_CFG, TRADING_CFG, setup_logging, now_cst_str
check_env()
setup_logging("risk_filter")
logger = logging.getLogger(__name__)


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

    if signal not in ["long", "short"]:
        return False, f"信号方向无效：{signal}"

    if confidence not in TRADING_CFG.get("allowed_confidence", ["high"]):
        return False, f"置信度不足：{confidence}"

    if signal_strength < TRADING_CFG.get("min_signal_strength", 7):
        return False, f"信号强度不足：{signal_strength}/10"

    min_trend_strength = TRADING_CFG.get("min_trend_strength", 7)
    if trend_strength > 0 and trend_strength < min_trend_strength:
        return False, f"趋势强度不足：{trend_strength}/10（要求≥{min_trend_strength}）"

    if not volume_confirmed:
        return False, "成交量未确认，可能为假突破"

    if rr < TRADING_CFG.get("min_rr_ratio", 2.0):
        return False, f"风险回报比不足：{decision.get('risk_reward')}"

    if divergence:
        return False, "存在顶底背离风险"

    if structure_broken:
        return False, "价格结构已被打破"

    return True, "信号质量检查通过"


def check_account_risk(
    exchange,
    symbol: str,
    decision: dict
) -> tuple[bool, str]:
    """
    检查账户级别风控
    - 止损冷却期检查
    - 当日亏损是否超限
    - 当前持仓数量是否超限
    - 是否已有同方向持仓
    """
    try:
        # 1. 止损冷却期检查
        from stop_loss_tracker import check_cooldown
        cooldown_hours = RISK_CFG.get("stop_loss_cooldown_hours", 4)
        passed, reason = check_cooldown(symbol, cooldown_hours)
        if not passed:
            return False, reason

        # 2. 检查当前持仓数量
        positions = exchange.fetch_positions([symbol])
        open_positions = [
            p for p in positions
            if float(p.get("contracts", 0)) > 0
        ]

        max_positions = RISK_CFG.get("max_open_positions", 3)
        if len(open_positions) >= max_positions:
            return False, f"持仓数量已达上限：{len(open_positions)}/{max_positions}"

        # 3. 检查是否已有同品种持仓
        symbol_positions = [
            p for p in open_positions
            if p.get("symbol") == symbol
        ]
        if symbol_positions:
            return False, f"{symbol} 已有持仓，跳过重复开仓"

        return True, "账户风控检查通过"

    except Exception as e:
        logger.error(f"账户风控检查异常：{e}")
        return False, f"账户风控检查异常：{e}"


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
        max_loss = RISK_CFG.get("max_loss_pct", RISK_CFG.get("max_daily_loss_pct", 5.0))
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
    keywords = TRADING_CFG.get("warning_keywords", ["低市值", "高波动", "插针", "流动性", "小市值", "波动大"])
    if any(kw in warning for kw in keywords):
        ratio = TRADING_CFG.get("warning_position_ratio", 0.5)
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
) -> dict:
    """
    基于凯利准则计算仓位大小
    单笔最大风险 = 账户余额 * max_position_pct
    若 AI warning 含高波动/低市值关键词，自动折减仓位。

    参数：
        contract_size: 每张合约面值（单位：币），如 HMSTR 为 100
        max_mkt_sz:    交易所市价单最大张数限制（来自 market info）

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
        leverage = TRADING_CFG.get("default_leverage", 10)

    max_risk_pct = TRADING_CFG.get("max_position_pct", 0.06)
    reduction = _check_warning_reduction(warning)
    max_risk_usdt = balance_usdt * max_risk_pct * reduction

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


def run_full_risk_check(
    exchange,
    symbol: str,
    decision: dict,
    balance_cache: dict
) -> tuple[bool, str, dict]:
    """
    执行完整风控检查流程
    返回 (是否通过, 原因, 仓位信息)
    """
    # 1. 信号质量检查（含 trend_strength）
    passed, reason = check_signal_quality(decision)
    if not passed:
        return False, reason, {}

    # 2. ADX 边缘区间加严：ADX 处于 20-25 时要求信号强度提高到 8
    adx_edge_min = TRADING_CFG.get("adx_edge_min", 20)
    adx_edge_max = TRADING_CFG.get("adx_edge_max", 25)
    adx_edge_min_ss = TRADING_CFG.get("adx_edge_min_signal_strength", 8)
    anchor_adx = decision.get("_anchor_adx", None)
    if anchor_adx is not None:
        try:
            adx_val = float(anchor_adx)
            if adx_edge_min <= adx_val < adx_edge_max:
                ss = decision.get("signal_strength", 0)
                if ss < adx_edge_min_ss:
                    return False, f"ADX边缘区间({adx_val:.1f})，信号强度需≥{adx_edge_min_ss}，当前{ss}", {}
                logger.info(f"ADX边缘区间({adx_val:.1f})，信号强度{ss}≥{adx_edge_min_ss}，通过加严检查")
        except (TypeError, ValueError):
            pass

    # 3. 日亏损检查
    passed, reason = check_daily_loss(exchange, balance_cache)
    if not passed:
        return False, reason, {}

    # 4. 账户持仓检查
    passed, reason = check_account_risk(exchange, symbol, decision)
    if not passed:
        return False, reason, {}

    # 5. 计算仓位（传入 warning 自动折减高波动仓位）
    balance = exchange.fetch_balance()
    balance_usdt = float(balance["free"].get("USDT", 0))

    # 获取合约面值和市价单张数上限（避免低价小币超限）
    contract_size = 1.0
    max_mkt_sz = None
    try:
        market = exchange.market(symbol)
        contract_size = float(market.get("contractSize") or 1.0)
        # OKX market info 中的 maxMktSz（市价单最大张数）
        info = market.get("info", {})
        if info.get("maxMktSz"):
            max_mkt_sz = float(info["maxMktSz"])
        logger.info(
            f"合约参数 | contractSize={contract_size} | maxMktSz={max_mkt_sz}"
        )
    except Exception as e:
        logger.warning(f"获取合约参数失败，使用默认值：{e}")

    warning = decision.get("warning") or ""
    position = calculate_position_size(
        balance_usdt=balance_usdt,
        entry_price=decision.get("entry_price", 0),
        stop_loss=decision.get("stop_loss", 0),
        warning=warning,
        contract_size=contract_size,
        max_mkt_sz=max_mkt_sz,
    )

    if not position:
        return False, "仓位计算失败", {}

    reduced_note = "（warning折减50%）" if position.get("warning_reduced") else ""
    logger.info(
        f"风控全部通过{reduced_note} | 仓位：{position['contracts']} 张 | "
        f"保证金：{position['margin_usdt']} USDT | "
        f"最大风险：{position['risk_usdt']} USDT"
    )
    return True, f"风控全部通过{reduced_note}", position


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