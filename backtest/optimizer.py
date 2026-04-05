"""
backtest/optimizer.py

网格搜索优化器：
  - 从 param_grid.yaml 生成笛卡尔积参数组合
  - 多进程并行运行 BacktestEngine
  - 80/20 训练集/测试集拆分（样本外验证）
  - 按优化目标排序，输出 Top-N 结果
  - 支持约束过滤（最小交易数、最大回撤、最低胜率）
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import multiprocessing
import time
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

logger = logging.getLogger(__name__)


def _discover_symbols(cache_dir: str) -> list[str]:
    """从缓存目录自动发现已下载的品种。"""
    p = Path(cache_dir)
    if not p.exists():
        return []
    symbols = []
    for d in sorted(p.iterdir()):
        if d.is_dir() and any(d.glob("*.parquet")):
            name = d.name
            if name.count("_") >= 2:
                idx = name.rfind("_")
                name = name[:idx] + ":" + name[idx + 1:]
                idx = name.find("_")
                name = name[:idx] + "/" + name[idx + 1:]
            symbols.append(name)
    return symbols


def _fix_signal_module() -> None:
    """子进程初始化器：修复 backtest/signal/ 遮蔽标准库 signal 模块的问题。
    backtest/ 目录在 sys.path 中时，`import signal` 会命中本包而非标准库，
    导致 multiprocessing 内部依赖的 signal.SIGINT 不存在。
    """
    import sys
    import importlib.util
    # 取出 backtest/ 目录路径，在搜索标准库时跳过它
    skip = {p for p in sys.path if p.endswith("backtest") or p.endswith("backtest/")}
    search_path = [p for p in sys.path if p not in skip]
    spec = importlib.util.find_spec("signal", search_path)
    if spec is not None and spec.origin and "backtest" not in spec.origin:
        # 已经是标准库，无需修复
        return
    # 强制从标准库路径加载
    stdlib_paths = [p for p in sys.path if "site-packages" not in p and p not in skip
                    and p != "" and "backtest" not in p]
    spec = importlib.util.find_spec("signal", stdlib_paths)
    if spec is not None:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["signal"] = mod


def _run_single(args: tuple) -> dict | None:
    """
    顶层函数（必须可 pickle）：运行单次回测，返回指标字典。
    params_override 会注入到 config['backtest']['override']。
    """
    config, params_override, data_dir = args
    try:
        # 延迟导入，避免主进程 import 时触发副作用
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from backtest.data.feed import DataFeed
        from backtest.engine.engine import BacktestEngine
        from backtest.report.reporter import BacktestReporter
        from backtest.sig.ai_mock import RuleOnlyMock
        from backtest.sig.pipeline import SignalPipeline

        cfg = copy.deepcopy(config)
        cfg.setdefault("backtest", {})
        cfg["backtest"].setdefault("override", {})
        cfg["backtest"]["override"].update(params_override)

        symbols = cfg.get("symbols") or _discover_symbols(data_dir)
        feed = DataFeed(
            cache_dir=data_dir,
            symbols=symbols,
            timeframes=cfg.get("timeframes", ["1h", "15m", "5m"]),
            start_date=cfg["backtest"]["start_date"],
            end_date=cfg["backtest"]["end_date"],
        )
        feed.load()

        ai_mock = RuleOnlyMock(cfg)
        pipeline = SignalPipeline(cfg, ai_mock)
        engine = BacktestEngine(cfg, feed, pipeline)
        results = engine.run()

        reporter = BacktestReporter(results, cfg, output_dir="/tmp/_opt_scratch")
        stats = reporter.compute_stats()
        stats["params"] = params_override
        return stats
    except Exception as exc:  # noqa: BLE001
        logger.warning("[optimizer] 参数组运行失败: %s | 错误: %s", params_override, exc)
        return None

class GridOptimizer:
    """
    网格搜索优化器。

    用法::

        opt = GridOptimizer(config, data_dir="backtest/data/cache")
        results = opt.run()
        opt.save_results(results, "backtest/results/optimization")
    """

    def __init__(self, config: dict, data_dir: str | Path) -> None:
        """
        Parameters
        ----------
        config   : 合并后的完整配置（含 backtest 节）
        data_dir : Parquet 缓存目录
        """
        self.config = config
        self.data_dir = Path(data_dir)
        self.opt_config = self._load_opt_config()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(
        self,
        workers: int | None = None,
        top_n: int = 20,
    ) -> dict[str, Any]:
        """
        执行完整优化流程：训练集搜索 → 约束过滤 → OOS 验证。

        Returns
        -------
        dict with keys: train_results, oos_results, best_params, summary_df
        """
        grid = self._build_grid()
        train_cfg, test_cfg = self._split_date_range()

        logger.info(
            "[optimizer] 参数组合数=%d，workers=%s，训练期=%s~%s，测试期=%s~%s",
            len(grid), workers or "auto",
            train_cfg["backtest"]["start_date"], train_cfg["backtest"]["end_date"],
            test_cfg["backtest"]["start_date"], test_cfg["backtest"]["end_date"],
        )

        # --- 训练集并行搜索 ---
        train_results = self._run_parallel(train_cfg, grid, workers)
        train_results = self._apply_constraints(train_results)
        train_results = self._sort_results(train_results)

        logger.info("[optimizer] 训练集有效结果=%d，取 Top-%d 进行 OOS 验证", len(train_results), top_n)

        # --- OOS 验证（仅对 Top-N）---
        top_params = [r["params"] for r in train_results[:top_n]]
        oos_results = self._run_parallel(test_cfg, top_params, workers)
        oos_results = self._sort_results(oos_results)

        best_params = oos_results[0]["params"] if oos_results else (top_params[0] if top_params else {})
        logger.info("[optimizer] OOS 最优参数: %s", best_params)

        return {
            "train_results": train_results,
            "oos_results": oos_results,
            "best_params": best_params,
        }

    def save_results(
        self, results: dict[str, Any], output_dir: str | Path
    ) -> dict[str, Path]:
        """将优化结果保存为 JSON + CSV。"""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        saved: dict[str, Path] = {}

        # 最优参数
        best_path = out / "best_params.json"
        with open(best_path, "w", encoding="utf-8") as f:
            json.dump(results["best_params"], f, ensure_ascii=False, indent=2)
        saved["best_params"] = best_path

        # 训练集汇总
        if results["train_results"]:
            df_train = pd.DataFrame(results["train_results"])
            train_path = out / "train_results.csv"
            df_train.to_csv(train_path, index=False, encoding="utf-8-sig")
            saved["train_csv"] = train_path

        # OOS 汇总
        if results["oos_results"]:
            df_oos = pd.DataFrame(results["oos_results"])
            oos_path = out / "oos_results.csv"
            df_oos.to_csv(oos_path, index=False, encoding="utf-8-sig")
            saved["oos_csv"] = oos_path

        logger.info("[optimizer] 优化结果已保存到：%s", out)
        return saved

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _load_opt_config(self) -> dict:
        """从 backtest/config/param_grid.yaml 加载优化配置。"""
        grid_path = Path(__file__).parent / "config" / "param_grid.yaml"
        if not grid_path.exists():
            raise FileNotFoundError(f"param_grid.yaml 不存在：{grid_path}")
        with open(grid_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _build_grid(self) -> list[dict]:
        """生成所有参数组合（笛卡尔积）。"""
        raw: dict[str, list] = self.opt_config.get("param_grid", {})
        if not raw:
            raise ValueError("param_grid.yaml 中 param_grid 节为空")
        keys = list(raw.keys())
        values = list(raw.values())
        combos = list(itertools.product(*values))
        grid = [dict(zip(keys, combo)) for combo in combos]
        logger.info("[optimizer] 参数网格大小：%d 组", len(grid))
        return grid

    def _split_date_range(self) -> tuple[dict, dict]:
        """按 train_ratio 拆分回测日期区间，返回 (train_cfg, test_cfg)。"""
        ratio = float(self.opt_config.get("train_ratio", 0.8))
        start = pd.Timestamp(self.config["backtest"]["start_date"])
        end = pd.Timestamp(self.config["backtest"]["end_date"])
        split = start + (end - start) * ratio

        def _make_cfg(s: pd.Timestamp, e: pd.Timestamp) -> dict:
            cfg = copy.deepcopy(self.config)
            cfg["backtest"]["start_date"] = s.strftime("%Y-%m-%d")
            cfg["backtest"]["end_date"] = e.strftime("%Y-%m-%d")
            return cfg

        return _make_cfg(start, split), _make_cfg(split, end)

    def _run_parallel(
        self, config: dict, param_list: list[dict], workers: int | None
    ) -> list[dict]:
        """并行运行回测，返回非 None 结果列表。"""
        if workers is None:
            workers = max(1, multiprocessing.cpu_count() - 1)

        args = [(config, params, str(self.data_dir)) for params in param_list]
        t0 = time.monotonic()

        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=workers, initializer=_fix_signal_module) as pool:
            raw = pool.map(_run_single, args)

        results = [r for r in raw if r is not None]
        elapsed = time.monotonic() - t0
        logger.info(
            "[optimizer] 并行完成：%d/%d 成功，耗时 %.1fs",
            len(results), len(param_list), elapsed,
        )
        return results

    def _apply_constraints(self, results: list[dict]) -> list[dict]:
        """过滤不满足约束条件的结果。"""
        constraints = self.opt_config.get("constraints", {})
        min_trades = int(constraints.get("min_trades", 0))
        max_dd = float(constraints.get("max_drawdown_pct", 100.0))
        min_wr = float(constraints.get("min_win_rate_pct", 0.0))

        filtered = [
            r for r in results
            if r.get("total_trades", 0) >= min_trades
            and r.get("max_drawdown_pct", 0) <= max_dd
            and r.get("win_rate_pct", 0) >= min_wr
        ]
        logger.info(
            "[optimizer] 约束过滤：%d → %d 组", len(results), len(filtered)
        )
        return filtered

    def _sort_results(self, results: list[dict]) -> list[dict]:
        """按优化目标降序排序。"""
        target = self.opt_config.get("optimize_target", "sharpe_ratio")
        return sorted(results, key=lambda r: r.get(target, float("-inf")), reverse=True)



