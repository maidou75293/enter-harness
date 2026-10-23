"""任务级评测（eval）子包：执行用例集合并给出通过率。

对外稳定符号：EvalCase、EvalResult、EvalReport、run_cases、load_cases。
符号采用惰性导入，保证 ``python -m enter_harness.eval.runner`` 不会在
包初始化阶段提前加载 runner 模块。命令行入口见该模块。
"""
from __future__ import annotations

__all__ = ["EvalCase", "EvalResult", "EvalReport", "run_cases", "load_cases"]


def __getattr__(name: str):
    """按需从 runner 模块取出公开符号（PEP 562 惰性导出）。"""
    if name in __all__:
        import importlib
        module = importlib.import_module(".runner", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
