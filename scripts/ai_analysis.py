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

_MIN_SIGNAL_STRENGTH = TRADING_CFG.get("min_signal_strength", 7)
_MIN_RR_RATIO        = TRADING_CFG.get("min_rr_ratio", 2.0)


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

    min_signal_strength  = TRADING_CFG.get("min_signal_strength", 7)
    min_rr_ratio         = TRADING_CFG.get("min_rr_ratio", 2.0)  # 风控门槛
    target_rr_ratio      = TRADING_CFG.get("target_rr_ratio", 1.2)  # 止盈设置
    atr_multiplier       = TRADING_CFG.get("stop_loss_atr_multiplier", 2.5)
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
    score += ema_align_ok * (6.0 / total_tfs)

    vol_ratio = tf_indicators.get(base_tf, {}).get("volume_ratio", 0)
    volume_confirmed = any(
        tf_indicators.get(tf, {}).get("volume_ratio", 0) >= vol_ratio_threshold
        for tf in timeframes
        if tf_indicators.get(tf, {}).get("valid")
    )
    if vol_ratio >= vol_ratio_threshold * 2:  score += 2.0
    elif vol_ratio >= vol_ratio_threshold:    score += 1.0
    if strong_trend_exemption:
        volume_confirmed = True

    patterns_list = tf_indicators.get(base_tf, {}).get("patterns", [])
    pattern = patterns_list[0]["pattern"] if patterns_list else "none"
    if pattern not in ("none", "", None):
        score += 1.5

    rsi = tf_indicators.get(base_tf, {}).get("rsi", 50)
    if 25 <= rsi <= 65:
        score += 1.5
    elif strong_trend_exemption and direction == "short" and rsi < 25:
        score += 1.5
    elif strong_trend_exemption and direction == "long" and rsi > 75:
        score += 1.5

    signal_strength = min(10, int(score))

    base_ind = tf_indicators.get(base_tf, {})
    entry = base_ind.get("current_price", 0)
    if entry <= 0:
        logger.warning(f"[rule_only] {symbol} 无法获取当前价格，返回wait")
        return _default_wait_response("rule_only模式无法获取当前价格")

    atr = base_ind.get("atr", entry * 0.01)

    # 使用动态止损（根据 ADX 自动调整）
    from dynamic_stop_take_profit import calculate_dynamic_stop_loss, calculate_take_profit
    stop_loss, multiplier_used = calculate_dynamic_stop_loss(
        entry_price=entry,
        atr=atr,
        signal=direction,
        adx=adx
    )

    # 计算关键支撑/阻力位（用于止盈限制）
    key_support = entry * 0.97 if direction == "short" else None
    key_resistance = entry * 1.03 if direction == "long" else None

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

    return {
        "signal":           direction,
        "signal_type":      pattern or "pullback",
        "signal_strength":  signal_strength,
        "trend":            expected,
        "trend_phase":      "mid",
        "trend_strength":   trend_strength,
        "volume_confirmed": volume_confirmed,
        "volume_note":      f"量比={vol_ratio:.2f}",
        "key_support":      entry * 0.97,
        "key_resistance":   entry * 1.03,
        "entry_price":      entry,
        "stop_loss":        stop_loss,
        "take_profit":      take_profit,
        "risk_reward":      f"1:{rr:.1f}",
        "divergence_risk":  False,
        "structure_broken": False,
        "confidence":       confidence,
        "reason":           (
            f"规则引擎信号：{direction}，ADX={adx:.1f}，"
            f"EMA对齐={ema_align_ok}/{total_tfs}，"
            f"量比={vol_ratio:.2f}，形态={pattern}，RSI={rsi:.1f}"
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
    atr_mult = TRADING_CFG.get("stop_loss_atr_multiplier", 2.5)
    rr_ratio = _MIN_RR_RATIO

    rule_cfg = _ANALYSIS_CFG.get("rule_filter", {})
    vol_ratio_thresh = rule_cfg.get("volume_ratio_threshold", 0.8)
    adx_trending = rule_cfg.get("adx_trending_threshold", 20)
    adx_edge_min = TRADING_CFG.get("adx_edge_min", 20)
    adx_edge_max = TRADING_CFG.get("adx_edge_max", 25)
    long_rsi_low = rule_cfg.get("long_signal_rsi_low", 40)
    rsi_oversold = rule_cfg.get("rsi_oversold", 20)
    rsi_overbought = rule_cfg.get("rsi_overbought", 80)
    recent_momentum_pct = rule_cfg.get("recent_trend_min_pct", 0.65)

    vol_burst_thresh = vol_ratio_thresh * 2
    momentum_strong_pct = int(recent_momentum_pct * 100)

    return f"""你是一位激进型裸K趋势交易员，专注加密货币合约单边行情，追求高胜率的趋势早期入场。

系统规则引擎已完成硬过滤（EMA排列、ADX门槛、RSI保护、背离检测），以下是通过预过滤的市场快照：

{{market_snapshot}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
你的任务：基于快照做"软判断"，评估入场时机的成熟度
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
- RSI 位置理想：
  - 做多：RSI {rsi_oversold}-{long_rsi_low}（回调充分但未超卖）：+1.5分
  - 做空：RSI {long_rsi_low}-{rsi_overbought - 10}（反弹充分但未超买）：+1.5分
- 近期动能强劲（空头/多头占比 >= {momentum_strong_pct}%）：+1分

**减分项：**
- RSI 趋势与信号方向相反（如做空但 RSI 连升2轮）：-2分
- ADX 处于边缘区（{adx_edge_min}-{adx_edge_max}）：-1分
- 量能不足（量比 < {vol_ratio_thresh}）：-2分
- {base_tf} 入场周期极端缩量（量比 < 0.1）：额外-2分（流动性陷阱风险）

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

    # 获取熔断器实例
    cb = get_llm_circuit_breaker()

    # 检查熔断器状态
    if cb.state.value == "open":
        logger.warning(f"[熔断器] LLM 熔断中，返回降级结果")
        return cb._get_fallback_result()

    text_cfg = _get_text_llm_cfg()
    primary_model  = text_cfg.get("primary_model",  "qwen3.5-plus")
    fallback_model = text_cfg.get("fallback_model", "qwen-max")
    max_tokens     = text_cfg.get("max_tokens", 2000)
    timeout        = text_cfg.get("timeout", 30)
    base_url       = text_cfg.get("base_url",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1")

    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    client  = OpenAI(api_key=api_key, base_url=base_url)

    prompt = TEXT_ANALYSIS_PROMPT.format(market_snapshot=market_snapshot)
    messages = [{"role": "user", "content": prompt}]

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
            # 成功返回，重置熔断器计数
            cb._on_success()
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"[文本 LLM] {model_name} 调用失败：{e}")

    logger.error("[文本 LLM] 所有文本模型均失败")
    # 记录失败到熔断器
    cb._on_failure()
    return _default_wait_response(f"文本 LLM 所有模型均失败：{last_error}")




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
                base_tf = TIMEFRAMES[-1] if TIMEFRAMES else "15m"
                base_ind = tf_indicators.get(base_tf, {})
                atr = base_ind.get("atr", entry * 0.01)
                adx = anchor_ind.get("adx", {}).get("adx", 0) if isinstance(anchor_ind.get("adx"), dict) else float(anchor_ind.get("adx", 0) or 0)

                # 动态止损
                stop_loss, multiplier_used = calculate_dynamic_stop_loss(
                    entry_price=entry,
                    atr=atr,
                    signal=signal,
                    adx=adx
                )

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
                result["_dynamic_stop_loss_applied"] = True
                result["_tp_reason"] = tp_reason
                logger.info(f"已应用动态止损止盈：SL={stop_loss:.6g}, TP={take_profit:.6g}, {tp_reason}")

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
