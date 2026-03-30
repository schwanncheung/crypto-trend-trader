from __future__ import annotations
import importlib as _importlib
import sys as _sys

# 懒加载：避免 Python 在解析标准库 signal 模块时意外导入本包，
# 导致 import signal（标准库）时触发循环引用错误。
__all__ = ["SignalPipeline", "RuleOnlyMock", "LLMMockCache"]

# 提前把真正的标准库 signal 缓存起来（趁本包还未注册到 sys.modules 前）
import signal as _signal_stdlib  # noqa: E402


def __getattr__(name: str):
    if name == "SignalPipeline":
        from .pipeline import SignalPipeline
        return SignalPipeline
    if name in ("RuleOnlyMock", "LLMMockCache"):
        from .ai_mock import RuleOnlyMock, LLMMockCache
        return RuleOnlyMock if name == "RuleOnlyMock" else LLMMockCache
    # 当本包被误当作标准库 signal 模块使用时（multiprocessing 内部 import signal），
    # 透传到真正的标准库 signal 模块，避免 AttributeError 阻断子进程初始化。
    if hasattr(_signal_stdlib, name):
        return getattr(_signal_stdlib, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
