"""
backtest/report/visualizer.py

使用 Plotly 生成交互式回测图表：
  1. 权益曲线（含买卖标记）
  2. 每笔交易 PnL 柱状图
  3. 月度收益热力图
  4. 胜率 / 平仓原因饼图

所有图表输出为独立 HTML 文件（不依赖外部服务器）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import plotly.graph_objects as go
    import plotly.subplots as sp
    import plotly.express as px
    _PLOTLY_AVAILABLE = True
except ImportError:
    _PLOTLY_AVAILABLE = False
    logger.warning("[visualizer] plotly 未安装，图表功能不可用。运行 pip install plotly")


class BacktestVisualizer:
    """
    接收 BacktestEngine.run() 结果 + 统计指标，生成 Plotly 图表 HTML。
    """

    def __init__(self, results: dict, stats: dict, output_dir: str | Path) -> None:
        self.results = results
        self.stats = stats
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.trades: list[dict] = results.get("trades", [])
        self.equity_curve: list[dict] = results.get("equity_curve", [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plot_equity_curve(self, filename: str = "equity_curve.html") -> Path | None:
        """绘制权益曲线，标记开/平仓点。"""
        if not _PLOTLY_AVAILABLE or not self.equity_curve:
            return None

        df_eq = pd.DataFrame(self.equity_curve)
        df_eq["dt"] = pd.to_datetime(df_eq["timestamp"], unit="ms", utc=True)

        closed = [t for t in self.trades if t.get("status") == "closed"]

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=df_eq["dt"],
                y=df_eq["equity"],
                mode="lines",
                name="权益",
                line=dict(color="#4A90D9", width=1.5),
            )
        )

        # 盈利平仓标记（绿色三角）
        win_trades = [t for t in closed if t.get("pnl_usdt", 0) > 0]
        if win_trades:
            win_times = pd.to_datetime([t["close_time"] for t in win_trades], unit="ms", utc=True)
            win_eq = self._equity_at_times([t["close_time"] for t in win_trades], df_eq)
            fig.add_trace(
                go.Scatter(
                    x=win_times, y=win_eq, mode="markers",
                    name="盈利平仓",
                    marker=dict(symbol="triangle-up", color="#2ECC71", size=8),
                )
            )

        # 亏损平仓标记（红色三角）
        loss_trades = [t for t in closed if t.get("pnl_usdt", 0) <= 0]
        if loss_trades:
            loss_times = pd.to_datetime([t["close_time"] for t in loss_trades], unit="ms", utc=True)
            loss_eq = self._equity_at_times([t["close_time"] for t in loss_trades], df_eq)
            fig.add_trace(
                go.Scatter(
                    x=loss_times, y=loss_eq, mode="markers",
                    name="亏损平仓",
                    marker=dict(symbol="triangle-down", color="#E74C3C", size=8),
                )
            )

        net_pct = self.stats.get("net_pnl_pct", 0)
        mdd = self.stats.get("max_drawdown_pct", 0)
        sharpe = self.stats.get("sharpe_ratio", 0)
        fig.update_layout(
            title=f"权益曲线  净收益={net_pct:.2f}%  MDD={mdd:.2f}%  Sharpe={sharpe:.2f}",
            xaxis_title="时间", yaxis_title="权益 (USDT)",
            template="plotly_dark", height=500,
        )
        out = self.output_dir / filename
        fig.write_html(str(out), include_plotlyjs="cdn")
        logger.info("[visualizer] 权益曲线已保存：%s", out)
        return out

    def plot_pnl_histogram(self, filename: str = "pnl_histogram.html") -> Path | None:
        """每笔交易 PnL 柱状图（盈绿亏红）。"""
        if not _PLOTLY_AVAILABLE:
            return None
        closed = [t for t in self.trades if t.get("status") == "closed"]
        if not closed:
            return None

        df = pd.DataFrame(closed)
        df["color"] = df["pnl_usdt"].apply(lambda x: "#2ECC71" if x > 0 else "#E74C3C")
        df["dt"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

        fig = go.Figure(
            go.Bar(
                x=df["dt"], y=df["pnl_usdt"],
                marker_color=df["color"],
                name="每笔 PnL",
                hovertext=df.apply(
                    lambda r: f"{r.get('symbol','')} {r.get('side','')} {r.get('close_reason','')}", axis=1
                ),
            )
        )
        win_rate = self.stats.get("win_rate_pct", 0)
        pf = self.stats.get("profit_factor", 0)
        fig.update_layout(
            title=f"逐笔 PnL  胜率={win_rate:.1f}%  盈亏比={pf:.2f}",
            xaxis_title="平仓时间", yaxis_title="PnL (USDT)",
            template="plotly_dark", height=400,
        )
        out = self.output_dir / filename
        fig.write_html(str(out), include_plotlyjs="cdn")
        logger.info("[visualizer] PnL 柱状图已保存：%s", out)
        return out

    def plot_monthly_heatmap(self, filename: str = "monthly_heatmap.html") -> Path | None:
        """月度收益热力图（行=年，列=月）。"""
        if not _PLOTLY_AVAILABLE or not self.equity_curve:
            return None

        df = pd.DataFrame(self.equity_curve)
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("dt").resample("ME")["equity"].last().dropna()
        monthly_ret = df.pct_change().dropna() * 100

        pivot = pd.DataFrame({
            "year": monthly_ret.index.year,
            "month": monthly_ret.index.month,
            "ret": monthly_ret.values,
        }).pivot(index="year", columns="month", values="ret")

        month_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                        "Jul","Aug","Sep","Oct","Nov","Dec"]
        fig = go.Figure(
            go.Heatmap(
                z=pivot.values,
                x=[month_labels[m-1] for m in pivot.columns],
                y=[str(y) for y in pivot.index],
                colorscale="RdYlGn",
                zmid=0,
                text=[[f"{v:.1f}%" if not pd.isna(v) else "" for v in row] for row in pivot.values],
                texttemplate="%{text}",
            )
        )
        fig.update_layout(
            title="月度收益热力图 (%)",
            template="plotly_dark", height=350,
        )
        out = self.output_dir / filename
        fig.write_html(str(out), include_plotlyjs="cdn")
        logger.info("[visualizer] 月度热力图已保存：%s", out)
        return out

    def plot_close_reason_pie(self, filename: str = "close_reasons.html") -> Path | None:
        """平仓原因饼图。"""
        if not _PLOTLY_AVAILABLE:
            return None
        breakdown = self.stats.get("close_reason_breakdown", {})
        if not breakdown:
            return None

        # 平仓原因中英文映射
        reason_cn = {
            "sl": "止损触发",
            "tp": "止盈触发",
            "partial_tp1": "第一批分批止盈",
            "partial_tp2": "第二批分批止盈",
            "trailing_sl": "移动止损触发",
            "eod": "回测结束平仓",
            "structure_break_long_1h": "多头结构破坏",
            "structure_break_short_1h": "空头结构破坏",
            "momentum_decay": "动量衰减出场",
        }

        # 转换标签为中文
        labels = []
        for k in breakdown.keys():
            # 处理 support_break_* 和 resistance_break_*
            if k.startswith("support_break_"):
                labels.append("多头跌破支撑")
            elif k.startswith("resistance_break_"):
                labels.append("空头突破阻力")
            else:
                labels.append(reason_cn.get(k, k))

        fig = go.Figure(
            go.Pie(
                labels=labels,
                values=list(breakdown.values()),
                hole=0.4,
            )
        )
        fig.update_layout(
            title="平仓原因分布",
            template="plotly_dark", height=400,
        )
        out = self.output_dir / filename
        fig.write_html(str(out), include_plotlyjs="cdn")
        logger.info("[visualizer] 平仓原因饼图已保存：%s", out)
        return out

    def generate_all(self) -> dict[str, Path | None]:
        """生成全部图表，返回各文件路径字典。"""
        return {
            "equity_curve": self.plot_equity_curve(),
            "pnl_histogram": self.plot_pnl_histogram(),
            "monthly_heatmap": self.plot_monthly_heatmap(),
            "close_reasons": self.plot_close_reason_pie(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _equity_at_times(self, timestamps_ms: list[int], df_eq: pd.DataFrame) -> list[float]:
        """从权益曲线 DataFrame 中查找最近的权益值（用于标记点）。"""
        result = []
        eq_arr = df_eq["timestamp"].values
        val_arr = df_eq["equity"].values
        for ts in timestamps_ms:
            idx = (abs(eq_arr - ts)).argmin()
            result.append(float(val_arr[idx]))
        return result



