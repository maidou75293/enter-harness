"""轻量设置适配器。

core 内部统一通过 `as_settings(obj)` 读取配置，兼容：
- dict（扁平 key，如 {"orchestrator.max_replans": 2}）
- 实现 get_config/get_bool/get_int/get_float 的任意对象（app.Store）
- 普通对象属性（无上述方法时，按 key 取属性）
"""
from __future__ import annotations

from typing import Any


class DictSettings:
    def __init__(self, data: dict | None = None):
        self.data = dict(data or {})

    def get_config(self, key: str, default: Any = None) -> Any:
        if key in self.data:
            return self.data[key]
        # 允许嵌套 dict：{"orchestrator": {"max_replans": 2}}
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get_config(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "y")
        return bool(v)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_config(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get_config(key, default))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class ObjectSettings:
    """包装任意对象；优先用其 Settings 方法，否则退回属性/字典。"""

    def __init__(self, obj: Any):
        self.obj = obj

    def get_config(self, key: str, default: Any = None) -> Any:
        o = self.obj
        if hasattr(o, "get_config"):
            try:
                v = o.get_config(key, default)
                return default if v is None else v
            except TypeError:
                v = o.get_config(key)
                return default if v is None else v
        if isinstance(o, dict):
            return DictSettings(o).get_config(key, default)
        cur = o
        for part in key.split("."):
            if not hasattr(cur, part):
                return default
            cur = getattr(cur, part)
        return cur

    def get_bool(self, key: str, default: bool = False) -> bool:
        if hasattr(self.obj, "get_bool"):
            try:
                return bool(self.obj.get_bool(key, default))
            except TypeError:
                return bool(self.obj.get_bool(key))
        return DictSettings({"v": self.get_config(key, default)}).get_bool("v", default)

    def get_int(self, key: str, default: int = 0) -> int:
        if hasattr(self.obj, "get_int"):
            try:
                return int(self.obj.get_int(key, default))
            except TypeError:
                return int(self.obj.get_int(key))
        try:
            return int(self.get_config(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        if hasattr(self.obj, "get_float"):
            try:
                return float(self.obj.get_float(key, default))
            except TypeError:
                return float(self.obj.get_float(key))
        try:
            return float(self.get_config(key, default))
        except (TypeError, ValueError):
            return default


def as_settings(obj: Any) -> Any:
    """返回一个具备 get_config/get_bool/get_int/get_float 的对象。"""
    if obj is None:
        return DictSettings()
    if isinstance(obj, DictSettings):
        return obj
    if all(hasattr(obj, m) for m in ("get_config", "get_bool", "get_int", "get_float")):
        return obj
    if isinstance(obj, dict):
        return DictSettings(obj)
    return ObjectSettings(obj)


def cfg(settings: Any, key: str, default: Any = None) -> Any:
    return as_settings(settings).get_config(key, default)


def cfg_int(settings: Any, key: str, default: int = 0) -> int:
    return as_settings(settings).get_int(key, default)


def cfg_float(settings: Any, key: str, default: float = 0.0) -> float:
    return as_settings(settings).get_float(key, default)


def cfg_bool(settings: Any, key: str, default: bool = False) -> bool:
    return as_settings(settings).get_bool(key, default)
