#!/usr/bin/env python3
from __future__ import annotations
"""
backtest/data/downloader.py
历史K线数据下载器

从 OKX 拉取历史 OHLCV 数据并缓存为 Parquet 文件。
支持增量更新：仅下载缓存中缺失的时段。

用法：
  python backtest/data/downloader.py --symbols BTC/USDT:USDT ETH/USDT:USDT \
                                      --timeframes 15m 30m 1h \
                                      --start 2024-01-01
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv
from tqdm import tqdm

# ── 路径设置 ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from config_loader import (
    EXCHANGE_CFG,
    setup_logging,
)

load_dotenv(PROJECT_ROOT / ".env")
setup_logging("backtest_downloader")
logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────
# OKX 每次最多返回 300 根 K 线
_MAX_LIMIT_PER_REQUEST = 300

# 各周期对应的毫秒数（用于翻页计算）
_TF_MS = {
    "1m":   60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
    "1w":  604_800_000,
}


def _get_exchange() -> ccxt.okx:
    """创建 OKX 交易所实例（只读，不需要私钥也可拉取公开K线）"""
    exchange = ccxt.okx({
        # 历史K线是公开接口，不传 API Key，避免测试环境Key被正式环境拒绝（错误50101）
        "options":  {"defaultType": "swap"},
        "enableRateLimit": True,
    })
    return exchange


def _cache_path(symbol: str, timeframe: str, cache_dir: Path) -> Path:
    """返回该品种/周期的 Parquet 缓存路径"""
    symbol_safe = symbol.replace("/", "_").replace(":", "_")
    return cache_dir / symbol_safe / f"{timeframe}.parquet"


def _load_existing(path: Path) -> pd.DataFrame:
    """加载已有缓存，若不存在返回空 DataFrame"""
    if path.exists():
        try:
            df = pd.read_parquet(path)
            logger.debug(f"  缓存加载：{path}，共 {len(df)} 根")
            return df
        except Exception as e:
            logger.warning(f"  缓存读取失败，将重新下载：{e}")
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def _save_cache(df: pd.DataFrame, path: Path) -> None:
    """保存 DataFrame 到 Parquet，自动创建目录"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    logger.debug(f"  缓存保存：{path}，共 {len(df)} 根")


