#!/usr/bin/env python3
"""
K 线数据获取脚本
从交易所获取多时间框架 K 线数据，计算支撑阻力位和趋势结构
"""

import os
import time
import logging
import yaml
import ccxt
import pandas as pd
import numpy as np
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional
from pathlib import Path
import sys

# 加载环境变量
load_dotenv()

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

from config_loader import CFG, SCANNER_CFG, KLINE_CFG, BLACKLIST_CFG, TIMEFRAMES, setup_logging
setup_logging("fetch_kline")
logger = logging.getLogger(__name__)


_DEFAULT_LIMITS = {"15m": 300, "1h": 200, "4h": 200}
_KLINE_LIMITS = {**_DEFAULT_LIMITS, **KLINE_CFG.get("limits", {})}


def get_exchange():
    """创建交易所实例（与 execute_trade 保持一致，使用 OKX）"""
    from config_loader import EXCHANGE_CFG, EXCHANGE_API_KEY, EXCHANGE_API_SECRET, EXCHANGE_PASSPHRASE

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


def retry_on_error(func, max_retries: int = 3, delay: float = 1.0):
    """网络超时重试装饰器"""
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
        raise last_error
    return wrapper


@retry_on_error
def fetch_ohlcv(
    exchange,
    symbol: str,
    timeframe: str,
    limit: int = 200
) -> pd.DataFrame:
    """
    获取单周期 K 线数据
    symbol 格式必须与交易所一致：
      OKX     → "BTC/USDT:USDT"
      Binance → "BTC/USDT"
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df
    except Exception as e:
        logger.error(f"fetch_ohlcv 失败 | {symbol} {timeframe}：{e}")
        raise


def fetch_multi_timeframe(
    symbol: str,
    exchange=None,
    timeframes: list = None
) -> dict:
    """
    获取多周期 K 线数据
    """
    if exchange is None:
        from execute_trade import create_exchange
        exchange = create_exchange()
        logger.warning("fetch_multi_timeframe：未传入 exchange，内部临时创建")

    if timeframes is None:
        timeframes = TIMEFRAMES

    result = {}
    for tf in timeframes:
        try:
            limit = _KLINE_LIMITS.get(tf, 200)
            df = fetch_ohlcv(exchange, symbol, tf, limit=limit)
            result[tf] = df
            logger.info(f"  [{tf}] {symbol} 获取 {len(df)} 根 K 线 ✅")
        except Exception as e:
            logger.error(f"  [{tf}] {symbol} 获取失败：{e}")
            result[tf] = pd.DataFrame()

    return result


def calculate_support_resistance(
    df: pd.DataFrame,
    lookback: int = 5
) -> tuple:
    """计算支撑阻力位（基于波段高低点）"""
    if df.empty or len(df) < lookback:
        return [], []
    
    highs = df["high"].values
    lows = df["low"].values
    swing_highs = []
    swing_lows = []
    
    for i in range(lookback, len(df) - lookback):
        if highs[i] > highs[i-lookback:i].max() and highs[i] > highs[i+1:i+lookback+1].max():
            swing_highs.append(highs[i])
        if lows[i] < lows[i-lookback:i].min() and lows[i] < lows[i+1:i+lookback+1].min():
            swing_lows.append(lows[i])
    
    support_levels = sorted(set(swing_lows))[-3:] if swing_lows else []
    resistance_levels = sorted(set(swing_highs), reverse=True)[:3] if swing_highs else []
    
    return support_levels, resistance_levels


def calculate_volume_ma(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """计算成交量移动平均"""
    if df.empty or "volume" not in df.columns:
        return pd.Series()
    return df["volume"].rolling(window=period).mean()


def detect_trend_structure(df: pd.DataFrame) -> dict:
    """判断趋势结构（裸 K 方法）"""
    if df.empty or len(df) < 10:
        return {
            "trend": "sideways", "hh": False, "hl": False,
            "lh": False, "ll": False,
            "swing_highs": [], "swing_lows": [],
            "structure_broken": False
        }
    
    swing_highs = []
    swing_lows = []
    
    for i in range(5, len(df) - 5):
        if df["high"].iloc[i] > df["high"].iloc[i-5:i].max() and df["high"].iloc[i] > df["high"].iloc[i+1:i+6].max():
            swing_highs.append((i, df["high"].iloc[i]))
        if df["low"].iloc[i] < df["low"].iloc[i-5:i].min() and df["low"].iloc[i] < df["low"].iloc[i+1:i+6].min():
            swing_lows.append((i, df["low"].iloc[i]))
    
    swing_highs = swing_highs[-3:]
    swing_lows = swing_lows[-3:]
    prices_high = [h[1] for h in swing_highs]
    prices_low = [l[1] for l in swing_lows]
    
    hh = hl = lh = ll = False
    trend = "sideways"
    structure_broken = False
    
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1][1] > swing_highs[-2][1]
        hl = swing_lows[-1][1] > swing_lows[-2][1]
        lh = swing_highs[-1][1] < swing_highs[-2][1]
        ll = swing_lows[-1][1] < swing_lows[-2][1]
        
        if hh and hl:
            trend = "up"
        elif lh and ll:
            trend = "down"
        
        if len(swing_highs) >= 2:
            last_close = df["close"].iloc[-1]
            if last_close > swing_highs[-2][1]:
                structure_broken = True
    
    return {
        "trend": trend, "hh": hh, "hl": hl, "lh": lh, "ll": ll,
        "swing_highs": prices_high, "swing_lows": prices_low,
        "structure_broken": structure_broken
    }


if __name__ == "__main__":
    print("获取 BTC/USDT:USDT 多周期数据...")
    data = fetch_multi_timeframe("BTC/USDT:USDT")
    for tf, df in data.items():
        print(f"\n[{tf}] 最新 5 根 K 线：")
        print(df.tail(5).to_string())
    if not data["4h"].empty:
        structure = detect_trend_structure(data["4h"])
        print(f"\n4H 趋势结构：{structure}")
        support, resistance = calculate_support_resistance(data["4h"])
        print(f"4H 支撑位：{support}")
        print(f"4H 阻力位：{resistance}")


# ── 动态热门合约获取 ─────────────────────────

def fetch_hot_symbols(
    exchange,
    top_n: int = 20,
    min_volume_usdt: float = 50_000_000,
    max_price_usdt: float = 0,
) -> list[str]:
    """获取热门合约列表，自动过滤黑名单"""
    try:
        logger.info("正在从 OKX 获取热门合约列表...")
        tickers = exchange.fetch_tickers(params={"instType": "SWAP"})

        hot_list = []
        for symbol, ticker in tickers.items():
            if not symbol.endswith(":USDT"):
                continue

            base = symbol.split("/")[0]
            stable_coins = {"USDC", "TUSD", "BUSD", "DAI", "FDUSD"}
            if base in stable_coins:
                continue

            info = ticker.get("info", {})
            quote_volume = float(info.get("volCcy24h", 0)) or 0

            if quote_volume == 0:
                close = ticker.get("close", 0) or 0
                base_vol = ticker.get("baseVolume", 0) or 0
                quote_volume = close * base_vol

            if quote_volume < min_volume_usdt:
                continue

            price = ticker.get("last", 0) or 0
            if max_price_usdt > 0 and price > max_price_usdt:
                continue

            hot_list.append({
                "symbol": symbol,
                "volume_usdt": quote_volume,
                "price": price,
                "change_pct": ticker.get("percentage", 0),
            })

        hot_list.sort(key=lambda x: x["volume_usdt"], reverse=True)
        top_symbols = [item["symbol"] for item in hot_list[:top_n]]

        # 黑名单过滤
        if BLACKLIST_CFG:
            filtered = []
            for sym in top_symbols:
                base_name = sym.split("/")[0]
                is_blacklisted = False
                for bl in BLACKLIST_CFG:
                    if bl == sym or bl == base_name:
                        is_blacklisted = True
                        break
                if not is_blacklisted:
                    filtered.append(sym)
                else:
                    logger.info(f"  ⏭️  黑名单过滤：{sym}")
            if len(filtered) < len(top_symbols):
                logger.info(f"黑名单过滤：{len(top_symbols)} → {len(filtered)} 个合约")
            top_symbols = filtered

        # 打印排行榜
        logger.info(f"✅ 热门合约获取成功，共 {len(top_symbols)} 个：")
        for i, item in enumerate(hot_list[:top_n], 1):
            if item["symbol"] in top_symbols:
                logger.info(
                    f"  {i:>2}. {item['symbol']:<25} "
                    f"24h 量：{item['volume_usdt']/1e8:.2f}亿 USDT  "
                    f"涨跌：{item['change_pct']:+.2f}%"
                )
            else:
                logger.info(f"  {i:>2}. {item['symbol']:<25} [黑名单已过滤]")

        return top_symbols

    except Exception as e:
        logger.error(f"获取热门合约失败：{e}")
        return _load_fallback_symbols()


def _load_fallback_symbols() -> list[str]:
    """兜底列表，格式严格使用 OKX 永续合约格式"""
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config" / "symbols.yaml"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        symbols = cfg.get("priority", [])

        valid = [s for s in symbols if s.endswith(":USDT")]
        invalid = [s for s in symbols if not s.endswith(":USDT")]
        if invalid:
            logger.warning(f"兜底列表中发现非 OKX 格式合约，已过滤：{invalid}")

        logger.warning(f"使用兜底合约列表，共 {len(valid)} 个")
        return valid

    except Exception as e:
        logger.error(f"兜底合约列表加载失败：{e}")
        return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]


def filter_symbols_by_trend(
    symbols: list[str],
    exchange: object,
    exclude_sideways: bool = True
) -> list[str]:
    """用日线趋势预过滤合约列表"""
    if not exclude_sideways:
        return symbols

    filtered = []
    for symbol in symbols:
        try:
            df = fetch_ohlcv(exchange, symbol, "1d", limit=50)
            structure = detect_trend_structure(df)
            if structure.get("trend") != "sideways":
                filtered.append(symbol)
                logger.info(f"  ✅ {symbol} 趋势：{structure['trend']}")
            else:
                logger.info(f"  ⏭️  {symbol} 横盘，已过滤")
        except Exception as e:
            logger.warning(f"  ⚠️  {symbol} 预过滤失败，保留：{e}")
            filtered.append(symbol)

    logger.info(f"趋势预过滤：{len(symbols)} → {len(filtered)} 个合约")
    return filtered
