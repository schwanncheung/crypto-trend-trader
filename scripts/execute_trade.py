"""
execute_trade.py
交易执行模块
负责开仓、设置止盈止损、平仓、持仓管理
"""

import logging
import time
from pathlib import Path
from datetime import datetime, timezone
import sys

import ccxt

# 配置日志：同时输出到控制台和文件
from config_loader import (
    check_env,
    EXCHANGE_CFG,
    EXCHANGE_API_KEY,
    EXCHANGE_API_SECRET,
    EXCHANGE_PASSPHRASE,
    RISK_CFG,
    TRADING_CFG,
    setup_logging,
    now_cst,
    now_cst_str,
)
check_env()
setup_logging("execute_trade")
logger = logging.getLogger(__name__)

# ── 交易执行参数（从配置读取）──────────────────────────
_MAX_SLIPPAGE_PCT = TRADING_CFG.get("max_slippage_pct", 5.0)
_MAX_MARGIN_USAGE_RATIO = TRADING_CFG.get("max_margin_usage_ratio", 0.5)


# ── 交易所连接 ─────────────────────────────────

def create_exchange() -> ccxt.Exchange:
    exchange = ccxt.okx({
        "apiKey":   EXCHANGE_API_KEY,
        "secret":   EXCHANGE_API_SECRET,
        "password": EXCHANGE_PASSPHRASE,
        "options":  {"defaultType": "swap"},
        "enableRateLimit": True,
    })
    if EXCHANGE_CFG.get("testnet", True):
        exchange.set_sandbox_mode(True)
    return exchange


# ── 平仓操作 ───────────────────────────────────

def close_position(
    exchange: ccxt.Exchange,
    symbol: str,
    reason: str = "手动平仓"
) -> dict:
    """
    平掉指定品种的所有持仓
    同时撤销对应的止盈止损挂单
    """
    try:
        positions = exchange.fetch_positions([symbol])
        active = [
            p for p in positions
            if float(p.get("contracts", 0)) > 0
        ]

        if not active:
            logger.info(f"{symbol} 无持仓，跳过平仓")
            return {"status": "no_position"}

        results = []
        for pos in active:
            side = "sell" if pos["side"] == "long" else "buy"
            contracts = float(pos["contracts"])

            order = exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=contracts,
                params={"reduceOnly": True}
            )
            logger.info(
                f"平仓成功：{symbol} {contracts}张 | 原因：{reason}"
            )
            results.append(order)

        # 撤销该品种所有挂单（含 algo 止损止盈单）
        # OKX 普通挂单和 algo 条件单需分别查询
        for order_type_params in [
            {"instType": "SWAP"},                          # 普通限价单
            {"instType": "SWAP", "algoOrdType": "conditional"},  # algo 条件单（止损止盈）
        ]:
            try:
                open_orders = exchange.fetch_open_orders(symbol, params=order_type_params)
                for order in open_orders:
                    try:
                        exchange.cancel_order(order["id"], symbol)
                    except Exception as cancel_err:
                        logger.warning(f"撤单失败 {order['id']}: {cancel_err}")
            except Exception as fetch_err:
                logger.warning(f"查询挂单失败（{order_type_params}）: {fetch_err}")
        logger.info(f"已撤销 {symbol} 所有挂单")

        close_result = {
            "type": "close",
            "status": "success",
            "symbol": symbol,
            "reason": reason,
            "orders": results,
            "timestamp": now_cst().isoformat()
        }
        _save_trade_log(close_result)
        return close_result

    except Exception as e:
        logger.error(f"平仓失败：{e}")
        return {"status": "failed", "error": str(e)}


# ── 持仓查询 ───────────────────────────────────

