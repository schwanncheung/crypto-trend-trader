"""
ai_analysis.py
AI 分析模块：规则引擎预过滤 + 文本 LLM 分析
"""

import re
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from config_loader import (
    check_env,
    ANALYSIS_CFG as _ANALYSIS_CFG,
    DASHSCOPE_API_KEY,
    TRADING_CFG,
    TIMEFRAMES,
    setup_logging,
)
check_env()
setup_logging("ai_analysis")
logger = logging.getLogger(__name__)

# 模块级配置副本（可被 reload_config_from_dict 更新）
_TRADING_CFG = TRADING_CFG.copy() if TRADING_CFG else {}

_MIN_SIGNAL_STRENGTH = _TRADING_CFG.get("min_signal_strength", 7)
_MIN_RR_RATIO        = _TRADING_CFG.get("min_rr_ratio", 2.0)
_PATTERN_POSITION_BOOST = _TRADING_CFG.get("pattern_position_boost", {})  # 形态仓位倍数配置


def reload_config_from_dict(config: dict) -> None:
    """
    从外部配置字典重新加载参数（回测系统 override 机制）。
    """
    global _MIN_SIGNAL_STRENGTH, _MIN_RR_RATIO, _TRADING_CFG, _PATTERN_POSITION_BOOST

    trading_cfg = config.get("trading", {})

    # 更新模块级配置字典
    _TRADING_CFG.update(trading_cfg)

    # 更新全局变量
    _MIN_SIGNAL_STRENGTH = _TRADING_CFG.get("min_signal_strength", _MIN_SIGNAL_STRENGTH)
    _MIN_RR_RATIO = _TRADING_CFG.get("min_rr_ratio", _MIN_RR_RATIO)
    _PATTERN_POSITION_BOOST = _TRADING_CFG.get("pattern_position_boost", _PATTERN_POSITION_BOOST)

    logger.info(
        f"[ai_analysis] 配置已重新加载："
        f"min_signal_strength={_MIN_SIGNAL_STRENGTH}, min_rr_ratio={_MIN_RR_RATIO}, "
        f"pattern_position_boost={_PATTERN_POSITION_BOOST}"
    )


def parse_ai_response(text: str) -> dict:
    """解析AI返回，兼容纯JSON和markdown包裹格式"""
    try:
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if match:
            return json.loads(match.group(1))
        return json.loads(text.strip())
    except Exception:
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except Exception:
            logger.error(f"JSON解析失败，原始内容：{text[:200]}")
            return _default_wait_response("AI返回内容解析失败")


def _default_wait_response(reason: str) -> dict:
    logger.info(f"[rule_only] 返回 wait: {reason}")
    return {
        "signal": "wait",
        "confidence": "low",
        "reason": reason,
        "warning": "AI分析异常，请人工确认"
    }


