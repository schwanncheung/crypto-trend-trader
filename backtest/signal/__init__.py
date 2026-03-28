from __future__ import annotations

# 懒加载：避免 Python 在解析标准库 signal 模块时意外导入本包，
# 导致 import signal（标准库）时触发循环引用错误。
__all__ = ["SignalPipeline", "RuleOnlyMock", "LLMMockCache"]


def __getattr__(name: str):
    if name == "SignalPipeline":
        from .pipeline import SignalPipeline
        return SignalPipeline
    if name in ("RuleOnlyMock", "LLMMockCache"):
        from .ai_mock import RuleOnlyMock, LLMMockCache
        return RuleOnlyMock if name == "RuleOnlyMock" else LLMMockCache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
