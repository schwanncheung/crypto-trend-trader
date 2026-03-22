#!/usr/bin/env python3
"""
K线数据获取脚本
从交易所获取多时间框架K线数据，计算支撑阻力位和趋势结构
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

# 配置日志：同时输出到控制台和文件
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "fetch_kline.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

from config_loader import CFG, SCANNER_CFG


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
    获取单周期K线数据
    symbol 格式必须与交易所一致：
      OKX    → "BTC/USDT:USDT"
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
    获取多周期K线数据
    """
    if exchange is None:
        # 避免每次都重新创建连接，从 execute_trade 复用
        from execute_trade import create_exchange
        exchange = create_exchange()
        logger.warning("fetch_multi_timeframe：未传入 exchange，内部临时创建")

    if timeframes is None:
        timeframes = ["15m", "1h", "4h", "1d"]

    result = {}
    for tf in timeframes:
        try:
            df = fetch_ohlcv(exchange, symbol, tf)
            result[tf] = df
            logger.info(f"  [{tf}] {symbol} 获取 {len(df)} 根K线 ✅")
        except Exception as e:
            logger.error(f"  [{tf}] {symbol} 获取失败：{e}")
            result[tf] = pd.DataFrame()  # 返回空 DataFrame 而不是崩溃

    return result


def calculate_support_resistance(
    df: pd.DataFrame,
    lookback: int = 5
) -> tuple:
    """
    计算支撑阻力位（基于波段高低点）
    
    Args:
        df: K线数据
        lookback: 回溯周期
    
    Returns:
        (support_levels: list, resistance_levels: list)
    """
    if df.empty or len(df) < lookback:
        return [], []
    
    highs = df["high"].values
    lows = df["low"].values
    
    # 找出波段高点（局部极值）
    swing_highs = []
    swing_lows = []
    
    for i in range(lookback, len(df) - lookback):
        # 波段高点
        if (
            highs[i] > highs[i-lookback:i].max() and 
            highs[i] > highs[i+1:i+lookback+1].max()
        ):
            swing_highs.append(highs[i])
        
        # 波段低点
        if (
            lows[i] < lows[i-lookback:i].min() and 
            lows[i] < lows[i+1:i+lookback+1].min()
        ):
            swing_lows.append(lows[i])
    
    # 取最近3个
    support_levels = sorted(set(swing_lows))[-3:] if swing_lows else []
    resistance_levels = sorted(set(swing_highs), reverse=True)[:3] if swing_highs else []
    
    return support_levels, resistance_levels


