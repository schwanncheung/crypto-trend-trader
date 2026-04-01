"""
ai_analysis.py
AI视觉分析模块
调用链：qwen-vl-max -> qwen-vl-plus -> gpt-4o
"""

import os
import re
import json
import logging
import base64
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

# 配置日志：同时输出到控制台和文件
from config_loader import (
    check_env,
    AI_CFG,
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


SINGLE_TF_PROMPT = """
你是一位精通裸K交易的专业量化分析师，专注于加密货币合约趋势追踪策略。
请严格基于图表中可见的视觉信息进行分析，不要凭空推测。

分析步骤：
1. 趋势结构：观察波段高低点排列，判断HH+HL(上升)/LH+LL(下降)/横盘，以及趋势阶段
2. 关键位置：识别有效支撑位和阻力位
3. 裸K信号：扫描吞没线/锤子线/内包线/启明星/回调确认K线
4. 成交量：信号K线量能是否大于前5根均量，回调是否缩量
5. 风险：是否有顶底背离或结构打破(BOS)风险

只输出如下JSON，不要输出其他任何内容：
{
  "trend": "up或down或sideways",
  "trend_phase": "early或mid或late",
  "trend_strength": 整数1到10,
  "signal": "long或short或wait或close",
  "signal_type": "engulfing或hammer或inside_bar或morning_star或pullback或none",
  "signal_strength": 整数1到10,
  "volume_confirmed": true或false,
  "volume_note": "成交量说明",
  "key_support": 数字,
  "key_resistance": 数字,
  "entry_price": 数字,
  "stop_loss": 数字,
  "take_profit": 数字,
  "risk_reward": "1:2.3",
  "divergence_risk": true或false,
  "structure_broken": true或false,
  "confidence": "high或low",
  "reason": "不少于80字的中文分析",
  "warning": "风险提示或null"
}

约束：
- confidence为high仅在signal_strength>={min_signal_strength}且volume_confirmed=true时使用
- 不确定时signal填wait
- stop_loss和take_profit必须基于图表结构位
- 图片模糊或信息不足时confidence填low
""".replace("{min_signal_strength}", str(_MIN_SIGNAL_STRENGTH))


def _build_multi_tf_prompt(timeframes: list) -> str:
    """
    根据时间框架列表动态生成多周期视觉分析 Prompt。
    timeframes 顺序为高→低（如 ["4h", "1h", "15m"]）。
    """
    total = len(timeframes)
    tf_list = "、".join(f"第{i+1}张={tf}" for i, tf in enumerate(timeframes))
    tf_roles = "\n".join(
        f"{i+1}. {tf} {'定宏观方向（最高权重）' if i == 0 else '确认中期结构' if i == 1 else '寻找入场信号' if i == 2 else '确认精确入场'}"
        for i, tf in enumerate(timeframes)
    )
    tf_alignment_fields = "\n".join(
        f'    "{tf}": "up或down或sideways",' for tf in timeframes
    )
    strong_score = total
    mid_score = total - 1
    return f"""你是一位精通裸K交易的专业量化分析师。
我将给你{total}张K线图，顺序为：{tf_list}

使用自上而下分析法：
{tf_roles}

共振标准：alignment_score={strong_score}为强信号，{mid_score}为中信号，小于等于{total//2}等待

只输出如下JSON，不要输出其他任何内容：
{{
  "timeframe_alignment": {{
{tf_alignment_fields}
  }},
  "alignment_score": 整数0到{total},"""


MULTI_TF_PROMPT_SUFFIX = """
  "trend": "up或down或sideways",
  "trend_phase": "early或mid或late",
  "trend_strength": 整数1到10,
  "signal": "long或short或wait或close",
  "signal_type": "engulfing或hammer或inside_bar或morning_star或pullback或none",
  "signal_strength": 整数1到10,
  "volume_confirmed": true或false,
  "volume_note": "成交量说明",
  "key_support": 数字,
  "key_resistance": 数字,
  "entry_price": 数字,
  "stop_loss": 数字,
  "take_profit": 数字,
  "risk_reward": "1:2.3",
  "divergence_risk": true或false,
  "structure_broken": true或false,
  "confidence": "high或low",
  "reason": "不少于100字的多周期综合分析",
  "warning": "风险提示或null"
}
"""


# 废弃：保留变量名避免外部引用报错，实际内容由 _build_multi_tf_prompt 动态生成
MULTI_TF_PROMPT_TEMPLATE_NOTE = "(dynamic — use _build_multi_tf_prompt)"


def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def parse_ai_response(text: str) -> dict:
    """解析AI返回，兼容纯JSON和markdown包裹格式"""
    try:
        # 尝试提取markdown代码块中的JSON
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if match:
            return json.loads(match.group(1))
        # 尝试直接解析
        return json.loads(text.strip())
    except Exception:
        # 尝试找到花括号范围
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
    逻辑与回测 RuleOnlyMock 保持一致。
    """
    from config_loader import TIMEFRAMES as _TFS
    rule_filter_cfg = _ANALYSIS_CFG.get("rule_filter", {})

    min_signal_strength  = TRADING_CFG.get("min_signal_strength", 7)
    min_rr_ratio         = TRADING_CFG.get("min_rr_ratio", 2.0)
    atr_multiplier       = TRADING_CFG.get("stop_loss_atr_multiplier", 2.5)
    vol_ratio_threshold  = rule_filter_cfg.get("volume_ratio_threshold", 0.8)
    strong_trend_adx     = rule_filter_cfg.get("strong_trend_adx_threshold", 60)
    strong_trend_di_diff = rule_filter_cfg.get("strong_trend_di_diff_threshold", 20)
    timeframes           = _TFS or ["1h", "30m", "15m"]
    anchor_tf            = timeframes[0]
    base_tf              = timeframes[-1]

    # 极强趋势豁免判断
    anchor_ind = tf_indicators.get(anchor_tf, {})
    adx_info   = anchor_ind.get("adx", {})
    adx        = adx_info.get("adx", 0) if isinstance(adx_info, dict) else float(adx_info or 0)
    plus_di    = adx_info.get("plus_di", 0) if isinstance(adx_info, dict) else 0
    minus_di   = adx_info.get("minus_di", 0) if isinstance(adx_info, dict) else 0
    di_diff    = abs(plus_di - minus_di)
    strong_trend_exemption = (adx >= strong_trend_adx and di_diff >= strong_trend_di_diff)

    # 信号强度评分
    score = 0.0
    total_tfs = len(timeframes)

    # EMA 对齐评分
    expected = "up" if direction == "long" else "down"
    ema_align_ok = sum(
        1 for tf in timeframes
        if tf_indicators.get(tf, {}).get("ema", {}).get("alignment") == ("bullish" if direction == "long" else "bearish")
    )
    score += ema_align_ok * (6.0 / total_tfs)

    # 成交量评分：取最低周期量比，但 volume_confirmed 改为任一周期放量即可
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

    # K线形态评分（取最低周期第一个形态名）
    patterns_list = tf_indicators.get(base_tf, {}).get("patterns", [])
    pattern = patterns_list[0]["pattern"] if patterns_list else "none"
    if pattern not in ("none", "", None):
        score += 1.5

    # RSI 评分
    rsi = tf_indicators.get(base_tf, {}).get("rsi", 50)
    if 25 <= rsi <= 65:
        score += 1.5
    elif strong_trend_exemption and direction == "short" and rsi < 25:
        score += 1.5
    elif strong_trend_exemption and direction == "long" and rsi > 75:
        score += 1.5

    signal_strength = min(10, int(score))

    # 入场/止损/止盈（ATR动态止损 + 固定盈亏比止盈）
    base_ind = tf_indicators.get(base_tf, {})
    # current_price 从最低周期 close 取（tf_indicators 里没有直接存价格，用 atr 的 entry 估算不可靠）
    # 通过 key_support/key_resistance 近似，或让调用方传入；此处用 atr 反推不安全，
    # 改为从 tf_indicators 的 momentum.last_close 或直接读 atr entry — 暂用0触发wait
    entry = base_ind.get("current_price", 0)
    if entry <= 0:
        logger.warning(f"[rule_only] {symbol} 无法获取当前价格，返回wait")
        return _default_wait_response("rule_only模式无法获取当前价格")

    atr = base_ind.get("atr", entry * 0.01)
    if direction == "long":
        stop_loss   = entry - atr_multiplier * atr
        take_profit = entry + min_rr_ratio * (entry - stop_loss)
    else:
        stop_loss   = entry + atr_multiplier * atr
        take_profit = entry - min_rr_ratio * (stop_loss - entry)
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


def _build_qwen_messages(image_paths: list, multi_tf: bool) -> list:
    """构建千问VL的消息体"""
    if multi_tf:
        prompt = _build_multi_tf_prompt(TIMEFRAMES) + MULTI_TF_PROMPT_SUFFIX
    else:
        prompt = SINGLE_TF_PROMPT
    content = []
    for path in image_paths:
        content.append({
            "type": "image",
            "image": f"data:image/png;base64,{encode_image(path)}"
        })
    content.append({"type": "text", "text": prompt})
    return [{"role": "user", "content": content}]


def analyze_chart_qwen(
    image_paths: list,
    model_key: str = "primary",
    multi_tf: bool = False
) -> dict:
    """
    调用阿里云千问VL API分析K线图
    model_key: primary=qwen-vl-max, secondary=qwen-vl-plus
    """
    try:
        from dashscope import MultiModalConversation

        model_cfg = AI_CFG[model_key]
        model_name = model_cfg["model"]
        thinking = model_cfg.get("thinking_mode", False)

        messages = _build_qwen_messages(image_paths, multi_tf)

        params = {
            "model": model_name,
            "messages": messages,
            "api_key": DASHSCOPE_API_KEY,
        }

        # 注意：部分千问VL模型不支持 max_tokens 参数，使用 max_new_tokens
        max_tokens = model_cfg.get("max_tokens", 1500)
        if model_name.startswith("qwen3"):
            params["max_new_tokens"] = max_tokens
        else:
            params["max_tokens"] = max_tokens

        if thinking:
            params["enable_thinking"] = True

        logger.info(f"调用 {model_name} 分析 {len(image_paths)} 张图表...")
        response = MultiModalConversation.call(**params)

        if response.status_code == 200:
            text = response.output.choices[0].message.content
            if isinstance(text, list):
                # Thinking模式返回列表，过滤thinking内容只取text
                text = " ".join(
                    item.get("text", "")
                    for item in text
                    if isinstance(item, dict) and item.get("type") != "thinking"
                )
            result = parse_ai_response(text)
            result["_model_used"] = model_name
            return result
        else:
            raise Exception(
                f"API返回错误：{response.status_code} - {response.message}"
            )

    except Exception as e:
        logger.warning(f"[{model_key}] 千问VL调用失败：{e}")
        raise


def analyze_with_fallback(
    image_paths: list,
    multi_tf: bool = True
) -> dict:
    """
    带自动降级的AI分析主入口
    调用链：qwen3-vl-plus → qwen3-vl-flash → qwen-vl-max
    """
    import os

    # ✅ 入口校验：确保传入的是真实文件路径列表
    if not image_paths:
        logger.error("image_paths 为空列表")
        return _default_wait_response("图片列表为空")

    valid_paths = []
    for p in image_paths:
        # 校验类型
        if not isinstance(p, str):
            logger.error(f"路径类型错误，期望str，实际{type(p)}，值：{p}")
            continue
        # 校验文件存在
        if not os.path.exists(p):
            logger.error(f"图片文件不存在：{p}")
            continue
        # 校验是图片格式
        if not p.lower().endswith((".png", ".jpg", ".jpeg")):
            logger.error(f"非图片格式文件：{p}")
            continue
        valid_paths.append(p)

    if not valid_paths:
        logger.error(f"所有图片路径均无效，原始列表：{image_paths}")
        return _default_wait_response("所有图片路径无效")

    if len(valid_paths) < len(image_paths):
        logger.warning(
            f"部分图片路径无效：{len(image_paths)} → {len(valid_paths)} 张"
        )

    logger.info(f"开始AI分析，有效图片：{len(valid_paths)} 张")
    for p in valid_paths:
        logger.info(f"  → {p}")

    # 第一优先：qwen3-vl-plus（Thinking模式）
    try:
        return analyze_chart_qwen(valid_paths, "primary", multi_tf)
    except Exception as e:
        logger.warning(f"主力模型失败，降级到 qwen3-vl-flash：{e}")

    # 第二优先：qwen3-vl-flash
    try:
        return analyze_chart_qwen(image_paths, "secondary", multi_tf)
    except Exception as e:
        logger.warning(f"降级模型失败，降级到 qwen-vl-max：{e}")

    # 兜底：qwen-vl-max
    try:
        return analyze_chart_qwen(image_paths, "fallback", multi_tf)
    except Exception as e:
        logger.error(f"所有模型均失败：{e}")
        return _default_wait_response("所有AI分析服务暂时不可用")


def passes_risk_filter(decision: dict) -> bool:
    """
    风控过滤：只有通过所有检查才允许交易
    """
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
    image_paths: list,
    log_dir: str = "logs/decisions"
) -> str:
    """保存AI决策日志为JSON文件"""
    from datetime import datetime, timezone
    from config_loader import now_cst_str
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    ts = now_cst_str()
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    log_path = f"{log_dir}/{safe_symbol}_{timeframe}_{ts}.json"

    log_data = {
        "timestamp": ts,
        "symbol": symbol,
        "timeframe": timeframe,
        "image_paths": image_paths,
        "decision": decision
    }

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)

    logger.info(f"决策日志已保存：{log_path}")
    return log_path


# ═══════════════════════════════════════════════════════════════════════
# 文本LLM分析（规则引擎 + 文本模型，替代视觉模式）
# ═══════════════════════════════════════════════════════════════════════

def _build_text_analysis_prompt() -> str:
    """
    动态生成文本LLM分析 Prompt，时间框架从 TIMEFRAMES 配置读取。
    激进型裸K交易员视角：规则引擎做硬过滤，LLM做软判断（结构、节奏、时机）。
    """
    total = len(TIMEFRAMES)
    tf_names = "、".join(TIMEFRAMES)
    anchor_tf = TIMEFRAMES[0]
    base_tf = TIMEFRAMES[-1]
    tf_alignment_fields = "\n".join(
        f'    "{tf}": "up或down或sideways",' for tf in TIMEFRAMES
    )
    min_alignment = total - 1
    atr_mult = TRADING_CFG.get("stop_loss_atr_multiplier", 2.5)
    rr_ratio = _MIN_RR_RATIO

    # 从配置读取阈值参数
    rule_cfg = _ANALYSIS_CFG.get("rule_filter", {})
    vol_ratio_thresh = rule_cfg.get("volume_ratio_threshold", 0.8)
    adx_trending = rule_cfg.get("adx_trending_threshold", 20)
    adx_edge_min = TRADING_CFG.get("adx_edge_min", 20)
    adx_edge_max = TRADING_CFG.get("adx_edge_max", 25)
    adx_strong = rule_cfg.get("strong_trend_adx_threshold", 60)
    long_rsi_low = rule_cfg.get("long_signal_rsi_low", 40)
    rsi_oversold = rule_cfg.get("rsi_oversold", 20)
    rsi_overbought = rule_cfg.get("rsi_overbought", 80)
    recent_momentum_pct = rule_cfg.get("recent_trend_min_pct", 0.65)

    # 计算动态阈值
    vol_burst_thresh = vol_ratio_thresh * 2  # 量能爆发阈值（2倍量比）
    adx_strong_thresh = adx_trending + 10    # 强趋势 ADX 阈值
    momentum_strong_pct = int(recent_momentum_pct * 100)  # 近期动能强劲占比

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

**评分逻辑：**
- 8-10分：完美入场时机（多周期共振+放量+形态+RSI位置+动能一致）
- 6-7分：标准信号（满足基础条件，有1-2个加分项）
- 4-5分：勉强信号（基础条件满足但缺乏确认）
- 1-3分：弱信号（周期冲突或关键条件缺失）

## 三、入场价格选择（激进型策略）

**优先级：结构位 > 当前价**

1. **做多入场**：
   - 首选：最近的支撑位附近（回调到位，风险最小）
   - 次选：当前价（突破后追单，适合强趋势）

2. **做空入场**：
   - 首选：最近的阻力位附近（反弹到位，风险最小）
   - 次选：当前价（跌破后追单，适合强趋势）

**判断标准：**
- 当前价距离结构位 < 0.5% → 使用结构位
- 当前价距离结构位 > 1% → 使用当前价（已突破/跌破）

## 四、止损止盈计算（严格执行）

**止损（ATR 动态）：**
- 做多：`stop_loss = entry - {atr_mult} × ATR`
- 做空：`stop_loss = entry + {atr_mult} × ATR`

**止盈（固定盈亏比）：**
- 做多：`take_profit = entry + {rr_ratio} × (entry - stop_loss)`
- 做空：`take_profit = entry - {rr_ratio} × (stop_loss - entry)`

**计算后检查：**
- 做多：止损必须 < entry < 止盈
- 做空：止盈 < entry < 止损
- 盈亏比必须 >= {rr_ratio}:1

## 五、confidence 判定（二元规则，无 medium）

**high（同时满足3个条件）：**
1. `signal_strength >= {_MIN_SIGNAL_STRENGTH}`
2. `volume_confirmed = true`（任一周期量比 >= {vol_ratio_thresh}）
3. `alignment_score >= {min_alignment}`（多周期对齐）

**low（任一条件不满足）：**
- 直接返回 low，不解释

## 六、关键检查项（激进型交易员的经验法则）

### ✅ 做多信号加分：
- {base_tf} RSI < {long_rsi_low}（回调充分）
- 近期动能显示"多头占比 >= {int(recent_momentum_pct * 100)}%"
- {base_tf} 出现看涨吞没/锤子线/Pin Bar
- {anchor_tf} ADX >= {adx_trending + 10}（强趋势）

### ✅ 做空信号加分：
- {base_tf} RSI > {long_rsi_low}（反弹充分但未超买）
- 近期动能显示"空头占比 >= {int(recent_momentum_pct * 100)}%"
- {base_tf} 出现看跌吞没/倒锤子/Pin Bar
- {anchor_tf} ADX >= {adx_trending + 10}（强趋势）

### ⚠️ 警告触发（填入 warning 字段）：
- {anchor_tf} ADX 在 {adx_edge_min}-{adx_edge_max} 边缘区 → "ADX边缘区，趋势不明朗"
- {base_tf} RSI 趋势与信号方向相反 → "RSI动能背离，注意反转风险"
- 量比 < {vol_ratio_thresh} 但其他条件完美 → "量能不足，可能假突破"
- 所有周期 RSI 都在 45-55 中性区 → "RSI横盘震荡，方向不明"

### 🚫 强制 wait（即使规则引擎通过）：
- 对齐分数 < {min_alignment}（周期冲突）
- {anchor_tf} 趋势=sideways 且 ADX < {adx_trending}（横盘市）
- {base_tf} RSI 趋势连续3轮与信号方向相反（动能衰竭）

## 七、divergence_risk 判定

系统已检测 RSI 背离，你需要基于快照中的"RSI趋势"字段二次确认：

- **底背离**（做空风险）：价格下跌但 {base_tf} RSI 连续回升 → `true`
- **顶背离**（做多风险）：价格上涨但 {base_tf} RSI 连续下行 → `true`
- 其他情况：`false`

## 八、输出格式（严格 JSON，禁止任何额外文本）

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
  "warning": "风险提示（ADX边缘/RSI背离/量能不足/横盘震荡）或null"
}}}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
激进型交易员心法：趋势早期入场，止损严格，让利润奔跑
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


TEXT_ANALYSIS_PROMPT = _build_text_analysis_prompt()


def _get_text_llm_cfg() -> dict:
    """读取文本LLM配置（避免模块级循环导入）"""
    from config_loader import ANALYSIS_CFG
    return ANALYSIS_CFG.get("text_llm", {})


def analyze_with_text_llm(market_snapshot: str) -> dict:
    """
    使用文本LLM分析市场快照。
    调用链：qwen-plus（主力）→ qwen-max（兜底）
    """
    from openai import OpenAI
    import os

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

    for model_name in [primary_model, fallback_model]:
        try:
            logger.info(f"[文本LLM] 调用 {model_name} 分析市场快照...")
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
            logger.info(f"[文本LLM] {model_name} 返回：signal={result.get('signal')}, "
                        f"confidence={result.get('confidence')}, "
                        f"strength={result.get('signal_strength')}")
            return result
        except Exception as e:
            logger.warning(f"[文本LLM] {model_name} 调用失败：{e}")

    logger.error("[文本LLM] 所有文本模型均失败")
    return _default_wait_response("文本LLM所有模型均失败")


def analyze_symbol(
    multi_tf_data: dict,
    symbol: str,
    support_levels: list = None,
    resistance_levels: list = None,
    image_paths: dict = None,
) -> dict:
    """
    统一分析入口。根据 settings.yaml analysis.mode 自动路由：
      - mode="text" : 规则引擎预过滤 → 文本LLM分析
      - mode="visual": 生成图表 → 视觉LLM分析（原有逻辑）

    返回标准决策 dict，格式与 analyze_with_fallback 完全一致。
    """
    from indicator_engine import generate_market_snapshot, rule_engine_filter

    mode = _ANALYSIS_CFG.get("mode", "visual")
    logger.info(f"[分析入口] {symbol} 使用模式：{mode}")

    if mode == "rule_only":
        # ── 纯规则模式：规则引擎预过滤 → 指标打分构造决策，不调用任何LLM ──
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

    elif mode == "text":
        # ── 1. 计算指标 & 生成快照
        snapshot, tf_indicators = generate_market_snapshot(
            multi_tf_data, symbol, support_levels, resistance_levels
        )
        logger.info(f"[市场快照]\n{snapshot}")

        # ── 2. 规则引擎预过滤（节省token）
        passed, direction, filter_reason = rule_engine_filter(tf_indicators, symbol)
        if not passed:
            logger.info(f"[分析入口] {symbol} 规则引擎未通过，跳过LLM | 原因：{filter_reason}")
            resp = _default_wait_response(filter_reason)
            resp["_analysis_mode"] = "rule_filter_rejected"
            resp["_filter_reason"] = filter_reason
            return resp

        # ── 3. 通过规则过滤，调用文本LLM
        logger.info(f"[分析入口] {symbol} 规则引擎通过（{direction}），调用文本LLM")
        result = analyze_with_text_llm(snapshot)
        result["_rule_direction"] = direction
        result["_filter_reason"]  = filter_reason
        # 注入锚周期 ADX 供风控 ADX 边缘检查使用
        from config_loader import TIMEFRAMES
        anchor_tf = TIMEFRAMES[0] if TIMEFRAMES else "1h"
        anchor_ind = tf_indicators.get(anchor_tf, {})
        anchor_adx = anchor_ind.get("adx", {}).get("adx", None)
        if anchor_adx is not None:
            result["_anchor_adx"] = anchor_adx
        return result

    else:
        # ── 视觉模式（原有逻辑）
        if not image_paths:
            logger.error(f"[分析入口] visual模式但未传入image_paths")
            return _default_wait_response("视觉模式缺少图表文件")
        valid_images = [
            p for p in image_paths.values()
            if isinstance(p, str) and p.lower().endswith((".png", ".jpg", ".jpeg"))
            and __import__("os").path.exists(p)
        ]
        return analyze_with_fallback(valid_images, multi_tf=True)


# ── 测试入口 ──────────────────────────────────
if __name__ == "__main__":
    import sys

    # 用法：python ai_analysis.py <图片路径1> [图片路径2 ...]
    paths = sys.argv[1:]

    if not paths:
        print("用法：python ai_analysis.py <图片路径1> [图片路径2 ...]")
        print("示例（单图）：python ai_analysis.py logs/decisions/BTC_1h.png")
        print("示例（多周期）：python ai_analysis.py d.png 4h.png 1h.png 15m.png")
        sys.exit(0)

    multi = len(paths) == 4
    print(f"\n{'多周期分析' if multi else '单周期分析'}，共 {len(paths)} 张图片")
    print("调用AI分析中...\n")

    result = analyze_with_fallback(paths, multi_tf=multi)

    print("=" * 50)
    print("AI 分析结果：")
    print("=" * 50)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 50)

    passed = passes_risk_filter(result)
    print(f"\n风控过滤结果：{'✅ 通过，可以交易' if passed else '❌ 未通过，继续等待'}")

    log_path = save_decision_log(
        symbol="TEST/USDT",
        timeframe="multi" if multi else "1h",
        decision=result,
        image_paths=paths
    )
    print(f"日志已保存：{log_path}")
