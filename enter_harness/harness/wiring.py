"""端口装配检查：宿主启动时确认 10 个端口都实现了。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ports import CORE_PORT_NAMES, PORT_PROTOCOLS


@dataclass
class PortSet:
    identity: Any = None
    context: Any = None
    memory: Any = None
    knowledge: Any = None
    policy: Any = None
    ingest: Any = None
    action: Any = None
    state: Any = None
    verify: Any = None
    audit: Any = None

    def missing(self) -> list[str]:
        return [name for name in CORE_PORT_NAMES
                if getattr(self, name, None) is None]

    def invalid(self) -> list[str]:
        """返回未满足协议的名字（None 也算未实现）。"""
        out = []
        for name in CORE_PORT_NAMES:
            value = getattr(self, name, None)
            if value is None or not isinstance(value, PORT_PROTOCOLS[name]):
                out.append(name)
        return out

    def validate(self) -> list[str]:
        """端口内部自检：实现了 validate() 的端口返回问题列表。"""
        out: list[str] = []
        for name in CORE_PORT_NAMES:
            port = getattr(self, name, None)
            validator = getattr(port, "validate", None)
            if not callable(validator):
                continue
            try:
                errors = validator() or []
            except Exception as e:  # noqa: BLE001
                out.append(f"{name}:validate_failed:{type(e).__name__}:{e}")
                continue
            out.extend(f"{name}:{err}" for err in errors)
        return out

    def check(self) -> bool:
        bad = self.invalid()
        if bad:
            raise TypeError(
                "enter-harness 端口不完整：" + ", ".join(bad)
                + f"（需要 {len(CORE_PORT_NAMES)} 个）")
        problems = self.validate()
        if problems:
            raise TypeError("enter-harness 端口校验失败：" + "; ".join(problems))
        return True
