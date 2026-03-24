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


# ── 开仓操作 ───────────────────────────────────

def open_position(
    exchange: ccxt.Exchange,
    symbol: str,
    signal: str,
    contracts: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    leverage: int = None
) -> dict:
    """
    开仓并同步设置止盈止损
    signal: "long" 或 "short"
    返回开仓结果字典
    """
    if leverage is None:
        leverage = TRADING_CFG.get("default_leverage", 10)

    side = "buy" if signal == "long" else "sell"
    sl_side = "sell" if signal == "long" else "buy"

    try:
        # 1. 设置杠杆
        exchange.set_leverage(leverage, symbol)
        logger.info(f"已设置杠杆：{leverage}x")

        # 2. 市价开仓
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=contracts,
            params={"reduceOnly": False}
        )
        logger.info(
            f"开仓成功：{signal.upper()} {symbol} "
            f"{contracts}张 @ 市价"
        )

        # 等待成交确认
        time.sleep(1)

        # 3. 设置止损单（OKX algo 条件单）
        sl_order_id = None
        try:
            sl_order = exchange.create_order(
                symbol=symbol,
                type="conditional",
                side=sl_side,
                amount=contracts,
                price=None,
                params={
                    "ordType": "conditional",
                    "slTriggerPx": str(stop_loss),
                    "slOrdPx": "-1",   # -1 表示市价触发
                    "reduceOnly": True,
                    "tdMode": "cross",
                }
            )
            sl_order_id = sl_order.get("id", "unknown")
            logger.info(f"止损已设置：{stop_loss} | 订单ID：{sl_order_id}")
        except Exception as e:
            logger.error(f"止损单挂单失败（请手动处理）：{e}")

        # 4. 设置止盈单（OKX algo 条件单）
        tp_order_id = None
        try:
            tp_order = exchange.create_order(
                symbol=symbol,
                type="conditional",
                side=sl_side,
                amount=contracts,
                price=None,
                params={
                    "ordType": "conditional",
                    "tpTriggerPx": str(take_profit),
                    "tpOrdPx": "-1",   # -1 表示市价触发
                    "reduceOnly": True,
                    "tdMode": "cross",
                }
            )
            tp_order_id = tp_order.get("id", "unknown")
            logger.info(f"止盈已设置：{take_profit} | 订单ID：{tp_order_id}")
        except Exception as e:
            logger.error(f"止盈单挂单失败（请手动处理）：{e}")

        result = {
            "type": "open",
            "status": "success",
            "symbol": symbol,
            "signal": signal,
            "contracts": contracts,
            "leverage": leverage,
            "entry_price": entry_price,
            "margin_usdt": margin_usdt,
            "entry_order_id": order["id"],
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timestamp": now_cst().isoformat()
        }

        _save_trade_log(result)
        return result

    except Exception as e:
        logger.error(f"开仓失败：{e}")
        return {"status": "failed", "error": str(e)}


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

        # 撤销该品种所有挂单（止盈止损）（逐个取消，OKX 不支持 cancelAllOrders）
        open_orders = exchange.fetch_orders(symbol, params={"instType": "SWAP", "state": "live"})
        for order in open_orders:
            try:
                exchange.cancel_order(order["id"], symbol)
            except Exception as cancel_err:
                logger.warning(f"撤单失败 {order['id']}: {cancel_err}")
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

        # ── 6. 挂止损单 ──────────────────────────────
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
        except Exception as e:
            logger.error(f"止损单挂单失败（请手动处理）：{e}")

        # ── 7. 挂止盈单 ──────────────────────────────
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
        except Exception as e:
            logger.error(f"止盈单挂单失败（请手动处理）：{e}")

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

        # 获取账户余额（使用 free 可用余额，非 total）
        balance = exchange.fetch_balance()
        free_usdt = _get_usdt_balance(balance)

        # 扣除已开仓占用的保证金
        try:
            open_positions = exchange.fetch_positions()
            used_margin = sum(
                float(p.get("initialMargin") or p.get("margin") or 0)
                for p in open_positions
                if float(p.get("contracts", 0)) > 0
            )
        except Exception as e:
            logger.warning(f"获取已用保证金失败，按0计算：{e}")
            used_margin = 0.0

        available_usdt = max(free_usdt - used_margin, 0)
        risk_usdt = available_usdt * risk_pct  # 每笔最大亏损金额

        logger.info(
            f"余额状态 | free：{free_usdt:.2f} USDT | "
            f"已用保证金：{used_margin:.2f} USDT | "
            f"可用：{available_usdt:.2f} USDT | "
            f"本次风险金额：{risk_usdt:.2f} USDT"
        )

        # 每张合约的风险
        price_diff = abs(entry_price - stop_loss)
        if price_diff == 0:
            return {"contracts": 0, "margin_usdt": 0}

        # 获取合约面值
        market        = exchange.market(symbol)
        contract_size = market.get("contractSize", 1)

        # 张数 = 风险金额 / (止损点数 × 合约面值)
        contracts   = int(risk_usdt / (price_diff * contract_size))
        margin_usdt = round((contracts * contract_size * entry_price) / leverage, 2)

        # 安全检查：保证金不得超过可用余额的 50%
        if margin_usdt > available_usdt * 0.5:
            contracts   = int((available_usdt * 0.5 * leverage) / (contract_size * entry_price))
            margin_usdt = round((contracts * contract_size * entry_price) / leverage, 2)
            logger.warning(f"仓位超限，已按可用余额50%上限调整：{contracts}张，保证金：{margin_usdt} USDT")

        logger.info(
            f"仓位计算 | 张数：{contracts} | 保证金：{margin_usdt} USDT"
        )

        return {
            "contracts":      contracts,
            "margin_usdt":    margin_usdt,
            "total_usdt":     free_usdt,
            "available_usdt": available_usdt,
            "used_margin":    used_margin,
        }

    except Exception as e:
        logger.error(f"仓位计算失败：{e}")
        return {"contracts": 0, "margin_usdt": 0}


# ── 余额获取 ───────────────────────────────────

def _get_usdt_balance(balance: dict) -> float:
    """
    兼容多种 fetch_balance 返回格式
    """
    # 格式1: balance["USDT"]["total"]
    if "USDT" in balance and isinstance(balance["USDT"], dict):
        return float(balance["USDT"].get("total", 0))
    
    # 格式2: balance["free"]["USDT"]
    if "free" in balance and isinstance(balance["free"], dict):
        return float(balance["free"].get("USDT", 0))
    
    # 格式3: balance["total"]["USDT"]
    if "total" in balance and isinstance(balance["total"], dict):
        return float(balance["total"].get("USDT", 0))
    
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