def _build_rule_only_decision(tf_indicators: dict, direction: str, symbol: str) -> dict:
    """
    纯规则模式：从 tf_indicators 量化指标构造与 risk_filter 兼容的 decision dict。
    """
    from config_loader import TIMEFRAMES as _TFS
    rule_filter_cfg = _ANALYSIS_CFG.get("rule_filter", {})

    min_signal_strength  = _MIN_SIGNAL_STRENGTH  # 使用全局变量（可被 reload 更新）
    min_rr_ratio         = _MIN_RR_RATIO         # 使用全局变量（可被 reload 更新）
    vol_ratio_threshold  = rule_filter_cfg.get("volume_ratio_threshold", 0.8)
    strong_trend_adx     = rule_filter_cfg.get("strong_trend_adx_threshold", 60)
    strong_trend_di_diff = rule_filter_cfg.get("strong_trend_di_diff_threshold", 20)
    timeframes           = _TFS or ["1h", "30m", "15m"]
    anchor_tf            = timeframes[0]
    base_tf              = timeframes[-1]

    anchor_ind = tf_indicators.get(anchor_tf, {})
    adx_info   = anchor_ind.get("adx", {})
    adx        = adx_info.get("adx", 0) if isinstance(adx_info, dict) else float(adx_info or 0)
    plus_di    = adx_info.get("plus_di", 0) if isinstance(adx_info, dict) else 0
    minus_di   = adx_info.get("minus_di", 0) if isinstance(adx_info, dict) else 0
    di_diff    = abs(plus_di - minus_di)
    strong_trend_exemption = (adx >= strong_trend_adx and di_diff >= strong_trend_di_diff)

    score = 0.0
    total_tfs = len(timeframes)

    expected = "up" if direction == "long" else "down"
    ema_align_ok = sum(
        1 for tf in timeframes
        if tf_indicators.get(tf, {}).get("ema", {}).get("alignment") == ("bullish" if direction == "long" else "bearish")
    )

    # 跨周期趋势一致性检测（新增）
    # 当非锚周期有强趋势且方向一致时，即使锚周期 EMA 对齐失败，也给予部分加分
    non_anchor_tfs = [tf for tf in timeframes if tf != anchor_tf]
    strong_trend_count = 0
    cross_tf_bonus = 0.0

    for tf in non_anchor_tfs:
        tf_ind = tf_indicators.get(tf, {})
        tf_adx = tf_ind.get("adx", {}).get("adx", 0) if isinstance(tf_ind.get("adx"), dict) else 0
        tf_trend = tf_ind.get("trend", "sideways")
        # 降低阈值：ADX >= 35 且趋势一致
        if tf_adx >= strong_trend_adx * 0.58 and tf_trend == expected:  # ADX >= 35
            strong_trend_count += 1

    if strong_trend_count >= len(non_anchor_tfs) * 0.5:  # 至少一半非锚周期有强趋势
        cross_tf_bonus = 2.0  # 跨周期趋势一致性加分
        logger.info(f"[rule_only] {symbol} 跨周期趋势一致性加分+2（{strong_trend_count}/{len(non_anchor_tfs)}强趋势）")

    score += ema_align_ok * (6.0 / total_tfs) + cross_tf_bonus

    # 量能评分（对齐 AI prompt 逻辑）
    vol_ratio = tf_indicators.get(base_tf, {}).get("volume_ratio", 0)
    volume_confirmed = any(
        tf_indicators.get(tf, {}).get("volume_ratio", 0) >= vol_ratio_threshold
        for tf in timeframes
        if tf_indicators.get(tf, {}).get("valid")
    )
    if vol_ratio >= vol_ratio_threshold * 2:
        score += 2.0
    elif vol_ratio >= vol_ratio_threshold:
        score += 1.0
    elif vol_ratio < vol_ratio_threshold:
        score -= 2.0  # 量能不足减分
        if vol_ratio < 0.1:
            score -= 1.0  # 极端缩量额外减分

    if strong_trend_exemption:
        volume_confirmed = True

    # 量价同向验证（优化3）：量价背离时降低信号强度
    from indicator_engine import detect_volume_price_alignment
    base_df_key = base_tf
    # 通过 price_series 和 volume 数据间接判断（tf_indicators 中无原始 df）
    # 使用锚周期的 momentum_acceleration 作为量价同向的代理指标
    anchor_mom_accel = anchor_ind.get("momentum_acceleration", {})
    vol_price_note = ""
    if anchor_mom_accel.get("decelerating", False):
        score -= 1.0
        vol_price_note = f"动量衰减（实体比={anchor_mom_accel.get('ratio', 1.0):.2f}）"
        logger.info(f"[rule_only] {symbol} 动量衰减，信号强度-1")
    elif anchor_mom_accel.get("accelerating", False):
        score += 1.0
        vol_price_note = f"动量加速（实体比={anchor_mom_accel.get('ratio', 1.0):.2f}）"
        logger.info(f"[rule_only] {symbol} 动量加速，信号强度+1")

    # K 线形态检测（检查所有周期，优先使用有形态的周期）
    pattern = "none"
    pattern_tf = base_tf
    pattern_boost = 1.0  # 仓位倍数（高胜率形态时增加）
    for tf in timeframes:
        patterns_list = tf_indicators.get(tf, {}).get("patterns", [])
        if patterns_list and patterns_list[0].get("pattern") not in ("none", "", None):
            pattern = patterns_list[0]["pattern"]
            pattern_tf = tf
            break

    if pattern not in ("none", "", None):
        score += 1.5
        # 高胜率形态额外加分 + 仓位 boost（从配置读取）
        boost_ratio = _PATTERN_POSITION_BOOST.get(pattern, 1.0)
        if boost_ratio > 1.0:
            score += 1.0  # 高胜率形态额外加1分
            pattern_boost = boost_ratio
            logger.info(f"[rule_only] {symbol} {pattern} 形态：信号强度+1，仓位+{(boost_ratio-1)*100:.0f}%")

    # ADX 边缘区减分（对齐 AI prompt）
    adx_edge_min = _TRADING_CFG.get("adx_edge_min", 20)
    adx_edge_max = _TRADING_CFG.get("adx_edge_max", 25)
    if adx_edge_min <= adx < adx_edge_max:
        score -= 1.0

    # RSI 评分（对齐 AI prompt 的精准区间）
    # 优先使用有形态周期的 RSI，若无形态则使用 base_tf
    rsi_tf = pattern_tf if pattern != "none" else base_tf
    rsi = tf_indicators.get(rsi_tf, {}).get("rsi", 50)
    long_rsi_low = rule_filter_cfg.get("long_signal_rsi_low", 40)
    rsi_oversold = rule_filter_cfg.get("rsi_oversold", 20)
    rsi_overbought = rule_filter_cfg.get("rsi_overbought", 80)

    if direction == "long":
        if rsi_oversold <= rsi <= long_rsi_low:
            score += 1.5  # 回调充分但未超卖
    else:  # short
        if long_rsi_low <= rsi <= (rsi_overbought - 10):
            score += 1.5  # 反弹充分但未超买
        # 新增：极端超卖后的整理形态（下跌中继）
        elif rsi < rsi_oversold:
            # 检查是否有 inside_bar 或横盘整理形态
            if pattern in ["inside_bar", "doji", "hammer"] or strong_trend_count > 0:
                score += 1.5  # 超卖后整理，典型下跌中继
                logger.info(f"[rule_only] {symbol} RSI极端超卖({rsi:.1f})后整理形态，加分+1.5")

    # 强趋势豁免（保留原逻辑）
    if strong_trend_exemption:
        if direction == "short" and rsi < rsi_oversold:
            score += 1.5
        elif direction == "long" and rsi > rsi_overbought:
            score += 1.5

    # RSI 趋势反向检测（新增）
    rsi_series = tf_indicators.get(base_tf, {}).get("rsi_series", [])
    if len(rsi_series) >= 3:
        # 检测最近 3 根 RSI 的变化趋势
        rsi_trend = "up" if rsi_series[-1] > rsi_series[-2] > rsi_series[-3] else \
                    "down" if rsi_series[-1] < rsi_series[-2] < rsi_series[-3] else "mixed"
        if (direction == "long" and rsi_trend == "down") or \
           (direction == "short" and rsi_trend == "up"):
            score -= 2.0  # RSI 趋势与信号方向相反

    # 近期动能占比（新增）
    momentum = tf_indicators.get(base_tf, {}).get("momentum", {})
    if momentum:
        bull_pct = momentum.get("bull_pct", 0)
        bear_pct = momentum.get("bear_pct", 0)
        momentum_threshold = rule_filter_cfg.get("recent_trend_min_pct", 0.65)
        if (direction == "long" and bull_pct >= momentum_threshold) or \
           (direction == "short" and bear_pct >= momentum_threshold):
            score += 1.0

    signal_strength = min(10, int(score))

    # 使用有形态周期的价格/ATR，若无形态则使用 base_tf
    signal_ind = tf_indicators.get(pattern_tf, {}) if pattern != "none" else tf_indicators.get(base_tf, {})
    entry = signal_ind.get("current_price", 0)
    if entry <= 0:
        logger.warning(f"[rule_only] {symbol} 无法获取当前价格，返回wait")
        return _default_wait_response("rule_only模式无法获取当前价格")

    atr = signal_ind.get("atr", entry * 0.01)

    # 使用动态止损（根据 ADX 自动调整）
    from dynamic_stop_take_profit import calculate_dynamic_stop_loss, calculate_take_profit
    stop_loss, multiplier_used = calculate_dynamic_stop_loss(
        entry_price=entry,
        atr=atr,
        signal=direction,
        adx=adx
    )

    # 止损距离异常时拒绝信号
    if stop_loss is None:
        return {
            "signal": "wait",
            "confidence": "low",
            "reason": f"止损距离异常（波动过大），跳过信号",
        }

    # 计算关键支撑/阻力位（从指标数据中取，无则置 None）
    # rule_only 模式下没有 LLM 标注，用 price_series 的近期高低点近似
    signal_price_series = signal_ind.get("price_series", [])
    if signal_price_series and len(signal_price_series) >= 2:
        key_support    = min(signal_price_series) if direction == "short" else None
        key_resistance = max(signal_price_series) if direction == "long" else None
    else:
        key_support    = None
        key_resistance = None

    take_profit, tp_reason = calculate_take_profit(
        entry, stop_loss, direction,
        key_support=key_support,
        key_resistance=key_resistance,
        adx=adx
    )
    risk   = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    if rr < min_rr_ratio:
        return _default_wait_response(f"RR不足：{rr:.2f} < {min_rr_ratio}")

    confidence    = "high" if (signal_strength >= min_signal_strength and volume_confirmed) else "low"
    trend_strength = min(10, int(adx / 4))

    # 构建详细的评分说明
    momentum_info = momentum.get("direction", "mixed") if momentum else "mixed"
    bull_pct_val = momentum.get("bull_pct", 0) if momentum else 0
    bear_pct_val = momentum.get("bear_pct", 0) if momentum else 0

    return {
        "signal":           direction,
        "signal_type":      pattern or "pullback",
        "signal_strength":  signal_strength,
        "trend":            expected,
        "trend_phase":      "mid",
        "trend_strength":   trend_strength,
        "volume_confirmed": volume_confirmed,
        "volume_note":      f"量比={vol_ratio:.2f}" + (f"，{vol_price_note}" if vol_price_note else ""),
        "key_support":      key_support,
        "key_resistance":   key_resistance,
        "entry_price":      entry,
        "stop_loss":        stop_loss,
        "take_profit":      take_profit,
        "risk_reward":      f"1:{rr:.1f}",
        "divergence_risk":  False,
        "structure_broken": False,
        "confidence":       confidence,
        "entry_rsi":        rsi,  # 新增：入场时 RSI 值
        "pattern_boost":    pattern_boost,  # 新增：形态仓位倍数（hammer=1.1）
        "reason":           (
            f"规则引擎信号：{direction}，信号强度={signal_strength}/10，"
            f"ADX={adx:.1f}，EMA对齐={ema_align_ok}/{total_tfs}，"
            f"量比={vol_ratio:.2f}，形态={pattern}({pattern_tf})，RSI={rsi:.1f}({rsi_tf})，"
            f"动能={momentum_info}(多{bull_pct_val:.0%}/空{bear_pct_val:.0%})"
        ),
        "warning":          None,
    }


