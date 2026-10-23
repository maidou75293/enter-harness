"""兼容层：旧包名 `agent_core` → `enter_harness`。

1.2.0 起工程与导入名统一为 `enter_harness`；本包保留至少两个 minor 版本，
让老宿主不必立刻改 import：

    from agent_core.harness import build_local_ports     # 仍然可用
    from enter_harness.harness import build_local_ports  # 新写法

子模块通过 meta path finder 别名到 `enter_harness.*`，两个名字拿到的是同一个
模块对象，不会出现两份状态（端口实例、注册表、contextvars 都共用）。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

import enter_harness as _impl
from enter_harness import *  # noqa: F401,F403

__version__ = _impl.__version__
__all__ = list(_impl.__all__)


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, real_name: str):
        self.real_name = real_name

    def create_module(self, spec):
        return importlib.import_module(self.real_name)

    def exec_module(self, module):
        # 复用真实模块，不重复执行
        pass


class _AliasFinder(importlib.abc.MetaPathFinder):
    prefix = "agent_core."

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.prefix):
            return None
        real = "enter_harness." + fullname[len(self.prefix):]
        try:
            importlib.import_module(real)
        except ModuleNotFoundError:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real))


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name):
    return getattr(_impl, name)
