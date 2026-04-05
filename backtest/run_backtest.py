#!/usr/bin/env python3
"""
backtest/run_backtest.py

CLI 入口：支持三种模式
  backtest  — 单次回测 + 报告生成
  optimize  — 网格搜索优化
  download  — 下载/更新历史数据

示例::

  # 下载数据
  python backtest/run_backtest.py download \\
      --symbols BTC/USDT:USDT ETH/USDT:USDT \\
      --timeframes 15m 1h 4h \\
      --start 2024-01-01

  # 单次回测
  python backtest/run_backtest.py backtest \\
      --start 2024-01-01 --end 2025-01-01

  # 网格优化
  python backtest/run_backtest.py optimize \\
      --start 2024-01-01 --end 2025-01-01 --workers 4
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT))

from backtest.config_loader import load_config  # noqa: E402

logger = logging.getLogger(__name__)


# ==================================================================
# 子命令实现
# ==================================================================

def cmd_download(args: argparse.Namespace, config: dict) -> None:
    """下载/增量更新历史 K 线数据。"""
    from backtest.data.downloader import download_all
    from datetime import date

    symbols = args.symbols or config.get("symbols", [])
    # --timeframes 未指定时从 backtest.yaml 的 download_timeframes 读取，再兜底默认值
    timeframes = args.timeframes or config["backtest"].get("download_timeframes", ["5m", "15m", "1h"])
    start = args.start or config["backtest"].get("start_date", "2024-01-01")
    # --end 未指定时默认今天，不从 backtest.yaml 取，避免固定值截断下载范围
    end = args.end or date.today().isoformat()
    cache_dir = config["backtest"].get("data_cache_dir", "backtest/data/cache")

    logger.info("[download] 品种=%s 时间框架=%s 起始=%s", symbols, timeframes, start)
    stats = download_all(symbols, timeframes, start, end, cache_dir)
    for sym, tf_stats in stats.items():
        for tf, count in tf_stats.items():
            logger.info("  %-25s %-6s  +%d 条", sym, tf, count)
    print(f"\n下载完成，共 {sum(sum(v.values()) for v in stats.values())} 条新数据。")


def cmd_backtest(args: argparse.Namespace, config: dict) -> None:
    """执行单次回测并生成报告。"""
    from backtest.data.feed import DataFeed
    from backtest.engine.engine import BacktestEngine
    from backtest.report.reporter import BacktestReporter
    from backtest.report.visualizer import BacktestVisualizer
    import datetime

    # 日期覆盖
    if args.start:
        config["backtest"]["start_date"] = args.start
    if args.end:
        config["backtest"]["end_date"] = args.end

    data_dir = config["backtest"].get("data_cache_dir", "backtest/data/cache")
    results_base = config["backtest"].get("results_dir", "backtest/results")
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(results_base) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("[backtest] 加载数据...")
    feed = DataFeed(
        cache_dir=data_dir,
        symbols=config.get("symbols") or _discover_symbols(data_dir),
        timeframes=config.get("timeframes", ["1h", "15m", "5m"]),
        start_date=config["backtest"]["start_date"],
        end_date=config["backtest"]["end_date"],
    )
    feed.load()

    logger.info("[backtest] 开始回测 %s ~ %s",
                config["backtest"]["start_date"],
                config["backtest"]["end_date"])
    from backtest.sig.ai_mock import RuleOnlyMock, LLMMockCache, LLMRealAnalyzer
    from backtest.sig.pipeline import SignalPipeline
    ai_mode = config.get("backtest", {}).get("ai_mode", "rule_only")
    if ai_mode == "llm_mock":
        ai_mock = LLMMockCache(config, cache_dir=config["backtest"].get("data_cache_dir", "backtest/data/cache"))
    elif ai_mode == "llm_real":
        ai_mock = LLMRealAnalyzer(config)
    else:
        ai_mock = RuleOnlyMock(config)
    pipeline = SignalPipeline(config, ai_mock)
    engine = BacktestEngine(config, feed, pipeline)
    results = engine.run()

    logger.info("[backtest] 生成报告...")
    reporter = BacktestReporter(results, config, output_dir=output_dir)
    stats = reporter.compute_stats()
    paths = reporter.generate_all()

    visualizer = BacktestVisualizer(results, stats, output_dir=output_dir)
    chart_paths = visualizer.generate_all()
    paths.update(chart_paths)

    _print_summary(stats)
    print(f"\n报告已保存至：{output_dir}")
    for name, p in paths.items():
        if p:
            print(f"  {name:20s}: {p}")

def cmd_optimize(args: argparse.Namespace, config: dict) -> None:
    """执行网格搜索优化。"""
    from backtest.optimizer import GridOptimizer
    import datetime

    if args.start:
        config["backtest"]["start_date"] = args.start
    if args.end:
        config["backtest"]["end_date"] = args.end

    data_dir = config["backtest"].get("data_cache_dir", "backtest/data/cache")
    results_base = config["backtest"].get("results_dir", "backtest/results")
    run_id = "opt_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(results_base) / run_id

    optimizer = GridOptimizer(config, data_dir=data_dir)
    results = optimizer.run(
        workers=args.workers,
        top_n=args.top_n,
    )
    saved = optimizer.save_results(results, output_dir=output_dir)

    print("\n=== 优化完成 ===")
    print(f"最优参数（OOS）：{results['best_params']}")
    print(f"结果目录：{output_dir}")
    for name, p in saved.items():
        print(f"  {name:20s}: {p}")


# ==================================================================
# 工具函数
# ==================================================================

def _discover_symbols(cache_dir: str) -> list[str]:
    """从缓存目录自动发现已下载的品种（子目录名还原为 symbol 格式）"""
    p = Path(cache_dir)
    if not p.exists():
        return []
    symbols = []
    for d in sorted(p.iterdir()):
        if d.is_dir() and any(d.glob("*.parquet")):
            # BTC_USDT_USDT → BTC/USDT:USDT
            name = d.name
            if name.count("_") >= 2:
                idx = name.rfind("_")
                name = name[:idx] + ":" + name[idx + 1:]
                idx = name.find("_")
                name = name[:idx] + "/" + name[idx + 1:]
            symbols.append(name)
    return symbols

def _print_summary(stats: dict) -> None:
    """在终端打印关键指标摘要。"""
    ann_ret = stats.get('annualized_return_pct')
    calmar = stats.get('calmar_ratio')
    sharpe = stats.get('sharpe_ratio', 0)

    ann_ret_str = f"{ann_ret:.2f} %" if ann_ret is not None else "N/A (回测天数不足30天)"
    calmar_str  = f"{calmar:.2f}"    if calmar  is not None else "N/A"
    sharpe_str  = f"{sharpe:.2f}"   if sharpe else "N/A"

    lines = [
        "\n" + "=" * 60,
        "  回测结果摘要",
        "=" * 60,
        f"  净收益 (PnL)  : {stats.get('net_pnl_usdt', 0):>10.2f} USDT  ({stats.get('net_pnl_pct', 0):.2f}%)",
        f"  年化收益      : {ann_ret_str:>35s}",
        f"  最大回撤      : {stats.get('max_drawdown_pct', 0):>10.2f} %",
        f"  夏普比率      : {sharpe_str:>10s}  (日收益波动率调整，>1为优)",
        f"  Calmar 比率   : {calmar_str:>10s}  (年化收益/最大回撤，>3为优)",
        f"  总交易次数    : {stats.get('total_trades', 0):>10d}",
        f"  胜率          : {stats.get('win_rate_pct', 0):>10.1f} %",
        f"  盈亏比        : {stats.get('profit_factor', 0):>10.2f}",
        f"  期望值        : {stats.get('expectancy_usdt', 0):>10.2f} USDT/笔",
        f"  平均持仓      : {stats.get('avg_hold_minutes', 0):>10.0f} min",
        f"  最大连亏      : {stats.get('max_consecutive_losses', 0):>10d} 笔",
        "=" * 60,
    ]
    print("\n".join(lines))

# ==================================================================
# Argument parser
# ==================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_backtest",
        description="Crypto Trend Trader 回测系统",
    )
    parser.add_argument(
        "--config", default=None,
        help="回测配置文件路径（默认：backtest/config/backtest.yaml）",
    )
    parser.add_argument(
        "--settings", default=None,
        help="生产配置文件路径（默认：config/settings.yaml）",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认：INFO）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- download ---
    dl = sub.add_parser("download", help="下载/更新历史 K 线数据")
    dl.add_argument("--symbols", nargs="+", help="合约列表，如 BTC/USDT:USDT")
    dl.add_argument("--timeframes", nargs="+", default=None,
                    help="周期列表，如 15m 30m 1h 4h（默认读取 backtest.yaml 的 download_timeframes）")
    dl.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    dl.add_argument("--end", default=None,
                    help="结束日期 YYYY-MM-DD（默认：今天）")

    # --- backtest ---
    bt = sub.add_parser("backtest", help="单次回测")
    bt.add_argument("--start", default=None, help="回测起始日期")
    bt.add_argument("--end", default=None, help="回测结束日期")

    # --- optimize ---
    op = sub.add_parser("optimize", help="网格搜索参数优化")
    op.add_argument("--start", default=None, help="回测起始日期")
    op.add_argument("--end", default=None, help="回测结束日期")
    op.add_argument("--workers", type=int, default=None, help="并行进程数（默认：CPU-1）")
    op.add_argument("--top-n", type=int, default=20, dest="top_n",
                    help="训练集 Top-N 参数组进行 OOS 验证")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    import time as _time

    class CSTFormatter(logging.Formatter):
        """强制使用北京时间（UTC+8）格式化日志时间。"""
        _CST_OFFSET = 8 * 3600
        def converter(self, timestamp):
            return _time.gmtime(timestamp + self._CST_OFFSET)
        def formatTime(self, record, datefmt=None):
            ct = self.converter(record.created)
            if datefmt:
                return _time.strftime(datefmt, ct)
            return _time.strftime("%Y-%m-%d %H:%M:%S", ct) + f",{int(record.msecs):03d}"

    fmt = CSTFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, args.log_level))
    root.addHandler(handler)

    config = load_config(
        backtest_yaml=args.config,
        settings_yaml=args.settings,
    )

    dispatch = {
        "download": cmd_download,
        "backtest": cmd_backtest,
        "optimize": cmd_optimize,
    }
    dispatch[args.command](args, config)


if __name__ == "__main__":
    main()