def passes_risk_filter(decision: dict) -> bool:
    """风控过滤：只有通过所有检查才允许交易"""
    checks = {
        "信号方向明确":
            decision.get("signal") in ["long", "short"],
        "置信度为high":
            decision.get("confidence") == "high",
        f"信号强度>={_MIN_SIGNAL_STRENGTH}":
            decision.get("signal_strength", 0) >= _MIN_SIGNAL_STRENGTH,
        "成交量确认":
            decision.get("volume_confirmed", False) is True,
        "无背离风险":
            decision.get("divergence_risk", True) is False,
        "结构未打破":
            decision.get("structure_broken", True) is False,
        f"风险回报比>={_MIN_RR_RATIO}":
            _parse_rr(decision.get("risk_reward", "1:0")) >= _MIN_RR_RATIO,
    }

    failed = [k for k, v in checks.items() if not v]
    if failed:
        logger.info(f"风控过滤未通过：{failed}")
        return False

    logger.info("风控过滤通过，允许交易")
    return True


def _parse_rr(rr_str: str) -> float:
    """解析风险回报比字符串，如 '1:2.5' -> 2.5"""
    try:
        parts = rr_str.split(":")
        return float(parts[1]) / float(parts[0])
    except Exception:
        return 0.0


def save_decision_log(
    symbol: str,
    timeframe: str,
    decision: dict,
    image_paths: list = None,
    log_dir: str = "logs/decisions"
) -> str:
    """保存AI决策日志为JSON文件"""
    from config_loader import now_cst_str
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    ts = now_cst_str()
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    log_path = f"{log_dir}/{safe_symbol}_{timeframe}_{ts}.json"

    log_data = {
        "timestamp": ts,
        "symbol": symbol,
        "timeframe": timeframe,
        "decision": decision
    }

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)

    logger.info(f"决策日志已保存：{log_path}")
    return log_path