def calculate_volume_ma(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """
    计算成交量移动平均
    
    Args:
        df: K线数据
        period: 均线周期
    
    Returns:
        成交量均线 Series
    """
    if df.empty or "volume" not in df.columns:
        return pd.Series()
    
    return df["volume"].rolling(window=period).mean()


def detect_trend_structure(df: pd.DataFrame) -> dict:
    """
    判断趋势结构（裸K方法）
    
    Args:
        df: K线数据
    
    Returns:
        {
            "trend": "up" / "down" / "sideways",
            "hh": bool,   # 是否创新高（Higher High）
            "hl": bool,   # 是否高于前低（Higher Low）
            "lh": bool,   # 是否低于前高（Lower High）
            "ll": bool,   # 是否创新低（Lower Low）
            "swing_highs": [价格列表],  # 最近3个波段高点
            "swing_lows":  [价格列表],  # 最近3个波段低点
            "structure_broken": bool    # 结构是否被打破（BOS）
        }
    """
    if df.empty or len(df) < 10:
        return {
            "trend": "sideways",
            "hh": False, "hl": False,
            "lh": False, "ll": False,
            "swing_highs": [], "swing_lows": [],
            "structure_broken": False
        }
    
    # 计算波段高低点
    swing_highs = []
    swing_lows = []
    
    for i in range(5, len(df) - 5):
        # 波段高点
        if (
            df["high"].iloc[i] > df["high"].iloc[i-5:i].max() and 
            df["high"].iloc[i] > df["high"].iloc[i+1:i+6].max()
        ):
            swing_highs.append((i, df["high"].iloc[i]))
        
        # 波段低点
        if (
            df["low"].iloc[i] < df["low"].iloc[i-5:i].min() and 
            df["low"].iloc[i] < df["low"].iloc[i+1:i+6].min()
        ):
            swing_lows.append((i, df["low"].iloc[i]))
    
    # 取最近3个
    swing_highs = swing_highs[-3:]
    swing_lows = swing_lows[-3:]
    
    prices_high = [h[1] for h in swing_highs]
    prices_low = [l[1] for l in swing_lows]
    
    # 判断趋势
    hh = False
    hl = False
    lh = False
    ll = False
    trend = "sideways"
    structure_broken = False
    
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        # HH: 当前高点高于前高点
        hh = swing_highs[-1][1] > swing_highs[-2][1]
        # HL: 当前低点高于前低点
        hl = swing_lows[-1][1] > swing_lows[-2][1]
        # LH: 当前高点低于前高点
        lh = swing_highs[-1][1] < swing_highs[-2][1]
        # LL: 当前低点低于前低点
        ll = swing_lows[-1][1] < swing_lows[-2][1]
        
        if hh and hl:
            trend = "up"
        elif lh and ll:
            trend = "down"
        
        # 结构打破：突破前一个波段高低点
        if len(swing_highs) >= 2:
            # 向上突破前高
            last_close = df["close"].iloc[-1]
            if last_close > swing_highs[-2][1]:
                structure_broken = True
    
    return {
        "trend": trend,
        "hh": hh,
        "hl": hl,
        "lh": lh,
        "ll": ll,
        "swing_highs": prices_high,
        "swing_lows": prices_low,
        "structure_broken": structure_broken
    }


if __name__ == "__main__":
    # 测试：打印 BTC/USDT:USDT 四个周期数据概览
    print("获取 BTC/USDT:USDT 多周期数据...")
    data = fetch_multi_timeframe("BTC/USDT:USDT")
    
    for tf, df in data.items():
        print(f"\n[{tf}] 最新5根K线：")
        print(df.tail(5).to_string())
    
    # 测试趋势结构
    if not data["4h"].empty:
        structure = detect_trend_structure(data["4h"])
        print(f"\n4H趋势结构：{structure}")
    
    # 测试支撑阻力
    if not data["4h"].empty:
        support, resistance = calculate_support_resistance(data["4h"])
        print(f"\n4H 支撑位：{support}")
        print(f"4H 阻力位：{resistance}")


# ── 动态热门合约获取 ─────────────────────────

def fetch_hot_symbols(
    exchange,
    top_n: int = 20,
    min_volume_usdt: float = 50_000_000
) -> list[str]:
    try:
        logger.info("正在从 OKX 获取热门合约列表...")
        tickers = exchange.fetch_tickers(params={"instType": "SWAP"})

        hot_list = []
        for symbol, ticker in tickers.items():

            # OKX 永续合约格式验证：必须以 :USDT 结尾
            if not symbol.endswith(":USDT"):
                continue

            # 过滤稳定币合约（USDC/USDT、TUSD/USDT 等）
            base = symbol.split("/")[0]
            stable_coins = {"USDC", "TUSD", "BUSD", "DAI", "FDUSD"}
            if base in stable_coins:
                continue

            # OKX: quoteVolume 为 None，使用 info 中的 volCcy24h（24h成交额USDT）
            # 或者用 close * baseVolume 计算
            info = ticker.get("info", {})
            quote_volume = float(info.get("volCcy24h", 0)) or 0
            
            # 如果 volCcy24h 为 0，用 close * baseVolume 估算
            if quote_volume == 0:
                close = ticker.get("close", 0) or 0
                base_vol = ticker.get("baseVolume", 0) or 0
                quote_volume = close * base_vol
            
            if quote_volume < min_volume_usdt:
                continue

            hot_list.append({
                "symbol": symbol,           # 已是 OKX 格式 "BTC/USDT:USDT"
                "volume_usdt": quote_volume,
                "price": ticker.get("last", 0),
                "change_pct": ticker.get("percentage", 0),
            })

        hot_list.sort(key=lambda x: x["volume_usdt"], reverse=True)
        top_symbols = [item["symbol"] for item in hot_list[:top_n]]

        # 打印排行榜
        logger.info(f"✅ 热门合约获取成功，共 {len(top_symbols)} 个：")
        for i, item in enumerate(hot_list[:top_n], 1):
            logger.info(
                f"  {i:>2}. {item['symbol']:<25} "
                f"24h量：{item['volume_usdt']/1e8:.2f}亿USDT  "
                f"涨跌：{item['change_pct']:+.2f}%"
            )

        return top_symbols

    except Exception as e:
        logger.error(f"获取热门合约失败：{e}")
        return _load_fallback_symbols()


def _load_fallback_symbols() -> list[str]:
    """
    兜底列表，格式严格使用 OKX 永续合约格式
    """
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config" / "symbols.yaml"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        symbols = cfg.get("priority", [])

        # 格式校验：过滤掉不符合 OKX 格式的条目
        valid = [s for s in symbols if s.endswith(":USDT")]
        invalid = [s for s in symbols if not s.endswith(":USDT")]
        if invalid:
            logger.warning(f"兜底列表中发现非OKX格式合约，已过滤：{invalid}")

        logger.warning(f"使用兜底合约列表，共 {len(valid)} 个")
        return valid

    except Exception as e:
        logger.error(f"兜底合约列表加载失败：{e}")
        return [
            "BTC/USDT:USDT",
            "ETH/USDT:USDT",
            "SOL/USDT:USDT",
        ]


def filter_symbols_by_trend(
    symbols: list[str],
    exchange: object,
    exclude_sideways: bool = True
) -> list[str]:
    """
    可选：用日线趋势预过滤合约列表
    去掉横盘合约，减少后续AI分析的无效调用

    参数：
        symbols: 合约列表
        exchange: ccxt 实例
        exclude_sideways: 是否过滤横盘合约

    返回：
        过滤后的合约列表
    """
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