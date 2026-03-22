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
import sys
from pathlib import Path
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "ai_analysis.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


from config_loader import (
    check_env,
    AI_CFG,
    DASHSCOPE_API_KEY,
)

check_env()


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
  "confidence": "high或medium或low",
  "reason": "不少于80字的中文分析",
  "warning": "风险提示或null"
}

约束：
- confidence为high仅在signal_strength>=7且volume_confirmed=true时使用
- 不确定时signal填wait
- stop_loss和take_profit必须基于图表结构位
- 图片模糊或信息不足时confidence填low
"""


MULTI_TF_PROMPT = """
你是一位精通裸K交易的专业量化分析师。
我将给你4张K线图，顺序为：第1张=日线，第2张=4小时，第3张=1小时，第4张=15分钟

使用自上而下分析法：
1. 日线判断大趋势（最高权重）
2. 4小时确认中期结构
3. 1小时寻找入场信号
4. 15分钟确认精确入场点

共振标准：alignment_score=4为强信号，3为中信号，小于等于2等待

只输出如下JSON，不要输出其他任何内容：
{
  "timeframe_alignment": {
    "1d": "up或down或sideways",
    "4h": "up或down或sideways",
    "1h": "up或down或sideways",
    "15m": "up或down或sideways"
  },
  "alignment_score": 整数0到4,
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
  "confidence": "high或medium或low",
  "reason": "不少于100字的多周期综合分析",
  "warning": "风险提示或null"
}
"""


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


def _build_qwen_messages(image_paths: list, multi_tf: bool) -> list:
    """构建千问VL的消息体"""
    prompt = MULTI_TF_PROMPT if multi_tf else SINGLE_TF_PROMPT
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
        "信号强度>=7":
            decision.get("signal_strength", 0) >= 7,
        "成交量确认":
            decision.get("volume_confirmed", False) is True,
        "无背离风险":
            decision.get("divergence_risk", True) is False,
         "结构未打破":
            decision.get("structure_broken", True) is False,
         "风险回报比>=2":
            _parse_rr(decision.get("risk_reward", "1:0")) >= 2.0,
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
    from datetime import datetime
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
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
