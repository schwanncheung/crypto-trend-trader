#!/usr/bin/env python3
from __future__ import annotations
"""
backtest/data/feed.py
数据馈送模块

加载本地 Parquet 缓存，提供严格无前视偏差的多周期K线切片。
核心原则：任何时刻只能看到该时刻已收盘的K线，不得泄露未来数据。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

logger = logging.getLogger(__name__)


class DataFeed:
    """
    多周期数据馈送。

    使用方式：
        feed = DataFeed(cache_dir, symbols, timeframes, start_date, end_date)
        feed.load()
        for ts, bar in feed.iter_bars(base_timeframe):
            hist = feed.get_history(symbol, '1h', end_ts=ts, limit=200)
    """

    def __init__(
        self,
        cache_dir: str | Path,
        symbols: list[str],
        timeframes: list[str],
        start_date: str,
        end_date: str,
        base_timeframe: str | None = None,
    ):
        """
        参数：
            cache_dir      : Parquet 缓存根目录
            symbols        : 品种列表，如 ['BTC/USDT:USDT']
            timeframes     : 周期列表，从高到低，如 ['1h','30m','15m']
            start_date     : 回测起始日期字符串 'YYYY-MM-DD'
            end_date       : 回测截止日期字符串 'YYYY-MM-DD'
            base_timeframe : 驱动时钟的最低周期（默认取 timeframes 最后一个）
        """
        self.cache_dir = Path(cache_dir)
        self.symbols = symbols
        self.timeframes = timeframes
        self.start_date = start_date
        self.end_date = end_date
        self.base_timeframe = base_timeframe or timeframes[-1]

        self._tz = timezone.utc
        self._start_ms = int(
            datetime.fromisoformat(start_date).replace(tzinfo=self._tz).timestamp() * 1000
        )
        self._end_ms = int(
            datetime.fromisoformat(end_date).replace(tzinfo=self._tz).timestamp() * 1000
        )

        # 内部存储：{symbol: {timeframe: DataFrame}}
        # DataFrame 已按 timestamp 排序，timestamp 为 UTC ms int
        self._data: dict[str, dict[str, pd.DataFrame]] = {}
        self._loaded = False

    # ─────────────────────────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────────────────────────

    def load(self) -> None:
        """从 Parquet 缓存加载所有品种/周期数据到内存，裁剪到回测区间。"""
        logger.info(
            f"DataFeed 加载：{len(self.symbols)} 品种 × {len(self.timeframes)} 周期，"
            f"区间 {self.start_date} ~ {self.end_date}"
        )
        missing = []
        for symbol in self.symbols:
            self._data[symbol] = {}
            for tf in self.timeframes:
                path = self._cache_path(symbol, tf)
                if not path.exists():
                    missing.append(f"{symbol} {tf}")
                    logger.warning(f"  缓存不存在：{path}")
                    self._data[symbol][tf] = pd.DataFrame(
                        columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )
                    continue

                df = pd.read_parquet(path)
                df = df.sort_values("timestamp").reset_index(drop=True)

                # 裁剪到回测区间（包含 start，不含 end）
                df = df[(df["timestamp"] >= self._start_ms) & (df["timestamp"] < self._end_ms)]
                df = df.reset_index(drop=True)

                self._data[symbol][tf] = df
                logger.info(
                    f"  {symbol:30s} {tf:5s} 加载 {len(df):5d} 根  "
                    f"[{self._ts_to_str(df['timestamp'].iloc[0] if len(df) else 0)} ~ "
                    f"{self._ts_to_str(df['timestamp'].iloc[-1] if len(df) else 0)}]"
                )

        if missing:
            logger.warning(
                f"以下数据缺失，回测可能不完整：{missing}\n"
                f"请先运行：python backtest/data/downloader.py"
            )
        self._loaded = True
        logger.info("DataFeed 加载完成")

    def iter_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
    ) -> Iterator[tuple[int, dict]]:
        """
        按时间顺序逐根 yield (timestamp_ms, bar_dict)。
        bar_dict 键：open / high / low / close / volume

        参数：
            symbol    : 品种
            timeframe : 驱动周期（默认 self.base_timeframe）
        """
        self._ensure_loaded()
        tf = timeframe or self.base_timeframe
        df = self._data.get(symbol, {}).get(tf)
        if df is None or df.empty:
            logger.warning(f"iter_bars：{symbol} {tf} 无数据")
            return

        for row in df.itertuples(index=False):
            yield int(row.timestamp), {
                "open":   float(row.open),
                "high":   float(row.high),
                "low":    float(row.low),
                "close":  float(row.close),
                "volume": float(row.volume),
            }

    def get_history(
        self,
        symbol: str,
        timeframe: str,
        end_ts_ms: int,
        limit: int = 300,
    ) -> pd.DataFrame:
        """
        返回截至 end_ts_ms（不含）的最近 limit 根K线。
        严格无前视偏差：end_ts_ms 时刻的K线本身不包含在结果中。

        参数：
            symbol     : 品种
            timeframe  : 周期
            end_ts_ms  : 截止时间戳（Unix ms），该时刻及之后的K线不可见
            limit      : 最多返回的K线数量

        返回：
            DataFrame，列为 [timestamp, open, high, low, close, volume]
            按 timestamp 升序排列
        """
        self._ensure_loaded()
        df = self._data.get(symbol, {}).get(timeframe)
        if df is None or df.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        # 严格过滤：只取 timestamp < end_ts_ms 的K线
        visible = df[df["timestamp"] < end_ts_ms]
        if visible.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        return visible.iloc[-limit:].reset_index(drop=True)

    def get_all_timestamps(self, symbol: str) -> list[int]:
        """返回该品种在基础周期上所有时间戳（用于引擎主循环）"""
        self._ensure_loaded()
        df = self._data.get(symbol, {}).get(self.base_timeframe)
        if df is None or df.empty:
            return []
        return df["timestamp"].tolist()

    def available_symbols(self) -> list[str]:
        """返回有有效数据的品种列表"""
        self._ensure_loaded()
        result = []
        for symbol in self.symbols:
            tf_data = self._data.get(symbol, {})
            if any(not df.empty for df in tf_data.values()):
                result.append(symbol)
        return result

    def get_bar_at(self, symbol: str, timeframe: str, ts_ms: int) -> dict | None:
        """获取指定时间戳的单根K线，不存在返回 None"""
        self._ensure_loaded()
        df = self._data.get(symbol, {}).get(timeframe)
        if df is None or df.empty:
            return None
        row = df[df["timestamp"] == ts_ms]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "timestamp": int(r["timestamp"]),
            "open":      float(r["open"]),
            "high":      float(r["high"]),
            "low":       float(r["low"]),
            "close":     float(r["close"]),
            "volume":    float(r["volume"]),
        }

    # ─────────────────────────────────────────────────────────────────
    # 内部工具
    # ─────────────────────────────────────────────────────────────────

    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        symbol_safe = symbol.replace("/", "_").replace(":", "_")
        return self.cache_dir / symbol_safe / f"{timeframe}.parquet"

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("DataFeed 未加载，请先调用 feed.load()")

    @staticmethod
    def _ts_to_str(ts_ms: int | float) -> str:
        if not ts_ms:
            return "N/A"
        try:
            return datetime.utcfromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d")
        except Exception:
            return "N/A"
