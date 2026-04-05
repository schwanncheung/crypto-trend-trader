#!/usr/bin/env python3
"""
circuit_breaker.py
熔断器工具模块
用于 LLM API 调用的故障隔离与自动降级

当 API 连续失败 N 次后，熔断器打开，自动切换到 rule_only 模式
经过恢复窗口后，熔断器半开，尝试一次请求
成功则关闭熔断器，失败则继续打开
"""

import time
import logging
from enum import Enum
from typing import Callable, Any, Optional
from functools import wraps

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"       # 正常状态，允许请求
    OPEN = "open"           # 熔断状态，拒绝请求
    HALF_OPEN = "half_open" # 恢复试探，允许一次请求


class CircuitBreakerError(Exception):
    """熔断器打开时的异常"""
    pass


class CircuitBreaker:
    """
    熔断器实现

    参数：
        name: 熔断器名称（用于日志）
        failure_threshold: 连续失败多少次后打开熔断
        success_threshold: 半开状态下成功多少次后关闭
        recovery_window: 熔断后多少秒进入半开状态
        fallback_mode: 熔断后的降级模式 ("rule_only" | "reject")
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        success_threshold: int = 1,
        recovery_window: float = 300.0,  # 5 分钟
        fallback_mode: str = "rule_only"
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.recovery_window = recovery_window
        self.fallback_mode = fallback_mode

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = __import__('threading').Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            # 检查是否应该从 OPEN 转为 HALF_OPEN
            if self._state == CircuitState.OPEN and self._should_attempt_reset():
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info(f"[熔断器 {self.name}] 进入半开状态，尝试恢复")
            return self._state

    def _should_attempt_reset(self) -> bool:
        if self._last_failure_time is None:
            return True
        return (time.time() - self._last_failure_time) >= self.recovery_window

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        """
        通过熔断器调用函数

        返回：
            函数返回值，或者降级返回值

        异常：
            CircuitBreakerError: 熔断器打开时
        """
        current_state = self.state

        if current_state == CircuitState.OPEN:
            logger.warning(f"[熔断器 {self.name}] 熔断中，使用降级模式：{self.fallback_mode}")
            return self._get_fallback_result()

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"[熔断器 {self.name}] 恢复正常状态")
            else:
                # CLOSED 状态下成功，重置失败计数
                self._failure_count = 0

    def _on_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # 半开状态下失败，立即回到熔断
                self._state = CircuitState.OPEN
                logger.error(f"[熔断器 {self.name}] 半开恢复失败，重新熔断")
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.error(f"[熔断器 {self.name}] 达到失败阈值 ({self.failure_threshold})，已熔断")

    def _get_fallback_result(self) -> dict:
        """返回降级结果"""
        if self.fallback_mode == "rule_only":
            return {
                "signal": "wait",
                "confidence": "low",
                "reason": f"LLM 熔断中，自动降级到 rule_only 模式",
                "_circuit_breaker": True,
                "_fallback_mode": "rule_only"
            }
        else:  # reject
            return {
                "signal": "wait",
                "confidence": "low",
                "reason": f"LLM 服务不可用，请稍后重试",
                "_circuit_breaker": True,
                "_fallback_mode": "reject"
            }

    def reset(self):
        """手动重置熔断器"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
            logger.info(f"[熔断器 {self.name}] 手动重置")

    def get_status(self) -> dict:
        """获取熔断器状态"""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time,
        }


# 全局熔断器实例（单例模式）
_llm_circuit_breaker: Optional[CircuitBreaker] = None


def get_llm_circuit_breaker() -> CircuitBreaker:
    """获取 LLM 熔断器实例"""
    global _llm_circuit_breaker
    if _llm_circuit_breaker is None:
        from config_loader import ANALYSIS_CFG
        cb_cfg = ANALYSIS_CFG.get("circuit_breaker", {})
        _llm_circuit_breaker = CircuitBreaker(
            name="llm",
            failure_threshold=cb_cfg.get("failure_threshold", 3),
            success_threshold=cb_cfg.get("success_threshold", 1),
            recovery_window=cb_cfg.get("recovery_window_sec", 300),
            fallback_mode=cb_cfg.get("fallback_mode", "rule_only")
        )
    return _llm_circuit_breaker


def with_circuit_breaker(fn: Callable) -> Callable:
    """
    装饰器：使用熔断器包装函数

    用法：
        @with_circuit_breaker
        def call_llm_api(...):
            ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        cb = get_llm_circuit_breaker()
        try:
            return cb.call(fn, *args, **kwargs)
        except CircuitBreakerError:
            # 熔断器打开时返回降级结果
            return cb._get_fallback_result()
    return wrapper
