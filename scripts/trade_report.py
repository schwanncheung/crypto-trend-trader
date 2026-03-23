#!/usr/bin/env python3
"""
trade_report.py
合约平仓后汇总交易日志，生成飞书通知报告并存储至 logs/reports/
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from notifier import send_notification

logger = logging.getLogger(__name__)

TRADES_DIR    = Path("logs/trades")
DECISIONS_DIR = Path("logs/decisions")
REPORTS_DIR   = Path("logs/reports")


def generate_close_report(
    symbol: str,
    reason: str,
    final_pnl: float,
    final_pnl_pct: float,
) -> None:
    """
    平仓后调用：汇总该品种当日所有日志，生成报告并发送飞书通知。
    symbol:        如 'LDO/USDT:USDT'
    reason:        平仓原因
    final_pnl:     最终盈亏（USDT）
    final_pnl_pct: 最终浮盈亏百分比（%）
    """
    try:
        symbol_safe = symbol.replace("/", "_").replace(":", "_")
        today = datetime.now(timezone.utc).strftime("%Y%m%d")

        # ── 1. 读取开仓记录 ──
        open_log = _find_latest_log(TRADES_DIR, symbol_safe, prefix="", exclude_prefix="position_", date=today)
        open_data = _load_json(open_log) if open_log else {}

        entry_price  = open_data.get("entry_price", "N/A")
        contracts    = open_data.get("contracts", "N/A")
        margin_usdt  = open_data.get("margin_usdt", "N/A")
        leverage     = open_data.get("leverage", 10)
        stop_loss    = open_data.get("stop_loss", "N/A")
        take_profit  = open_data.get("take_profit", "N/A")
        signal       = open_data.get("signal", "N/A")
        open_ts      = open_data.get("timestamp", "N/A")

        # 风险回报比
        rr = "N/A"
        if entry_price != "N/A" and stop_loss != "N/A" and take_profit != "N/A":
            sl_dist = abs(float(entry_price) - float(stop_loss))
            tp_dist = abs(float(take_profit) - float(entry_price))
            if sl_dist > 0:
                rr = f"1:{tp_dist / sl_dist:.1f}"

        # ── 2. 读取持仓快照（时间线）──
        position_logs = sorted(
            [f for f in TRADES_DIR.glob(f"position_{symbol_safe}_*.json")],
            key=lambda f: f.name
        )
        timeline_events = []
        prev_contracts = None
        for plog in position_logs:
            d = _load_json(plog)
            if not d:
                continue
            ts  = d.get("timestamp", "")
            ct  = d.get("contracts", 0)
            pnl = d.get("pnl_pct", "")
            ep  = d.get("entry_price", "")
            event = ""
            if prev_contracts is None:
                event = "开仓快照"
            elif float(ct) < float(prev_contracts or ct):
                event = f"部分止盈（{prev_contracts}→{ct}张）"
            else:
                event = "持仓巡检"
            timeline_events.append({
                "ts": ts, "contracts": ct,
                "entry_price": ep, "pnl_pct": pnl, "event": event
            })
            prev_contracts = ct

        # ── 3. 读取 AI 决策 ──
        decision_log = _find_latest_log(DECISIONS_DIR, symbol_safe, prefix="", date=today)
        decision_data = {}
        if decision_log:
            raw = _load_json(decision_log)
            decision_data = raw.get("decision", raw)

        signal_type    = decision_data.get("signal_type", "")
        volume_note    = decision_data.get("volume_note", "")
        trend_strength = decision_data.get("trend_strength", "")
        confidence     = decision_data.get("confidence", "")
        tf_alignment   = decision_data.get("timeframe_alignment", {})
        reason_text    = decision_data.get("reason", "")
        warning_text   = decision_data.get("warning", "")
        adx_note       = ""
        if reason_text:
            # 提取 ADX 数值（格式如 ADX59.36）
            import re
            m = re.search(r"ADX[=\s]?([\d.]+)", reason_text)
            if m:
                adx_note = f"ADX={m.group(1)} 强势趋势"

        # ── 4. 组装报告文本 ──
        side_label = "LONG（做多）" if signal == "long" else "SHORT（做空）" if signal == "short" else signal
        pnl_sign   = "+" if final_pnl >= 0 else ""
        status_icon = "✅ 盈利" if final_pnl >= 0 else "❌ 亏损/止损"

        tf_str = "/".join(tf_alignment.keys()) if tf_alignment else "多周期"
        tf_vals = "/".join(tf_alignment.values()) if tf_alignment else ""
        alignment_str = f"多周期共振 ({tf_str} 均为 {tf_vals})"

        # 时间线文本
        timeline_lines = []
        step = 1
        if open_ts != "N/A":
            open_dt = _fmt_ts(open_ts)
            timeline_lines.append(
                f"1️⃣ 开仓 ({open_dt})\n"
                f"入场价: {entry_price} USDT\n"
                f"张数: {contracts} 张\n"
                f"保证金: {margin_usdt} USDT\n"
                f"杠杆: {leverage}x\n"
                f"止损: {stop_loss} USDT\n"
                f"止盈: {take_profit} USDT\n"
                f"风险回报比: {rr}"
            )
            if signal_type or alignment_str or volume_note or trend_strength:
                cond_lines = ["开仓条件:"]
                if alignment_str and tf_vals:
                    cond_lines.append(f"  {alignment_str}")
                if signal_type:
                    cond_lines.append(f"  {signal_type} 信号")
                if volume_note:
                    cond_lines.append(f"  {volume_note}")
                if trend_strength:
                    cond_lines.append(f"  趋势强度评分：{trend_strength}/10")
                if confidence:
                    cond_lines.append(f"  置信度：{confidence}")
                if adx_note:
                    cond_lines.append(f"  {adx_note}")
                timeline_lines[0] += "\n" + "\n".join(cond_lines)
            step = 2

        for ev in timeline_events:
            if "部分止盈" in ev["event"]:
                timeline_lines.append(
                    f"{step}️⃣ 部分止盈 ({_fmt_ts(ev['ts'])})\n"
                    f"浮盈: {ev['pnl_pct']}\n"
                    f"剩余: {ev['contracts']} 张\n"
                    f"原因: 浮盈触发部分止盈策略"
                )
                step += 1

        # 平仓事件
        close_dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        timeline_lines.append(
            f"{step}️⃣ 平仓 ({close_dt})\n"
            f"盈亏: {pnl_sign}{final_pnl:.2f} USDT ({pnl_sign}{final_pnl_pct:.1f}%)\n"
            f"原因: {reason}"
        )

        timeline_text = "\n\n".join(timeline_lines)

        report = (
            f"{'='*40}\n"
            f"{symbol} 合约交易报告\n"
            f"{'='*40}\n"
            f"📊 交易概览\n"
            f"合约：{symbol}\n"
            f"方向：{side_label}\n"
            f"状态：{status_icon}（{reason}）\n"
            f"净盈亏：{pnl_sign}{final_pnl:.2f} USDT ({pnl_sign}{final_pnl_pct:.1f}%)\n"
            f"\n"
            f"⏰ 时间线\n"
            f"{timeline_text}\n"
        )

        if warning_text:
            report += f"\n⚠️ AI 警告\n{warning_text}\n"

        # ── 5. 存储报告 ──
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"trade_report_{symbol_safe}_{today}.md"
        # 同一天同品种可能有多笔，追加写入
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(report + "\n")
        logger.info(f"交易报告已保存：{report_path}")

        # ── 6. 发送飞书通知 ──
        send_notification(report)
        logger.info(f"交易报告已发送飞书：{symbol}")

    except Exception as e:
        logger.error(f"生成交易报告失败：{e}")


def _find_latest_log(
    directory: Path,
    symbol_safe: str,
    prefix: str = "",
    exclude_prefix: str = "",
    date: str = "",
) -> Path | None:
    """在 directory 中找匹配 symbol_safe 的最新文件"""
    pattern = f"{prefix}{symbol_safe}_*.json"
    candidates = list(directory.glob(pattern))
    if exclude_prefix:
        candidates = [f for f in candidates if not f.name.startswith(exclude_prefix)]
    if date:
        candidates = [f for f in candidates if date in f.name]
    if not candidates:
        # 放宽：不限日期
        candidates = list(directory.glob(pattern))
        if exclude_prefix:
            candidates = [f for f in candidates if not f.name.startswith(exclude_prefix)]
    return max(candidates, key=lambda f: f.name) if candidates else None


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fmt_ts(ts: str) -> str:
    """将 ISO 或 %Y%m%d_%H%M%S 格式时间戳转为可读字符串"""
    try:
        if "T" in ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts
