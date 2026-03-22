"""
notifier.py
统一通知模块 - 飞书 Webhook
通过 .env 配置：
  FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
"""

import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")


def send_notification(message: str, title: str = "交易系统通知") -> None:
    """
    发送飞书 Webhook 消息（富文本卡片）
    未配置时降级为日志输出
    """
    logger.info(f"📢 [{title}] {message}")

    if not FEISHU_WEBHOOK_URL:
        logger.warning("未配置 FEISHU_WEBHOOK_URL，消息仅记录到日志")
        return

    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "content": message,
                            "tag": "lark_md"
                        }
                    }
                ],
                "header": {
                    "title": {
                        "content": title,
                        "tag": "plain_text"
                    },
                    "template": "blue"
                }
            }
        }
        resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code", 0) != 0:
            logger.warning(f"飞书通知返回异常：{result}")
        else:
            logger.info("飞书通知发送成功")
    except Exception as e:
        logger.error(f"飞书通知发送失败：{e}")


if __name__ == "__main__":
    send_notification(
        message="✅ 飞书通知测试\ncrypto-trend-trader 通知模块已接入",
        title="系统自检"
    )