def _load_ai_key_levels(symbol: str) -> dict:
    """从最新 decision log 加载 key_support / key_resistance"""
    import json
    decisions_dir = Path("logs/decisions")
    if not decisions_dir.exists():
        return {}
    symbol_safe = symbol.replace("/", "_").replace(":", "_")
    candidates = list(decisions_dir.glob(f"{symbol_safe}_*.json"))
    if not candidates:
        return {}
    latest = max(candidates, key=lambda f: f.name)
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
        decision = data.get("decision", data)
        return {
            "key_support":    decision.get("key_support"),
            "key_resistance": decision.get("key_resistance"),
        }
    except Exception:
        return {}


def get_open_positions(exchange: ccxt.Exchange) -> list:
    """获取所有当前持仓信息"""
    try:
        positions = exchange.fetch_positions()
        active = [
            {
                "symbol": p["symbol"],
                "side": p["side"],
                "contracts": float(p.get("contracts", 0)),
                "entry_price": float(p.get("entryPrice", 0)),
                "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                "percentage": float(p.get("percentage", 0)),
                "leverage": float(p.get("leverage", 1)),
                "margin": float(p.get("initialMargin") or p.get("margin") or 0),
                "liquidation_price": float(p.get("liquidationPrice") or 0),
                **_load_ai_key_levels(p["symbol"]),
            }
            for p in positions
            if float(p.get("contracts", 0)) > 0
        ]
        return active
    except Exception as e:
        logger.error(f"获取持仓失败：{e}")
        return []


def check_position_health(
    exchange: ccxt.Exchange,
    max_loss_pct: float = -10.0
) -> list:
    """
    检查持仓健康状态
    返回需要紧急平仓的持仓列表（亏损超过阈值）
    """
    positions = get_open_positions(exchange)
    urgent_close = []

    for pos in positions:
        pnl_pct = pos.get("percentage", 0)
        if pnl_pct <= max_loss_pct:
            logger.warning(
                f"紧急风控：{pos['symbol']} 亏损 {pnl_pct:.1f}%，"
                f"触发强制平仓"
            )
            urgent_close.append(pos)

    return urgent_close


# ── 完整执行流程 ───────────────────────────────

