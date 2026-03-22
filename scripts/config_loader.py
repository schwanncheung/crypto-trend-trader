"""
config_loader.py
统一配置加载入口
- 业务参数从 config/settings.yaml 读取
- 敏感密钥从 .env 读取
- 启动时校验必要配置是否齐全
"""

import os
import sys
import yaml
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 项目根目录
ROOT_DIR = Path(__file__).parent.parent

# 加载 .env（明确指定路径，避免工作目录不同导致找不到）
load_dotenv(dotenv_path=ROOT_DIR / ".env", override=True)


def load_settings() -> dict:
    """读取 settings.yaml"""
    cfg_path = ROOT_DIR / "config" / "settings.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_name: str, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 20) -> None:
    """
    统一日志配置：控制台 + 按大小滚动的文件日志

    参数：
        log_name:     日志文件名（不含 .log），如 "market_scanner"
        max_bytes:    单文件最大字节数，默认 10MB
        backup_count: 保留的历史文件数，默认 20（即最多占用 200MB）
    """
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{log_name}.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    # 避免重复添加 handler（脚本被多次 import 时）
    if not root.handlers:
        root.setLevel(logging.INFO)
        root.addHandler(file_handler)
        root.addHandler(console_handler)


# ── 业务配置 ──────────────────────────────────
CFG = load_settings()

# ── 敏感配置（从 .env 读取）──────────────────
EXCHANGE_API_KEY    = os.getenv("EXCHANGE_API_KEY", "")
EXCHANGE_API_SECRET = os.getenv("EXCHANGE_API_SECRET", "")
EXCHANGE_PASSPHRASE = os.getenv("EXCHANGE_PASSPHRASE", "")
DASHSCOPE_API_KEY   = os.getenv("DASHSCOPE_API_KEY", "")


def check_env():
    """
    启动时校验必要环境变量
    缺少任何一个直接抛出异常，避免程序带着空配置运行
    """
    required = {
        "EXCHANGE_API_KEY":    EXCHANGE_API_KEY,
        "EXCHANGE_API_SECRET": EXCHANGE_API_SECRET,
        "EXCHANGE_PASSPHRASE": EXCHANGE_PASSPHRASE,
        "DASHSCOPE_API_KEY":   DASHSCOPE_API_KEY,
    }

    missing = [k for k, v in required.items() if not v]

    if missing:
        raise EnvironmentError(
            f"\n❌ 以下环境变量未配置：{missing}\n"
            f"请检查项目根目录下的 .env 文件\n"
            f".env 路径：{ROOT_DIR / '.env'}"
        )

# ── 便捷访问 ──────────────────────────────────
EXCHANGE_CFG     = CFG.get("exchange", {})
AI_CFG           = CFG.get("ai", {})
RISK_CFG         = CFG.get("risk", {})
SCANNER_CFG      = CFG.get("scanner", {})
CHART_CFG        = CFG.get("chart", {})
TRADING_CFG      = CFG.get("trading", {})
ANALYSIS_CFG     = CFG.get("analysis", {})
TRADE_MGR_CFG    = CFG.get("trade_manager", {})
KLINE_CFG        = CFG.get("kline", {})
# 全局时间框架列表（高周期→低周期），所有模块统一使用
TIMEFRAMES       = CFG.get("timeframes", ["4h", "1h", "15m"])