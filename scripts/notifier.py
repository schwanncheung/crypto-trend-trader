"""
notifier.py
统一通知模块 - 飞书 Webhook
通过 .env 配置：
  FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
"""

import os
import logging
import time
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")


def send_notification(message: str, title: str = "", cooldown_key: str = "", cooldown_seconds: int = 0) -> None:
    """
    发送飞书 Webhook 消息（富文本卡片）
    未配置时降级为日志输出；title 为空时不显示卡片标题
    cooldown_key + cooldown_seconds：同类通知的冷却机制（跨进程持久化到文件）
    """
    # 冷却检查
    if cooldown_key and cooldown_seconds > 0:
        cache_file = Path("logs/notify_cooldown.json")
        try:
            if cache_file.exists():
                data = json.loads(cache_file.read_text())
            else:
                data = {}
            last_sent = data.get(cooldown_key, 0)
            if last_sent and (time.time() - last_sent) < cooldown_seconds:
                logger.info(f"[{cooldown_key}] 冷却中（{cooldown_seconds}秒），跳过通知")
                return
            data[cooldown_key] = int(time.time())
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"冷却检查异常（继续发送）：{e}")

    if not FEISHU_WEBHOOK_URL:
        logger.warning("未配置 FEISHU_WEBHOOK_URL，消息仅记录到日志")
        return

    try:
        card: dict = {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "content": message,
                        "tag": "lark_md"
                    }
                }
            ]
        }
        if title:
            card["header"] = {
                "title": {"content": title, "tag": "plain_text"},
                "template": "blue"
            }
        payload = {"msg_type": "interactive", "card": card}
        resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code", 0) != 0:
            logger.warning(f"飞书通知返回异常：{result}")
    except Exception as e:
        logger.error(f"飞书通知发送失败：{e}")


if __name__ == "__main__":
    send_notification(
        message="✅ 飞书通知测试\ncrypto-trend-trader 通知模块已接入",
        title="系统自检"
    )