def execute_from_decision(
    exchange,
    symbol: str,
    decision: dict,
    position_info: dict = None,
) -> dict:
    """
    根据 AI 决策执行开仓

    参数：
        exchange:      ccxt 交易所实例
        symbol:        合约名称，如 "ANIME/USDT:USDT"
        decision:      AI 分析结果，包含 signal / entry_price / stop_loss / take_profit
        position_info: 风控计算后的仓位信息，包含 contracts / margin_usdt 等
                       如果为 None，内部自动计算
    返回：
        {"status": "success/skipped/error", "order": {...}, "reason": "..."}
    """
    import traceback
    try:
        # ── 0. 检查开仓开关 ──────────────────────────
        enable_open = TRADING_CFG.get("enable_open_position", True)
        if not enable_open:
            logger.warning(f"{symbol} 开仓已关闭（enable_open_position=false），跳过开仓")
            return {
                "status": "skipped",
                "reason": "开仓功能已关闭"
            }

        # ── 1. 解析 AI 决策 ──────────────────────────
        signal      = decision.get("signal", "").lower()    # "long" / "short" / "wait"
        entry_price = decision.get("entry_price")
        stop_loss   = decision.get("stop_loss")
        take_profit = decision.get("take_profit")
        confidence  = decision.get("confidence", 0)

        # 只处理 long / short 信号
        if signal not in ("long", "short"):
            return {
                "status": "skipped",
                "reason": f"信号为 {signal}，无需开仓"
            }

        if not all([entry_price, stop_loss, take_profit]):
            return {
                "status": "error",
                "reason": f"AI决策缺少必要价格参数：entry={entry_price} sl={stop_loss} tp={take_profit}"
            }

        # ── 2. 仓位信息（优先用风控传入的，否则自动计算）──
        if position_info is None:
            position_info = _calculate_position(
                exchange=exchange,
                symbol=symbol,
                entry_price=float(entry_price),
                stop_loss=float(stop_loss),
            )

        contracts   = position_info.get("contracts")
        margin_usdt = position_info.get("margin_usdt")

        if not contracts or contracts <= 0:
            return {
                "status": "skipped",
                "reason": f"计算仓位为0，跳过开仓（保证金：{margin_usdt} USDT）"
            }

        # ── 3. 确定开仓方向 ──────────────────────────
        side      = "buy"  if signal == "long"  else "sell"
        sl_side   = "sell" if signal == "long"  else "buy"
        tp_side   = "sell" if signal == "long"  else "buy"

        logger.info(
            f"准备开仓 | {symbol} | 方向：{signal.upper()} | "
            f"入场：{entry_price} | 止损：{stop_loss} | 止盈：{take_profit} | "
            f"张数：{contracts} | 保证金：{margin_usdt} USDT"
        )

        # ── 4. 设置杠杆 ──────────────────────────────
        leverage = RISK_CFG.get("leverage", 10)
        try:
            exchange.set_leverage(leverage, symbol)
            logger.info(f"杠杆设置：{leverage}x")
        except Exception as e:
            logger.warning(f"杠杆设置失败（可能已设置）：{e}")

        # ── 5. 市价开仓 ──────────────────────────────
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=contracts,
            params={"tdMode": "cross"}  # 全仓模式
        )
        order_id = order.get("id", "unknown")
        logger.info(f"开仓订单已提交 | 订单ID：{order_id}")

        # ── 6. 获取实际成交价，重新计算止损止盈 ──────────
        time.sleep(0.5)  # 等待订单成交
        try:
            # 查询实际成交价格
            positions = exchange.fetch_positions([symbol])
            actual_entry = None
            for p in positions:
                if float(p.get("contracts", 0)) > 0:
                    actual_entry = float(p.get("entryPrice", 0))
                    break

            if actual_entry and actual_entry > 0:
                # 计算滑点
                slippage_pct = (actual_entry - entry_price) / entry_price * 100
                logger.info(f"实际成交价：{actual_entry}（计划：{entry_price}，滑点：{slippage_pct:+.2f}%）")

                # 如果滑点超过阈值，基于实际成交价重新计算止损止盈
                if abs(slippage_pct) > _MAX_SLIPPAGE_PCT:
                    logger.warning(f"滑点超过 {_MAX_SLIPPAGE_PCT}%，基于实际成交价重新计算止损止盈")

                    # 基于实际成交价平移原始止损止盈距离
                    stop_loss_distance = abs(stop_loss - entry_price)
                    take_profit_distance = abs(take_profit - entry_price)

                    if signal == "long":
                        stop_loss = actual_entry - stop_loss_distance
                        take_profit = actual_entry + take_profit_distance
                    else:  # short
                        stop_loss = actual_entry + stop_loss_distance
                        take_profit = actual_entry - take_profit_distance

                    logger.info(f"重新计算后 | 止损：{stop_loss}，止盈：{take_profit}")
            else:
                logger.warning("无法获取实际成交价，使用计划价格创建止损止盈")
        except Exception as e:
            logger.warning(f"获取实际成交价失败，使用计划价格：{e}")

        # ── 7. 挂止损单（带重试机制）──────────────────
        sl_success = False
        sl_error = None
        for attempt in range(2):  # 最多尝试 2 次
            try:
                sl_order = exchange.create_order(
                    symbol=symbol,
                    type="conditional",
                    side=sl_side,
                    amount=contracts,
                    price=None,
                    params={
                        "ordType": "conditional",
                        "slTriggerPx": str(float(stop_loss)),
                        "slOrdPx": "-1",   # -1 表示市价触发
                        "reduceOnly": True,
                        "tdMode": "cross",
                    },
                )
                logger.info(f"止损单已挂 | 价格：{stop_loss} | 订单ID：{sl_order.get('id')}")
                sl_success = True
                break
            except Exception as e:
                sl_error = str(e)
                if attempt == 0:
                    logger.warning(f"止损单挂单失败（第 {attempt + 1} 次），等待 1 秒后重试：{e}")
                    time.sleep(1)
                else:
                    logger.error(f"止损单挂单失败（第 {attempt + 1} 次），已达最大重试次数：{e}")

        # ── 8. 挂止盈单（带重试机制）──────────────────
        tp_success = False
        tp_error = None
        for attempt in range(2):  # 最多尝试 2 次
            try:
                tp_order = exchange.create_order(
                    symbol=symbol,
                    type="conditional",
                    side=tp_side,
                    amount=contracts,
                    price=None,
                    params={
                        "ordType": "conditional",
                        "tpTriggerPx": str(float(take_profit)),
                        "tpOrdPx": "-1",   # -1 表示市价触发
                        "reduceOnly": True,
                        "tdMode": "cross",
                    },
                )
                logger.info(f"止盈单已挂 | 价格：{take_profit} | 订单ID：{tp_order.get('id')}")
                tp_success = True
                break
            except Exception as e:
                tp_error = str(e)
                if attempt == 0:
                    logger.warning(f"止盈单挂单失败（第 {attempt + 1} 次），等待 1 秒后重试：{e}")
                    time.sleep(1)
                else:
                    logger.error(f"止盈单挂单失败（第 {attempt + 1} 次），已达最大重试次数：{e}")

        # ── 9. 检查止损止盈状态，失败时发送飞书通知 ────
        if not sl_success or not tp_success:
            from notifier import send_notification
            failed_items = []
            if not sl_success:
                failed_items.append(f"止损单（价格：{stop_loss}）")
            if not tp_success:
                failed_items.append(f"止盈单（价格：{take_profit}）")

            error_msg = (
                f"⚠️ {symbol} 开仓成功，但以下订单创建失败：\n"
                f"{'、'.join(failed_items)}\n\n"
                f"请立即手动设置！\n"
                f"开仓价：{entry_price}\n"
                f"止损价：{stop_loss}\n"
                f"止盈价：{take_profit}\n"
                f"张数：{contracts}"
            )
            if sl_error:
                error_msg += f"\n\n止损错误：{sl_error}"
            if tp_error:
                error_msg += f"\n止盈错误：{tp_error}"

            send_notification(error_msg)
            logger.error(error_msg)

        result = {
            "type":      "open",
            "status":    "success",
            "order_id":  order_id,
            "symbol":    symbol,
            "signal":    signal,
            "contracts": contracts,
            "margin_usdt": margin_usdt,
            "entry_price": entry_price,
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
            "key_support":    decision.get("key_support"),
            "key_resistance":  decision.get("key_resistance"),
            "confidence":  confidence,
            "reason":    "开仓成功",
            "timestamp": now_cst().isoformat(),
        }
        _save_trade_log(result)
        return result

    except Exception as e:
        logger.error(f"execute_from_decision 异常：{e}")
        logger.error(traceback.format_exc())
        return {
            "status": "error",
            "reason": str(e),
        }


