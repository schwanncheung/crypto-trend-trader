#!/usr/bin/env python3
"""
trade_report.py
合约平仓后汇总交易日志，生成飞书通知报告并存储至 logs/reports/
"""

import json
import logging
import sys
from datetime import datetime, timezone
from config_loader import now_cst, now_cst_str
from pathlib import Path
from typing import Optional

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
        today = now_cst_str("%Y%m%d")

        # ── 1. 读取开仓记录（优先 trades 目录，fallback 到 decisions）──
        open_log = _find_latest_log(TRADES_DIR, symbol_safe, prefix="", exclude_prefix="position_", date=today)
        open_data = _load_json(open_log) if open_log else {}

        # fallback：从 decisions 日志补全开仓信息
        decision_log = _find_latest_log(DECISIONS_DIR, symbol_safe, prefix="", date=today)
        decision_raw = _load_json(decision_log) if decision_log else {}
        decision_data = decision_raw.get("decision", decision_raw)

        entry_price  = open_data.get("entry_price") or decision_data.get("entry_price", "N/A")
        leverage     = open_data.get("leverage", 10)
        stop_loss    = open_data.get("stop_loss") or decision_data.get("stop_loss", "N/A")
        take_profit  = open_data.get("take_profit") or decision_data.get("take_profit", "N/A")
        signal       = open_data.get("signal") or decision_data.get("signal", "N/A")
        open_ts      = open_data.get("timestamp") or decision_raw.get("timestamp", "N/A")
        rr_raw       = open_data.get("risk_reward") or decision_data.get("risk_reward", "")

        # contracts / margin_usdt: 优先开仓记录，兜底从最早持仓快照读取
        contracts   = open_data.get("contracts")
        margin_usdt = open_data.get("margin_usdt")
        if contracts is None:
            _first_pos_logs = sorted(
                [f for f in TRADES_DIR.glob(f"position_{symbol_safe}_*.json")],
                key=lambda f: f.name
            )
            if _first_pos_logs:
                _first = _load_json(_first_pos_logs[0])
                contracts = _first.get("contracts")
        if contracts is None:
            contracts = "N/A"
        if margin_usdt is None:
            margin_usdt = "N/A"

        # 风险回报比
        if rr_raw:
            rr = rr_raw
        elif entry_price != "N/A" and stop_loss != "N/A" and take_profit != "N/A":
            sl_dist = abs(float(entry_price) - float(stop_loss))
            tp_dist = abs(float(take_profit) - float(entry_price))
            rr = f"1:{tp_dist / sl_dist:.1f}" if sl_dist > 0 else "N/A"
        else:
            rr = "N/A"

        # ── 2. 读取持仓快照，仅保留开仓时间之后 contracts 减少的事件（部分止盈）──
        position_logs = sorted(
            [f for f in TRADES_DIR.glob(f"position_{symbol_safe}_*.json")],
            key=lambda f: f.name
        )
        # 用开仓时间戳作为过滤基准（格式 %Y%m%d_%H%M%S）
        open_ts_safe = ""
        if open_ts != "N/A":
            try:
                if "T" in open_ts:
                    open_ts_safe = datetime.fromisoformat(
                        open_ts.replace("Z", "+00:00")
                    ).strftime("%Y%m%d_%H%M%S")
                else:
                    open_ts_safe = open_ts
            except Exception:
                open_ts_safe = ""

        partial_profit_events = []
        prev_contracts = None
        for plog in position_logs:
            # 跳过开仓时间之前的快照
            if open_ts_safe and plog.stem.split(f"{symbol_safe}_", 1)[-1] <= open_ts_safe:
                continue
            d = _load_json(plog)
            if not d:
                continue
            ct = float(d.get("contracts", 0))
            if prev_contracts is not None and ct < prev_contracts:
                partial_profit_events.append({
                    "ts": d.get("timestamp", ""),
                    "contracts": ct,
                    "prev_contracts": prev_contracts,
                    "pnl_pct": d.get("pnl_pct", ""),
                })
            prev_contracts = ct

        # ── 3. 从已读取的 decision_data 提取分析字段 ──
        signal_type    = decision_data.get("signal_type", "")
        volume_note    = decision_data.get("volume_note", "")
        trend_strength = decision_data.get("trend_strength", "")
        confidence     = decision_data.get("confidence", "")
        tf_alignment   = decision_data.get("timeframe_alignment", {})
        reason_text    = decision_data.get("reason", "")
        warning_text   = decision_data.get("warning", "")
        adx_note       = ""
        if reason_text:
            import re
            m = re.search(r"ADX[=\s)?]?([\d.]+)", reason_text)
            if m:
                adx_note = f"ADX={m.group(1)} 强势趋势"

        # ── 4. 组装报告文本 ──
        side_label = "LONG（做多）" if signal == "long" else "SHORT（做空）" if signal == "short" else signal
        pnl_sign   = "+" if final_pnl >= 0 else ""
        status_icon = "✅ 盈利" if final_pnl >= 0 else "❌ 亏损/止损"

        tf_str  = "/".join(tf_alignment.keys()) if tf_alignment else "多周期"
        tf_vals_list = list(set(tf_alignment.values())) if tf_alignment else []
        tf_val  = tf_vals_list[0] if len(tf_vals_list) == 1 else "/".join(tf_alignment.values())
        tf_vals = tf_val
        alignment_str = f"多周期共振 ({tf_str} 均为 {tf_val})" if tf_vals_list else ""

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

        for ev in partial_profit_events:
            timeline_lines.append(
                f"{step}️⃣ 部分止盈 ({_fmt_ts(ev['ts'])})\n"
                f"浮盈: {ev['pnl_pct']}\n"
                f"平仓: {int(ev['prev_contracts'] - ev['contracts'])} 张\n"
                f"剩余: {int(ev['contracts'])} 张\n"
                f"原因: 浮盈触发部分止盈策略"
            )
            step += 1

        # 平仓事件
        close_dt = now_cst_str("%Y-%m-%d %H:%M:%S")
        timeline_lines.append(
            f"{step}️⃣ 平仓 ({close_dt})\n"
            f"盈亏: {pnl_sign}{final_pnl:.2f} USDT ({pnl_sign}{final_pnl_pct:.1f}%)\n"
            f"原因: {reason}"
        )

        timeline_text = "\n\n".join(timeline_lines)

        report = (
            # f"{'='*40}\n"
            f"{symbol} 合约交易报告\n"
            # f"{'='*40}\n"
            f"📊 交易概览\n"
            f"合约：{symbol}\n"
            f"方向：{side_label}\n"
            f"状态：{status_icon}（{reason}）\n"
            f"净盈亏：{pnl_sign}{final_pnl:.2f} USDT ({pnl_sign}{final_pnl_pct:.1f}%)\n"
            f"\n"
            f"⏰ 时间线\n"
            f"{timeline_text}\n"
        )

        if reason_text:
            report += f"\n🤖 AI 分析\n{reason_text}\n"

        if warning_text:
            report += f"\n⚠️ AI 警告\n{warning_text}\n"

        # ── 5. 存储报告（每次平仓生成独立文件，避免多次追加混淆）──
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        close_ts = now_cst_str()
        report_path = REPORTS_DIR / f"trade_report_{symbol_safe}_{close_ts}.md"
        with open(report_path, "w", encoding="utf-8") as f:
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
) -> Optional[Path]:
    """在 directory 中找匹配 symbol_safe 的最新文件"""
    pattern = f"{prefix}{symbol_safe}_*.json"
    candidates = list(directory.glob(pattern))
    if exclude_prefix:
        candidates = [f for f in candidates if not f.name.startswith(exclude_prefix)]
    if date:
        candidates = [f for f in candidates if date in f.name]
    if not candidates:
        # 放宽：不限日期，但仍严格匹配 symbol_safe 前缀
        candidates = list(directory.glob(pattern))
        if exclude_prefix:
            candidates = [f for f in candidates if not f.name.startswith(exclude_prefix)]
        # 二次校验：确保文件名确实以 symbol_safe 开头，防止跨品种串数据
        candidates = [f for f in candidates if f.name.startswith(symbol_safe + "_")]
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
