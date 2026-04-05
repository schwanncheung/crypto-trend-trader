#!/usr/bin/env python3
"""
file_lock.py
文件锁工具模块
为 JSON 状态文件提供原子写入保证，防止并发损坏
"""

import fcntl
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class FileLock:
    """上下文管理器：文件锁"""

    def __init__(self, file_path: Path, mode: str = 'r'):
        self.file_path = file_path
        self.mode = mode
        self.fd = None

    def __enter__(self):
        # 确保父目录存在
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        # 打开文件
        self.fd = open(self.file_path, self.mode)
        # 读模式用共享锁，写/追加模式用排他锁
        lock_type = fcntl.LOCK_SH if self.mode == 'r' else fcntl.LOCK_EX
        fcntl.flock(self.fd.fileno(), lock_type)
        return self.fd

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd:
            # 释放锁
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            self.fd.close()


def atomic_read_json(file_path: Path, default: Any = None) -> Any:
    """
    原子读取 JSON 文件（带共享锁）
    文件不存在时返回 default
    """
    if not file_path.exists():
        return default

    try:
        with FileLock(file_path, 'r') as f:
            content = f.read()
            if not content:
                return default
            return json.loads(content)
    except Exception:
        return default


def atomic_write_json(file_path: Path, data: Any, indent: int = 2) -> bool:
    """
    原子写入 JSON 文件（带排他锁）
    返回是否成功
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(file_path, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
        return True
    except Exception as e:
        logger.error(f"原子写入失败 {file_path}: {e}")
        return False


def atomic_update_json(file_path: Path, update_fn: callable, default: Dict = None) -> bool:
    """
    原子更新 JSON 文件（读取 - 修改 - 写入原子操作）

    参数：
        file_path: JSON 文件路径
        update_fn: 回调函数，接收当前数据 dict，返回更新后的 dict
        default: 文件不存在时的默认数据

    返回：
        是否成功
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # 使用读写锁模式
        with FileLock(file_path, 'a+') as f:
            # 读取当前内容
            f.seek(0)
            content = f.read()

            if content:
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    data = default or {}
            else:
                data = default or {}

            # 应用更新
            updated_data = update_fn(data)

            # 写回
            f.seek(0)
            f.truncate()
            json.dump(updated_data, f, ensure_ascii=False, indent=2)
            f.flush()

        return True
    except Exception as e:
        logger.error(f"原子更新失败 {file_path}: {e}")
        return False


# 带超时的非阻塞锁版本
def try_acquire_lock(file_path: Path, timeout_sec: float = 5.0) -> Tuple[bool, Optional[Any]]:
    """
    尝试获取文件锁，超时则放弃
    用于不希望阻塞的场景

    返回：
        (success, fd) - 成功时返回文件描述符，失败时 fd 为 None
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    start_time = time.time()

    while time.time() - start_time < timeout_sec:
        try:
            fd = open(file_path, 'a+')
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True, fd
        except (BlockingIOError, OSError):
            if fd:
                fd.close()
                fd = None
            time.sleep(0.1)  # 等待 100ms 后重试

    return False, None


def release_lock(fd) -> None:
    """释放文件锁"""
    if fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()
        except Exception:
            pass