def _calculate_position(
    exchange,
    symbol: str,
    entry_price: float,
    stop_loss: float,
) -> dict:
    """
    根据固定风险比例自动计算开仓张数
    默认每笔交易风险不超过总资金的 1%
    已用保证金从可用余额中扣除，避免超额使用资金
    """
    try:
        risk_pct      = RISK_CFG.get("risk_per_trade_pct", 1.0) / 100
        leverage      = RISK_CFG.get("leverage", 10)

        # 获取账户可用余额（free = 已扣除保证金后的可用资金，OKX 直接返回）
        balance = exchange.fetch_balance()
        free_usdt = _get_usdt_balance(balance)
        available_usdt = free_usdt

        logger.info(
            f"余额状态 | free（可用）：{free_usdt:.2f} USDT | "
            f"本次风险金额：{free_usdt * risk_pct:.2f} USDT"
        )

        risk_usdt = available_usdt * risk_pct  # 每笔最大亏损金额

        # 每张合约的风险
        price_diff = abs(entry_price - stop_loss)
        if price_diff == 0:
            return {"contracts": 0, "margin_usdt": 0}

        # 获取合约面值 & 市价单最大张数
        market        = exchange.market(symbol)
        contract_size = float(market.get("contractSize") or 1.0)
        info          = market.get("info", {})
        max_mkt_sz    = float(info["maxMktSz"]) if info.get("maxMktSz") else None

        # 张数 = 风险金额 / (止损点数 × 合约面值)
        contracts   = int(risk_usdt / (price_diff * contract_size))
        margin_usdt = round((contracts * contract_size * entry_price) / leverage, 2)

        # 安全检查：保证金不得超过可用余额的阈值
        if margin_usdt > available_usdt * _MAX_MARGIN_USAGE_RATIO:
            contracts   = int((available_usdt * _MAX_MARGIN_USAGE_RATIO * leverage) / (contract_size * entry_price))
            margin_usdt = round((contracts * contract_size * entry_price) / leverage, 2)
            logger.warning(f"仓位超限，已按可用余额50%上限调整：{contracts}张，保证金：{margin_usdt} USDT")

        # 限制不超过交易所市价单最大张数（防止低价小币超限）
        if max_mkt_sz is not None and contracts > max_mkt_sz:
            logger.warning(
                f"计算张数 {contracts} 超过 maxMktSz={max_mkt_sz}，已截断至上限"
            )
            contracts   = int(max_mkt_sz)
            margin_usdt = round((contracts * contract_size * entry_price) / leverage, 2)

        logger.info(
            f"仓位计算 | 张数：{contracts} | 保证金：{margin_usdt} USDT"
        )

        return {
            "contracts":      contracts,
            "margin_usdt":    margin_usdt,
            "total_usdt":     free_usdt,
            "available_usdt": available_usdt,
        }

    except Exception as e:
        logger.error(f"仓位计算失败：{e}")
        return {"contracts": 0, "margin_usdt": 0}