def _build_text_analysis_prompt() -> str:
    """动态生成文本LLM分析 Prompt，时间框架从 TIMEFRAMES 配置读取。"""
    total = len(TIMEFRAMES)
    anchor_tf = TIMEFRAMES[0]
    base_tf = TIMEFRAMES[-1]
    tf_alignment_fields = "\n".join(
        f'    "{tf}": "up或down或sideways",' for tf in TIMEFRAMES
    )
    min_alignment = total - 1
    atr_mult = _TRADING_CFG.get("stop_loss_atr_multiplier", 2.5)
    rr_ratio = _MIN_RR_RATIO

    rule_cfg = _ANALYSIS_CFG.get("rule_filter", {})
    vol_ratio_thresh = rule_cfg.get("volume_ratio_threshold", 0.8)
    adx_edge_min = _TRADING_CFG.get("adx_edge_min", 20)
    adx_edge_max = _TRADING_CFG.get("adx_edge_max", 25)
    long_rsi_low = rule_cfg.get("long_signal_rsi_low", 40)
    rsi_oversold = rule_cfg.get("rsi_oversold", 20)
    rsi_overbought = rule_cfg.get("rsi_overbought", 80)
    recent_momentum_pct = rule_cfg.get("recent_trend_min_pct", 0.65)

    vol_burst_thresh = vol_ratio_thresh * 2
    momentum_strong_pct = int(recent_momentum_pct * 100)

    # 动态生成形态加分说明（从配置读取，仅说明信号强度加分）
    pattern_boost_lines = []
    for pattern, boost in _PATTERN_POSITION_BOOST.items():
        if boost > 1.0:
            pattern_boost_lines.append(f"  - {pattern}：额外+1分")
    pattern_boost_note = "\n".join(pattern_boost_lines) if pattern_boost_lines else "  - （未配置高胜率形态）"

    return f"""你是一位激进型裸K趋势交易员，专注加密货币合约单边行情，追求高胜率的趋势早期入场。

系统规则引擎已完成硬过滤（EMA排列、ADX门槛、RSI保护、背离检测），以下是通过预过滤的市场快照：

{{market_snapshot}}

你的任务：基于快照做"软判断"，评估入场时机的成熟度

## 一、多周期共振分析（自上而下）

1. **{anchor_tf} 锚周期**：定宏观方向，ADX>={adx_edge_max} 为强趋势，{adx_edge_min}-{adx_edge_max} 为边缘区需谨慎
2. **中间周期**：确认结构完整性（HH+HL 或 LH+LL 是否清晰）
3. **{base_tf} 入场周期**：寻找回调结束信号（K线形态 + 量能 + RSI位置）

**对齐分数 < {min_alignment} 时必须返回 wait**（周期冲突 = 假信号）

## 二、signal_strength 评分标准（1-10分，激进型视角）

**基础分（6分）：**
- 多周期 EMA 排列一致：+2分/周期（{total}周期全对齐=6分）

**加分项（最多+4分）：**
- 量能爆发（量比 >= {vol_burst_thresh:.1f}）：+2分
- 明确K线形态（吞没/锤子/Pin Bar）：+1.5分
- **高胜率形态额外加分**：
{pattern_boost_note}
- RSI 位置理想：
  - 做多：RSI {rsi_oversold}-{long_rsi_low}（回调充分但未超卖）：+1.5分
  - 做空：RSI {long_rsi_low}-{rsi_overbought - 10}（反弹充分但未超买）：+1.5分
- 近期动能强劲（空头/多头占比 >= {momentum_strong_pct}%）：+1分
- 动量加速（快照中显示"动量加速"，实体放大 >= 1.5x）：+1分

**减分项：**
- RSI 趋势与信号方向相反（如做空但 RSI 连升2轮）：-2分
- ADX 处于边缘区（{adx_edge_min}-{adx_edge_max}）：-1分
- 量能不足（量比 < {vol_ratio_thresh}）：-2分
- {base_tf} 入场周期极端缩量（量比 < 0.1）：额外-1分（流动性陷阱风险）
- 动量衰减（快照中显示"动量衰减"，实体缩小 < 0.8x）：-1分

## 三、入场价格选择（激进型策略）

1. **做多入场**：首选最近支撑位附近，次选当前价（强趋势追单）
2. **做空入场**：首选最近阻力位附近，次选当前价（强趋势追单）

当前价距结构位 < 0.5% → 使用结构位；> 1% → 使用当前价

## 四、止损止盈计算（严格执行）

- 做多：`stop_loss = entry - {atr_mult} × ATR`，`take_profit = entry + {rr_ratio} × (entry - stop_loss)`
- 做空：`stop_loss = entry + {atr_mult} × ATR`，`take_profit = entry - {rr_ratio} × (stop_loss - entry)`
- 盈亏比必须 >= {rr_ratio}:1

## 五、confidence 判定（二元规则）

**high（同时满足3个条件）：**
1. `signal_strength >= {_MIN_SIGNAL_STRENGTH}`
2. `volume_confirmed = true`（任一周期量比 >= {vol_ratio_thresh}）
3. `alignment_score >= {min_alignment}`

**low（任一条件不满足）**

## 六、输出格式（严格 JSON，禁止任何额外文本）

{{{{
  "timeframe_alignment": {{{{
{tf_alignment_fields}
  }}}},
  "alignment_score": 整数0到{total},
  "trend": "up或down或sideways",
  "trend_phase": "early或mid或late",
  "trend_strength": 整数1到10,
  "signal": "long或short或wait",
  "signal_type": "engulfing或hammer或inside_bar或morning_star或pullback或momentum或none",
  "signal_strength": 整数1到10,
  "volume_confirmed": true或false,
  "volume_note": "简短说明（如：量比1.8放量、量比0.6缩量）",
  "key_support": 数字,
  "key_resistance": 数字,
  "entry_price": 数字,
  "stop_loss": 数字,
  "take_profit": 数字,
  "risk_reward": "1:X.X",
  "divergence_risk": true或false,
  "structure_broken": false,
  "confidence": "high或low",
  "reason": "80-150字分析，必须包含：①多周期对齐情况 ②RSI动能方向 ③近期动能占比 ④入场时机成熟度 ⑤为什么给这个signal_strength评分",
  "warning": "风险提示或null"
}}}}
"""