def _fetch_range(
    exchange: ccxt.okx,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    分页拉取指定时间范围内的 OHLCV 数据。
    since_ms: 起始时间（包含），Unix 毫秒
    until_ms: 截止时间（不包含），Unix 毫秒
    """
    tf_ms = _TF_MS.get(timeframe)
    if tf_ms is None:
        raise ValueError(f"不支持的时间框架：{timeframe}")

    all_rows = []
    cursor = since_ms
    batch_count = 0

    while cursor < until_ms:
        for attempt in range(max_retries):
            try:
                raw = exchange.fetch_ohlcv(
                    symbol,
                    timeframe=timeframe,
                    since=cursor,
                    limit=_MAX_LIMIT_PER_REQUEST,
                )
                break
            except ccxt.NetworkError as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"  网络错误，{wait}s 后重试：{e}")
                    time.sleep(wait)
                else:
                    raise
            except ccxt.RateLimitExceeded:
                logger.warning("  触发频率限制，等待5s")
                time.sleep(5)

        if not raw:
            break

        # OKX 返回格式：[timestamp, open, high, low, close, volume]
        # 过滤超出 until_ms 的数据
        rows = [r for r in raw if r[0] < until_ms]
        all_rows.extend(rows)
        batch_count += 1

        last_ts = raw[-1][0]
        if last_ts >= until_ms or len(raw) < _MAX_LIMIT_PER_REQUEST:
            break

        cursor = last_ts + tf_ms
        # 遵守速率限制
        time.sleep(exchange.rateLimit / 1000)

    if not all_rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    logger.debug(f"  拉取 {batch_count} 批，获得 {len(df)} 根K线")
    return df


def download_symbol(
    exchange: ccxt.okx,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str | None,
    cache_dir: Path,
) -> int:
    """
    下载单个品种单个周期的历史数据，支持增量更新。
    返回本次新增的K线数量。
    """
    cache_path = _cache_path(symbol, timeframe, cache_dir)
    existing = _load_existing(cache_path)

    # 确定起止时间（毫秒）
    tz = timezone.utc
    since_ms = int(datetime.fromisoformat(start_date).replace(tzinfo=tz).timestamp() * 1000)
    if end_date:
        until_ms = int(datetime.fromisoformat(end_date).replace(tzinfo=tz).timestamp() * 1000)
    else:
        # 未指定 end 时，取当前时间截断到本周期整点
        # 避免"最后一批不足300根"的提前退出逻辑误判为已拉完
        tf_ms = _TF_MS.get(timeframe, 1)
        now_ms = int(datetime.now(tz).timestamp() * 1000)
        until_ms = (now_ms // tf_ms) * tf_ms

    # 增量更新：若缓存已有数据，从最新时间戳+1个周期开始
    if not existing.empty:
        max_ts = int(existing["timestamp"].max())
        tf_ms = _TF_MS.get(timeframe, 0)
        incremental_since = max_ts + tf_ms
        if incremental_since >= until_ms:
            logger.info(f"  {symbol} {timeframe} 缓存已是最新，跳过")
            return 0
        logger.info(
            f"  {symbol} {timeframe} 增量更新："
            f"{datetime.utcfromtimestamp(incremental_since/1000).strftime('%Y-%m-%d')} → "
            f"{datetime.utcfromtimestamp(until_ms/1000).strftime('%Y-%m-%d')}"
        )
        since_ms = incremental_since
    else:
        logger.info(
            f"  {symbol} {timeframe} 全量下载："
            f"{start_date} → {end_date or '今天'}"
        )

    new_data = _fetch_range(exchange, symbol, timeframe, since_ms, until_ms)

    if new_data.empty:
        logger.info(f"  {symbol} {timeframe} 无新数据")
        return 0

    # 合并并去重
    combined = pd.concat([existing, new_data], ignore_index=True)
    combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    _save_cache(combined, cache_path)

    added = len(new_data)
    logger.info(f"  {symbol} {timeframe} 新增 {added} 根，总计 {len(combined)} 根")
    return added


def download_all(
    symbols: list[str],
    timeframes: list[str],
    start_date: str,
    end_date: str | None = None,
    cache_dir: Path | None = None,
) -> dict:
    """
    批量下载多个品种、多个周期的历史数据。
    返回下载统计 {symbol: {timeframe: added_count}}
    """
    if cache_dir is None:
        cache_dir = PROJECT_ROOT / "backtest" / "data" / "cache"
    cache_dir = Path(cache_dir)

    exchange = _get_exchange()
    stats = {}
    total_tasks = len(symbols) * len(timeframes)

    logger.info(
        f"开始下载历史数据：{len(symbols)} 个品种 × {len(timeframes)} 个周期 "
        f"= {total_tasks} 个任务"
    )
    logger.info(f"时间范围：{start_date} ~ {end_date or '今天'}")
    logger.info(f"缓存目录：{cache_dir}")

    with tqdm(total=total_tasks, desc="下载进度", unit="任务") as pbar:
        for symbol in symbols:
            stats[symbol] = {}
            for tf in timeframes:
                pbar.set_description(f"{symbol} {tf}")
                try:
                    added = download_symbol(
                        exchange, symbol, tf, start_date, end_date, cache_dir
                    )
                    stats[symbol][tf] = added
                except Exception as e:
                    logger.error(f"  {symbol} {tf} 下载失败：{e}")
                    stats[symbol][tf] = -1
                pbar.update(1)

    # 汇总日志
    total_added = sum(
        v for sym_stats in stats.values() for v in sym_stats.values() if v > 0
    )
    logger.info(f"下载完成，共新增 {total_added} 根K线")
    return stats


def main():
    parser = argparse.ArgumentParser(description="OKX 历史K线数据下载器")
    parser.add_argument(
        "--symbols", nargs="+",
        default=["BTC/USDT:USDT", "ETH/USDT:USDT"],
        help="合约品种列表，如 BTC/USDT:USDT ETH/USDT:USDT"
    )
    parser.add_argument(
        "--timeframes", nargs="+",
        default=["5m", "15m", "1h"],
        help="时间框架列表，如 5m 15m 1h"
    )
    parser.add_argument(
        "--start", default="2024-01-01",
        help="起始日期 YYYY-MM-DD"
    )
    parser.add_argument(
        "--end", default=None,
        help="截止日期 YYYY-MM-DD（默认：今天）"
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="缓存目录（默认 backtest/data/cache）"
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = download_all(
        symbols=args.symbols,
        timeframes=args.timeframes,
        start_date=args.start,
        end_date=end,
        cache_dir=cache_dir,
    )

    print("\n下载统计：")
    for symbol, tf_stats in stats.items():
        for tf, count in tf_stats.items():
            status = f"+{count}根" if count >= 0 else "失败"
            print(f"  {symbol:30s} {tf:5s} {status}")


if __name__ == "__main__":
    main()