# ── 余额获取 ───────────────────────────────────

def _get_usdt_balance(balance: dict) -> float:
    """
    兼容多种 fetch_balance 返回格式，统一优先取 free（可用）余额
    """
    # 格式1: balance["free"]["USDT"]
    if "free" in balance and isinstance(balance["free"], dict):
        val = balance["free"].get("USDT")
        if val is not None:
            return float(val)

    # 格式2: balance["USDT"]["free"]
    if "USDT" in balance and isinstance(balance["USDT"], dict):
        val = balance["USDT"].get("free")
        if val is not None:
            return float(val)

    # 格式3: balance["total"]["USDT"]（兜底，不含已用保证金）
    if "total" in balance and isinstance(balance["total"], dict):
        val = balance["total"].get("USDT")
        if val is not None:
            return float(val)

    logger.warning(f"未找到USDT余额，balance keys: {list(balance.keys())}")
    return 0.0


# ── 日志保存 ───────────────────────────────────

def _save_trade_log(trade_result: dict) -> None:
    """保存交易记录到日志"""
    import json
    log_dir = Path("logs/trades")
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = now_cst_str()
    symbol_safe = trade_result.get(
        "symbol", "UNKNOWN"
    ).replace("/", "_").replace(":", "_")

    log_path = log_dir / f"{symbol_safe}_{ts}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(trade_result, f, ensure_ascii=False, indent=2)

    logger.info(f"交易日志已保存：{log_path}")


# ── 测试入口 ───────────────────────────────────
if __name__ == "__main__":
    exchange = create_exchange()

    # 测试获取持仓
    positions = get_open_positions(exchange)
    print(f"\n当前持仓数量：{len(positions)}")
    for p in positions:
        print(
            f"  {p['symbol']} | {p['side']} | "
            f"{p['contracts']}张 | PnL: {p['unrealized_pnl']:.2f} USDT"
        )

    # 测试余额
    balance = exchange.fetch_balance()
    usdt = float(balance["free"].get("USDT", 0))
    print(f"\n可用余额：{usdt:.2f} USDT")