TEXT_ANALYSIS_PROMPT = _build_text_analysis_prompt()


def _get_text_llm_cfg() -> dict:
    from config_loader import ANALYSIS_CFG
    return ANALYSIS_CFG.get("text_llm", {})


def analyze_with_text_llm(market_snapshot: str) -> dict:
    """
    使用文本 LLM 分析市场快照。
    调用链：primary_model → fallback_model
    集成熔断器：连续失败 3 次后自动降级到 rule_only 模式
    """
    from openai import OpenAI
    import os
    from circuit_breaker import get_llm_circuit_breaker

    text_cfg = _get_text_llm_cfg()
    primary_model  = text_cfg.get("primary_model",  "qwen3.5-plus")
    fallback_model = text_cfg.get("fallback_model", "qwen-max")
    max_tokens     = text_cfg.get("max_tokens", 2000)
    timeout        = text_cfg.get("timeout", 30)
    base_url       = text_cfg.get("base_url",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1")

    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    client  = OpenAI(api_key=api_key, base_url=base_url)
    prompt  = TEXT_ANALYSIS_PROMPT.format(market_snapshot=market_snapshot)
    messages = [{"role": "user", "content": prompt}]

    def _call_llm_with_fallback():
        last_error = None
        for model_name in [primary_model, fallback_model]:
            try:
                logger.info(f"[文本 LLM] 调用 {model_name} 分析市场快照...")
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                text = resp.choices[0].message.content or ""
                result = parse_ai_response(text)
                result["_model_used"] = model_name
                result["_analysis_mode"] = "text_llm"
                logger.info(f"[文本 LLM] {model_name} 返回：signal={result.get('signal')}, "
                            f"confidence={result.get('confidence')}, "
                            f"strength={result.get('signal_strength')}")
                return result
            except Exception as e:
                last_error = e
                logger.warning(f"[文本 LLM] {model_name} 调用失败：{e}")
        raise RuntimeError(f"所有文本模型均失败：{last_error}")

    cb = get_llm_circuit_breaker()
    try:
        return cb.call(_call_llm_with_fallback)
    except Exception as e:
        logger.error(f"[文本 LLM] 熔断器调用失败：{e}")
        return _default_wait_response(str(e))




def analyze_symbol(
    multi_tf_data: dict,
    symbol: str,
    support_levels: list = None,
    resistance_levels: list = None,
) -> dict:
    """
    统一分析入口。根据 settings.yaml analysis.mode 自动路由：
      - mode="text"      : 规则引擎预过滤 → 文本LLM分析
      - mode="rule_only" : 纯规则引擎，不调用LLM
    """
    from indicator_engine import generate_market_snapshot, rule_engine_filter

    mode = _ANALYSIS_CFG.get("mode", "text")
    logger.info(f"[分析入口] {symbol} 使用模式：{mode}")

    if mode == "rule_only":
        snapshot, tf_indicators = generate_market_snapshot(
            multi_tf_data, symbol, support_levels, resistance_levels
        )
        passed, direction, filter_reason = rule_engine_filter(tf_indicators, symbol)
        if not passed:
            logger.info(f"[分析入口] {symbol} 规则引擎未通过 | 原因：{filter_reason}")
            resp = _default_wait_response(filter_reason)
            resp["_analysis_mode"] = "rule_filter_rejected"
            resp["_filter_reason"] = filter_reason
            return resp

        logger.info(f"[分析入口] {symbol} 规则引擎通过（{direction}），构造规则决策")
        result = _build_rule_only_decision(tf_indicators, direction, symbol)
        result["_analysis_mode"] = "rule_only"
        result["_rule_direction"] = direction
        result["_filter_reason"] = filter_reason
        return result

    else:
        snapshot, tf_indicators = generate_market_snapshot(
            multi_tf_data, symbol, support_levels, resistance_levels
        )
        logger.info(f"[市场快照]\n{snapshot}")

        passed, direction, filter_reason = rule_engine_filter(tf_indicators, symbol)
        if not passed:
            logger.info(f"[分析入口] {symbol} 规则引擎未通过，跳过LLM | 原因：{filter_reason}")
            resp = _default_wait_response(filter_reason)
            resp["_analysis_mode"] = "rule_filter_rejected"
            resp["_filter_reason"] = filter_reason
            return resp

        logger.info(f"[分析入口] {symbol} 规则引擎通过（{direction}），调用文本LLM")
        result = analyze_with_text_llm(snapshot)
        result["_rule_direction"] = direction
        result["_filter_reason"]  = filter_reason
        from config_loader import TIMEFRAMES
        anchor_tf = TIMEFRAMES[0] if TIMEFRAMES else "1h"
        anchor_ind = tf_indicators.get(anchor_tf, {})
        anchor_adx = anchor_ind.get("adx", {}).get("adx", None)
        if anchor_adx is not None:
            result["_anchor_adx"] = anchor_adx

        # 统一应用动态止损止盈逻辑（覆盖 LLM 返回值）
        signal = result.get("signal")
        if signal in ["long", "short"]:
            entry = result.get("entry_price", 0)
            if entry > 0:
                from dynamic_stop_take_profit import calculate_dynamic_stop_loss, calculate_take_profit
                base_tf = "15m"  # 固定使用 15m ATR 计算止损，避免 5m 周期噪音导致止损过近
                base_ind = tf_indicators.get(base_tf, {})
                atr = base_ind.get("atr", entry * 0.01)
                adx = anchor_ind.get("adx", {}).get("adx", 0) if isinstance(anchor_ind.get("adx"), dict) else float(anchor_ind.get("adx", 0) or 0)

                # 添加入场 RSI（用于风控检查）
                entry_rsi = base_ind.get("rsi", None)
                if entry_rsi is not None:
                    result["entry_rsi"] = entry_rsi

                # 动态止损
                stop_loss, multiplier_used = calculate_dynamic_stop_loss(
                    entry_price=entry,
                    atr=atr,
                    signal=signal,
                    adx=adx
                )

                # 止损距离异常时拒绝信号
                if stop_loss is None:
                    result["signal"] = "wait"
                    result["reason"] = "止损距离异常（波动过大），跳过信号"
                    logger.info(f"[分析入口] {symbol} 止损异常，信号降级为 wait")
                    return result

                # 智能止盈（考虑关键位）
                key_support = result.get("key_support")
                key_resistance = result.get("key_resistance")
                take_profit, tp_reason = calculate_take_profit(
                    entry, stop_loss, signal,
                    key_support=key_support,
                    key_resistance=key_resistance,
                    adx=adx
                )

                # 覆盖 LLM 返回的止损止盈
                result["stop_loss"] = stop_loss
                result["take_profit"] = take_profit
                # 重新计算 RR 字段（关键修复：避免风控检查读取错误的 RR）
                risk = abs(entry - stop_loss)
                reward = abs(take_profit - entry)
                rr = round(reward / risk, 2) if risk > 0 else 0.0
                result["risk_reward"] = f"1:{rr:.1f}"
                result["_dynamic_stop_loss_applied"] = True
                result["_tp_reason"] = tp_reason
                logger.info(f"已应用动态止损止盈：SL={stop_loss:.6g}, TP={take_profit:.6g}, RR={rr:.2f}, {tp_reason}")

            # 根据形态设置仓位倍数（与 rule_only 模式保持一致）
            signal_type = result.get("signal_type", "none")
            pattern_boost = _PATTERN_POSITION_BOOST.get(signal_type, 1.0)
            result["pattern_boost"] = pattern_boost
            if pattern_boost > 1.0:
                # 高胜率形态额外加分
                current_strength = result.get("signal_strength", 0)
                result["signal_strength"] = min(10, current_strength + 1)
                logger.info(f"[分析入口] {symbol} {signal_type} 形态：信号强度+1（{current_strength}→{result['signal_strength']}），仓位+{(pattern_boost-1)*100:.0f}%")
            else:
                result["pattern_boost"] = 1.0

        return result


if __name__ == "__main__":
    import sys
    import json as _json

    if len(sys.argv) < 2:
        print("用法：python ai_analysis.py <symbol>")
        print("示例：python ai_analysis.py BTC/USDT:USDT")
        sys.exit(0)

    symbol = sys.argv[1]
    print(f"\n文本LLM分析：{symbol}")
    print("请通过 market_scanner.py 触发完整分析流程")
