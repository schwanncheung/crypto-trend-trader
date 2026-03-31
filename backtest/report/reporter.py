"""
backtest/report/reporter.py

计算回测统计指标，生成 CSV / JSON 报告。
指标覆盖：总收益、年化收益、最大回撤、夏普比率、Calmar比率、
          胜率、盈亏比、期望值、平均持仓时间、品种分析等共12+项。
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class BacktestReporter:
    """
    接收 BacktestEngine.run() 返回的原始结果，计算统计指标，
    输出 JSON / CSV 报告。
    """

    ANNUALIZE_FACTOR = 365  # 按日历天数年化

    def __init__(self, results: dict, config: dict, output_dir: str | Path) -> None:
        """
        Parameters
        ----------
        results   : BacktestEngine.run() 的返回值
        config    : 合并后的完整配置字典（含 backtest / trading 两级）
        output_dir: 报告输出目录
        """
        self.results = results
        self.config = config
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.trades: list[dict] = results.get("trades", [])
        self.equity_curve: list[dict] = results.get("equity_curve", [])
        self.initial_balance: float = results.get("initial_balance", 10_000.0)
        self.final_balance: float = results.get("final_balance", self.initial_balance)
        self.start_date: str = results.get("start_date", "")
        self.end_date: str = results.get("end_date", "")

        self._stats: dict[str, Any] | None = None  # lazy cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_stats(self) -> dict[str, Any]:
        """计算所有统计指标，结果缓存后返回。"""
        if self._stats is not None:
            return self._stats

        logger.info("[reporter] 开始计算统计指标，共 %d 笔交易", len(self.trades))

        closed = [t for t in self.trades if t.get("status") == "closed"]
        stats: dict[str, Any] = {}

        stats["total_trades"] = len(closed)
        stats["initial_balance"] = self.initial_balance
        stats["final_balance"] = self.final_balance
        stats["net_pnl_usdt"] = self.final_balance - self.initial_balance
        stats["net_pnl_pct"] = (
            (self.final_balance - self.initial_balance) / self.initial_balance * 100
        )

        # --- 日历天数 ---
        calendar_days = self._calendar_days()
        stats["calendar_days"] = calendar_days

        # --- 年化收益（回测天数 < 30 天时无统计意义，置为 None）---
        if calendar_days >= 30:
            stats["annualized_return_pct"] = self._annualized_return(stats["net_pnl_pct"], calendar_days)
        else:
            stats["annualized_return_pct"] = None

        # --- 最大回撤 ---
        mdd_pct, mdd_usdt = self._max_drawdown()
        stats["max_drawdown_pct"] = mdd_pct
        stats["max_drawdown_usdt"] = mdd_usdt

        # --- 夏普比率 ---
        stats["sharpe_ratio"] = self._sharpe_ratio()

        # --- Calmar 比率（依赖年化收益，天数不足时同样置为 None）---
        if stats["annualized_return_pct"] is not None and mdd_pct != 0:
            stats["calmar_ratio"] = round(stats["annualized_return_pct"] / abs(mdd_pct), 2)
        else:
            stats["calmar_ratio"] = None

        # --- 胜率 ---
        wins = [t for t in closed if t.get("pnl_usdt", 0) > 0]
        losses = [t for t in closed if t.get("pnl_usdt", 0) <= 0]
        stats["win_count"] = len(wins)
        stats["loss_count"] = len(losses)
        stats["win_rate_pct"] = (
            len(wins) / len(closed) * 100 if closed else 0.0
        )

        # --- 盈亏比 ---
        avg_win = (
            sum(t["pnl_usdt"] for t in wins) / len(wins) if wins else 0.0
        )
        avg_loss = (
            abs(sum(t["pnl_usdt"] for t in losses) / len(losses)) if losses else 0.0
        )
        stats["avg_win_usdt"] = avg_win
        stats["avg_loss_usdt"] = avg_loss
        stats["profit_factor"] = (
            avg_win / avg_loss if avg_loss != 0 else float("inf")
        )

        # --- 期望值（每笔平均盈亏） ---
        stats["expectancy_usdt"] = (
            sum(t["pnl_usdt"] for t in closed) / len(closed) if closed else 0.0
        )

        # --- 平均持仓时间（分钟） ---
        stats["avg_hold_minutes"] = self._avg_hold_minutes(closed)

        # --- 按平仓原因分组 ---
        stats["close_reason_breakdown"] = self._reason_breakdown(closed)

        # --- 按品种分组 ---
        stats["per_symbol"] = self._per_symbol_stats(closed)

        # --- 连续亏损 ---
        stats["max_consecutive_losses"] = self._max_consecutive_losses(closed)

        # --- 回测元信息 ---
        stats["start_date"] = self.start_date
        stats["end_date"] = self.end_date
        stats["config_snapshot"] = {
            k: v
            for k, v in self.config.get("backtest", {}).items()
            if k not in ("data_cache_dir", "results_dir")
        }

        self._stats = stats
        logger.info(
            "[reporter] 统计完成：净盈亏=%.2f USDT (%.2f%%)，胜率=%.1f%%，MDD=%.2f%%",
            stats["net_pnl_usdt"],
            stats["net_pnl_pct"],
            stats["win_rate_pct"],
            stats["max_drawdown_pct"],
        )
        return stats

    def save_json(self, filename: str = "stats.json") -> Path:
        """将统计指标保存为 JSON 文件。"""
        stats = self.compute_stats()
        out = self.output_dir / filename
        with open(out, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2, default=str)
        logger.info("[reporter] 统计 JSON 已保存：%s", out)
        return out

    def save_trades_csv(self, filename: str = "trades.csv") -> Path:
        """将逐笔交易记录保存为 CSV 文件。"""
        closed = [t for t in self.trades if t.get("status") == "closed"]
        if not closed:
            logger.warning("[reporter] 无已平仓交易，跳过 CSV 输出")
            return self.output_dir / filename
        df = pd.DataFrame(closed)
        out = self.output_dir / filename
        df.to_csv(out, index=False, encoding="utf-8-sig")
        logger.info("[reporter] 交易明细 CSV 已保存：%s（%d 行）", out, len(df))
        return out

    def save_equity_csv(self, filename: str = "equity_curve.csv") -> Path:
        """将权益曲线保存为 CSV 文件。"""
        if not self.equity_curve:
            logger.warning("[reporter] 权益曲线为空，跳过 CSV 输出")
            return self.output_dir / filename
        df = pd.DataFrame(self.equity_curve)
        out = self.output_dir / filename
        df.to_csv(out, index=False, encoding="utf-8-sig")
        logger.info("[reporter] 权益曲线 CSV 已保存：%s（%d 行）", out, len(df))
        return out

    def save_html_report(self, filename: str = "report.html") -> Path | None:
        """使用 Jinja2 渲染 HTML 报告（需安装 jinja2）。"""
        try:
            from jinja2 import Environment, FileSystemLoader
        except ImportError:
            logger.warning("[reporter] jinja2 未安装，跳过 HTML 报告生成。运行 pip install jinja2")
            return None

        from datetime import datetime, timezone
        stats = self.compute_stats()
        template_dir = Path(__file__).parent / "templates"
        env = Environment(loader=FileSystemLoader(str(template_dir)))
        tmpl = env.get_template("report.html")
        context = {
            **stats,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        html = tmpl.render(**context)
        out = self.output_dir / filename
        out.write_text(html, encoding="utf-8")
        logger.info("[reporter] HTML 报告已保存：%s", out)
        return out

    def generate_all(self) -> dict[str, Path]:
        """一键生成全部报告文件，返回各文件路径字典。"""
        return {
            "stats_json": self.save_json(),
            "trades_csv": self.save_trades_csv(),
            "equity_csv": self.save_equity_csv(),
            "html_report": self.save_html_report(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _calendar_days(self) -> float:
        """从权益曲线首尾时间戳计算日历天数。"""
        if len(self.equity_curve) < 2:
            return 1.0
        t0 = self.equity_curve[0]["timestamp"]
        t1 = self.equity_curve[-1]["timestamp"]
        return max((t1 - t0) / 86_400_000, 1.0)  # ms → days

    def _annualized_return(self, net_pct: float, days: float) -> float:
        """将区间收益率年化（复利公式）。"""
        if days <= 0:
            return 0.0
        ratio = 1 + net_pct / 100
        if ratio <= 0:
            return -100.0
        return (ratio ** (self.ANNUALIZE_FACTOR / days) - 1) * 100

    def _max_drawdown(self) -> tuple[float, float]:
        """计算最大回撤百分比和 USDT 绝对值。"""
        if not self.equity_curve:
            return 0.0, 0.0
        equities = [row["equity"] for row in self.equity_curve]
        peak = equities[0]
        max_dd_pct = 0.0
        max_dd_usdt = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            dd_usdt = peak - eq
            dd_pct = dd_usdt / peak * 100 if peak > 0 else 0.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_usdt = dd_usdt
        return max_dd_pct, max_dd_usdt

    def _sharpe_ratio(self, risk_free_daily: float = 0.0) -> float:
        """基于每日权益变化计算夏普比率（年化）。"""
        if len(self.equity_curve) < 2:
            return 0.0
        df = pd.DataFrame(self.equity_curve)
        df["ts_day"] = (df["timestamp"] // 86_400_000).astype(int)
        daily = df.groupby("ts_day")["equity"].last()
        returns = daily.pct_change().dropna()
        if returns.std() == 0 or len(returns) < 2:
            return 0.0
        sharpe = (returns.mean() - risk_free_daily) / returns.std()
        return round(sharpe * math.sqrt(self.ANNUALIZE_FACTOR), 4)

    def _avg_hold_minutes(self, closed: list[dict]) -> float:
        """计算平均持仓时间（分钟）。"""
        durations = []
        for t in closed:
            ot = t.get("open_time")
            ct = t.get("close_time")
            if ot and ct:
                durations.append((ct - ot) / 60_000)  # ms → minutes
        return sum(durations) / len(durations) if durations else 0.0

    def _reason_breakdown(self, closed: list[dict]) -> dict[str, int]:
        """按平仓原因统计笔数。"""
        counts: dict[str, int] = defaultdict(int)
        for t in closed:
            reason = t.get("close_reason", "unknown")
            counts[reason] += 1
        return dict(counts)

    def _per_symbol_stats(self, closed: list[dict]) -> dict[str, dict]:
        """按交易品种分组计算胜率、PnL 等。"""
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in closed:
            groups[t.get("symbol", "UNKNOWN")].append(t)

        result = {}
        for sym, trades in groups.items():
            wins = [t for t in trades if t.get("pnl_usdt", 0) > 0]
            total_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
            result[sym] = {
                "total_trades": len(trades),
                "win_count": len(wins),
                "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
                "total_pnl_usdt": round(total_pnl, 4),
                "avg_pnl_usdt": round(total_pnl / len(trades), 4) if trades else 0.0,
            }
        return result

    def _max_consecutive_losses(self, closed: list[dict]) -> int:
        """计算最大连续亏损笔数。"""
        max_streak = 0
        streak = 0
        for t in closed:
            if t.get("pnl_usdt", 0) <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return max_streak



