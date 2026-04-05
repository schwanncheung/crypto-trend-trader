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
    import time as _time

    class CSTFormatter(logging.Formatter):
        """强制使用北京时间（UTC+8）格式化日志时间，避免受服务器本地时区影响。"""
        _CST_OFFSET = 8 * 3600  # 秒

        def converter(self, timestamp):
            return _time.gmtime(timestamp + self._CST_OFFSET)

        def formatTime(self, record, datefmt=None):
            ct = self.converter(record.created)
            if datefmt:
                return _time.strftime(datefmt, ct)
            return _time.strftime("%Y-%m-%d %H:%M:%S", ct) + f",{int(record.msecs):03d}"

    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{log_name}.log"

    fmt = CSTFormatter("%(asctime)s [%(levelname)s] %(message)s")

    root = logging.getLogger()

    # 清理所有已存在的文件 handler，避免日志重复写入多个文件
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, RotatingFileHandler)
    ]

    # 添加当前模块的文件 handler
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # console handler：只加一个
    has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
                      for h in root.handlers)
    if not has_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

    root.setLevel(logging.INFO)


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
RISK_CFG         = CFG.get("risk", {})
SCANNER_CFG      = CFG.get("scanner", {})
TRADING_CFG      = CFG.get("trading", {})
ANALYSIS_CFG     = CFG.get("analysis", {})
TRADE_MGR_CFG    = CFG.get("trade_manager", {})
KLINE_CFG        = CFG.get("kline", {})
# 全局时间框架列表（高周期→低周期），所有模块统一使用
TIMEFRAMES       = CFG.get("timeframes", ["4h", "1h", "15m"])


# ── 时间工具（统一使用北京时间 CST = UTC+8）────────────────────
from datetime import datetime, timezone, timedelta

_CST = timezone(timedelta(hours=8))

def now_cst() -> datetime:
    """返回当前北京时间（带时区信息）"""
    return datetime.now(_CST)

def now_cst_str(fmt: str = "%Y%m%d_%H%M%S") -> str:
    """返回当前北京时间字符串，默认格式 YYYYmmdd_HHMMSS"""
    return now_cst().strftime(fmt)