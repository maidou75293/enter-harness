"""Harness 端口共用的数据结构。

只放跨端口传递的稳定字段；存储形态、厂商字段一律不进这里。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


@dataclass
class RawInput:
    """采集到的原始输入；尚未归一成 Block。"""

    source: str = "user"          # user / agent / tool / event / file
    content: str = ""
    kind_hint: str = ""           # 建议的块类型，由内核归一规则最终决定
    scope: str = ""
    scope_id: str = ""
    meta: dict = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self):
        self.source = str(self.source or "user").strip().lower()
        self.kind_hint = str(self.kind_hint or "").strip().lower()
        self.scope = str(self.scope or "").strip().lower()
        self.created_at = float(self.created_at or now())

    @property
    def at(self) -> float:
        """兼容旧字段名；新代码统一用 created_at。"""
        return self.created_at

    @at.setter
    def at(self, value: float) -> None:
        self.created_at = float(value or 0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActionResult:
    """一次动作的统一结果：ok / partial / error / denied / needs_approval。"""

    ok: bool = True
    status: str = "ok"
    result: Any = None
    error: str = ""
    approval_id: str = ""
    duration_ms: int = 0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApprovalTicket:
    """审批单：暂停时落这张单，人工裁决后 resume。"""

    id: str = ""
    tenant_id: str = ""
    actor: str = ""
    tool: str = ""
    payload: dict = field(default_factory=dict)   # {"args": ..., "ctx": ...}
    scope: str = "user"
    status: str = "pending"                       # pending / approved / rejected / expired
    reason: str = ""
    created_at: float = 0.0
    decided_at: float = 0.0
    decided_by: str = ""

    def __post_init__(self):
        self.id = self.id or new_id("ap")
        self.created_at = float(self.created_at or now())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    """验收结论；只判定，不执行修复。"""

    passed: bool = True
    score: float = 1.0
    reasons: list = field(default_factory=list)
    retryable: bool = True
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditEvent:
    """审计事件；hash/prev_hash 由 AuditPort 计算，形成哈希链。"""

    id: str = ""
    tenant_id: str = ""
    actor: str = ""
    action: str = ""
    target: str = ""
    decision: str = ""
    reason: str = ""
    trace_id: str = ""
    created_at: float = 0.0
    prev_hash: str = ""
    hash: str = ""

    def __post_init__(self):
        self.id = self.id or new_id("au")
        self.created_at = float(self.created_at or now())

    @property
    def at(self) -> float:
        """兼容旧字段名；新代码统一用 created_at。"""
        return self.created_at

    @at.setter
    def at(self, value: float) -> None:
        self.created_at = float(value or 0)

    def payload(self) -> dict:
        """参与哈希计算的字段（不含 hash 自身）。"""
        return {
            "id": self.id, "tenant_id": self.tenant_id, "actor": self.actor,
            "action": self.action, "target": self.target,
            "decision": self.decision, "reason": self.reason,
            "trace_id": self.trace_id, "created_at": self.created_at,
            "prev_hash": self.prev_hash,
        }

    def canonical(self) -> str:
        return json.dumps(self.payload(), sort_keys=True, ensure_ascii=False,
                          default=str)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Checkpoint:
    """运行检查点；state 可以是任意可 JSON 化的字典。"""

    id: str = ""
    thread_id: str = ""
    node: str = ""
    state: dict = field(default_factory=dict)
    parent_id: str = ""
    created_at: float = 0.0

    def __post_init__(self):
        self.id = self.id or new_id("ck")
        self.created_at = float(self.created_at or now())

    def to_dict(self, include_state: bool = True) -> dict:
        d = {"id": self.id, "thread_id": self.thread_id, "node": self.node,
             "parent_id": self.parent_id, "created_at": self.created_at}
        if include_state:
            d["state"] = self.state
        return d
