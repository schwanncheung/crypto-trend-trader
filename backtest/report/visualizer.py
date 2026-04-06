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

    def plot_daily_heatmap(self, filename: str = "daily_heatmap.html") -> Path | None:
        """日收益热力图（日历格式：行=周，列=星期几）。"""
        if not _PLOTLY_AVAILABLE or not self.equity_curve:
            return None

        df = pd.DataFrame(self.equity_curve)
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["date"] = df["dt"].dt.date

        # 计算每日收盘权益
        daily_equity = df.groupby("date")["equity"].last().reset_index()
        daily_equity["daily_ret"] = daily_equity["equity"].pct_change() * 100

        # 转换为日历格式
        daily_equity["date"] = pd.to_datetime(daily_equity["date"])
        daily_equity["weekday"] = daily_equity["date"].dt.dayofweek  # 0=Monday, 6=Sunday
        daily_equity["week"] = daily_equity["date"].dt.isocalendar().week
        daily_equity["year"] = daily_equity["date"].dt.year
        daily_equity["day_label"] = daily_equity["date"].dt.strftime("%m/%d")

        # 创建透视表（行=年+周，列=星期几）
        pivot = daily_equity.pivot_table(
            index=["year", "week"],
            columns="weekday",
            values="daily_ret",
            aggfunc="first"
        )

        # 创建标签
        day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        week_labels = [f"W{w}" for y, w in pivot.index]

        # 构建 hover 文本（显示日期和收益）
        hover_text = []
        for i, (idx, row) in enumerate(pivot.iterrows()):
            year, week = idx
            week_row = []
            for j, val in enumerate(row):
                if pd.notna(val):
                    # 找到对应的日期
                    mask = (daily_equity["year"] == year) & (daily_equity["week"] == week) & (daily_equity["weekday"] == j)
                    if mask.any():
                        date_str = daily_equity.loc[mask, "day_label"].values[0]
                        week_row.append(f"{date_str}<br>{val:.2f}%")
                    else:
                        week_row.append(f"{val:.2f}%")
                else:
                    week_row.append("")
            hover_text.append(week_row)

        fig = go.Figure(
            go.Heatmap(
                z=pivot.values,
                x=day_labels,
                y=week_labels,
                colorscale="RdYlGn",
                zmid=0,
                text=hover_text,
                texttemplate="%{text}",
                hovertemplate="%{text}<extra></extra>",
                colorbar=dict(title="日收益(%)"),
            )
        )
        fig.update_layout(
            title="日收益热力图（日历视图）",
            xaxis_title="星期",
            yaxis_title="周",
            template="plotly_dark",
            height=max(300, len(week_labels) * 40 + 100),
        )
        out = self.output_dir / filename
        fig.write_html(str(out), include_plotlyjs="cdn")
        logger.info("[visualizer] 日收益热力图已保存：%s", out)
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

    def plot_analysis_dimensions(self, filename: str = "analysis_dimensions.html") -> Path | None:
        """分析维度可视化（裸K策略优化核心）。"""
        if not _PLOTLY_AVAILABLE:
            return None

        analysis = self.stats.get("analysis_dimensions", {})
        if not analysis:
            return None

        # 创建子图布局：3行2列
        fig = sp.make_subplots(
            rows=3, cols=2,
            subplot_titles=(
                "R:R 比率分布", "ADX 值域分布",
                "RSI 值域分布", "EMA 对齐得分",
                "K线形态分布", "入场时段分布"
            ),
            vertical_spacing=0.12,
            horizontal_spacing=0.08,
        )

        # 中文标签映射
        rr_labels = {
            "rr_lt_1.5": "<1.5",
            "rr_1.5_2.0": "1.5-2.0",
            "rr_2.0_2.5": "2.0-2.5",
            "rr_2.5_3.0": "2.5-3.0",
            "rr_ge_3.0": ">3.0",
        }
        adx_labels = {
            "adx_weak": "<20(弱趋势)",
            "adx_forming": "20-25(形成中)",
            "adx_medium": "25-35(中等)",
            "adx_strong": ">35(强趋势)",
        }
        rsi_labels = {
            "rsi_oversold": "<30(超卖)",
            "rsi_neutral_weak": "30-50(偏弱)",
            "rsi_neutral_strong": "50-70(偏强)",
            "rsi_overbought": ">70(超买)",
        }
        hour_labels = {
            "asia": "亚洲时段(0-8)",
            "europe": "欧洲时段(8-16)",
            "america": "美洲时段(16-24)",
        }

        # ── 1. R:R 比率分布（胜率柱状图）──────────────────────────────
        rr_data = analysis.get("risk_reward", {})
        if rr_data:
            labels = [rr_labels.get(k, k) for k in rr_data.keys()]
            win_rates = [v.get("win_rate_pct", 0) for v in rr_data.values()]
            counts = [v.get("count", 0) for v in rr_data.values()]
            colors = ["#2ECC71" if wr >= 50 else "#E74C3C" for wr in win_rates]
            fig.add_trace(
                go.Bar(x=labels, y=win_rates, marker_color=colors,
                       name="胜率%", text=[f"{wr:.1f}% ({c}笔)" for wr, c in zip(win_rates, counts)],
                       textposition="auto", textfont=dict(size=11, color="#f2f5fa")),
                row=1, col=1
            )

        # ── 2. ADX 值域分布 ─────────────────────────────────────────────
        adx_data = analysis.get("adx_distribution", {})
        if adx_data:
            labels = [adx_labels.get(k, k) for k in adx_data.keys()]
            win_rates = [v.get("win_rate_pct", 0) for v in adx_data.values()]
            counts = [v.get("count", 0) for v in adx_data.values()]
            colors = ["#2ECC71" if wr >= 50 else "#E74C3C" for wr in win_rates]
            fig.add_trace(
                go.Bar(x=labels, y=win_rates, marker_color=colors,
                       name="胜率%", text=[f"{wr:.1f}% ({c}笔)" for wr, c in zip(win_rates, counts)],
                       textposition="auto", textfont=dict(size=11, color="#f2f5fa")),
                row=1, col=2
            )

        # ── 3. RSI 值域分布 ─────────────────────────────────────────────
        rsi_data = analysis.get("rsi_distribution", {})
        if rsi_data:
            labels = [rsi_labels.get(k, k) for k in rsi_data.keys()]
            win_rates = [v.get("win_rate_pct", 0) for v in rsi_data.values()]
            counts = [v.get("count", 0) for v in rsi_data.values()]
            colors = ["#2ECC71" if wr >= 50 else "#E74C3C" for wr in win_rates]
            fig.add_trace(
                go.Bar(x=labels, y=win_rates, marker_color=colors,
                       name="胜率%", text=[f"{wr:.1f}% ({c}笔)" for wr, c in zip(win_rates, counts)],
                       textposition="auto", textfont=dict(size=11, color="#f2f5fa")),
                row=2, col=1
            )

        # ── 4. EMA 对齐得分分布 ─────────────────────────────────────────────
        ema_data = analysis.get("ema_score_distribution", {})
        if ema_data:
            labels = list(ema_data.keys())
            win_rates = [v.get("win_rate_pct", 0) for v in ema_data.values()]
            counts = [v.get("count", 0) for v in ema_data.values()]
            colors = ["#2ECC71" if wr >= 50 else "#E74C3C" for wr in win_rates]
            fig.add_trace(
                go.Bar(x=labels, y=win_rates, marker_color=colors,
                       name="胜率%", text=[f"{wr:.1f}% ({c}笔)" for wr, c in zip(win_rates, counts)],
                       textposition="auto", textfont=dict(size=11, color="#f2f5fa")),
                row=2, col=2
            )

        # ── 5. K线形态分布 ─────────────────────────────────────────────
        pattern_data = analysis.get("pattern_distribution", {})
        if pattern_data:
            labels = list(pattern_data.keys())
            win_rates = [v.get("win_rate_pct", 0) for v in pattern_data.values()]
            counts = [v.get("count", 0) for v in pattern_data.values()]
            colors = ["#2ECC71" if wr >= 50 else "#E74C3C" for wr in win_rates]
            fig.add_trace(
                go.Bar(x=labels, y=win_rates, marker_color=colors,
                       name="胜率%", text=[f"{wr:.1f}% ({c}笔)" for wr, c in zip(win_rates, counts)],
                       textposition="auto", textfont=dict(size=11, color="#f2f5fa")),
                row=3, col=1
            )

        # ── 6. 入场时段分布 ─────────────────────────────────────────────
        hour_data = analysis.get("hour_distribution", {})
        if hour_data:
            labels = [hour_labels.get(k, k) for k in hour_data.keys()]
            win_rates = [v.get("win_rate_pct", 0) for v in hour_data.values()]
            counts = [v.get("count", 0) for v in hour_data.values()]
            colors = ["#2ECC71" if wr >= 50 else "#E74C3C" for wr in win_rates]
            fig.add_trace(
                go.Bar(x=labels, y=win_rates, marker_color=colors,
                       name="胜率%", text=[f"{wr:.1f}% ({c}笔)" for wr, c in zip(win_rates, counts)],
                       textposition="auto", textfont=dict(size=11, color="#f2f5fa")),
                row=3, col=2
            )

        # 更新布局 + 显式设置所有 Y 轴范围（避免 Plotly subplot 轴更新不一致）
        # 注意：range=[0, 110] 留出空间显示 outside 文本（避免高胜率时文字被裁剪）
        fig.update_layout(
            title_text="分析维度统计（胜率按分组）",
            template="plotly_dark",
            height=900,
            showlegend=False,
            yaxis=dict(range=[0, 110]),
            yaxis2=dict(range=[0, 110]),
            yaxis3=dict(range=[0, 110]),
            yaxis4=dict(range=[0, 110]),
            yaxis5=dict(range=[0, 110]),
            yaxis6=dict(range=[0, 110]),
        )

        out = self.output_dir / filename
        fig.write_html(str(out), include_plotlyjs="cdn")
        logger.info("[visualizer] 分析维度图表已保存：%s", out)
        return out

    def generate_all(self) -> dict[str, Path | None]:
        """生成全部图表，返回各文件路径字典。"""
        return {
            "equity_curve": self.plot_equity_curve(),
            "pnl_histogram": self.plot_pnl_histogram(),
            "daily_heatmap": self.plot_daily_heatmap(),
            "close_reasons": self.plot_close_reason_pie(),
            "analysis_dimensions": self.plot_analysis_dimensions(),
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



