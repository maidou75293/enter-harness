"""内核的 10 个治理端口。

5 个沿用统一模型已有协议（身份 / 上下文 / 记忆 / 知识 / 访问策略），
5 个为补齐运行时新增（采集 / 动作 / 状态 / 验收 / 审计）。
`ModelPort` 是基础端口，不计入这 10 个。

端口只定契约，不定存储与厂商；全部按结构化类型判断，宿主无需继承。
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ..context.ports import (AccessPolicyPort, ContextPort, IdentityPort,
                             KnowledgePort, MemoryPort)
from ..ports import ModelPort
from .types import (ActionResult, ApprovalTicket, AuditEvent, Checkpoint,
                    RawInput, Verdict)


@runtime_checkable
class IngestPort(Protocol):
    """采集：把用户输入、工具结果、环境事件、文件引用统一收进来。"""

    def collect(self, req: Any = None) -> Sequence[RawInput]:
        ...


@runtime_checkable
class ActionPort(Protocol):
    """动作：工具清单、调用、审批、恢复的唯一入口。"""

    def list_tools(self, identity: dict, scope: str = "") -> Sequence[dict]:
        ...

    async def call(self, identity: dict, name: str, args: dict,
                   ctx: Any = None) -> ActionResult:
        ...

    def request_approval(self, identity: dict, call: Any) -> ApprovalTicket:
        ...

    async def resume(self, ticket_id: str, decision: Any) -> ActionResult:
        ...


@runtime_checkable
class StatePort(Protocol):
    """状态：会话、运行状态、检查点的统一读写。"""

    def load(self, thread_id: str) -> dict | None:
        ...

    def save(self, thread_id: str, state: dict) -> None:
        ...

    def checkpoint(self, thread_id: str, node: str,
                   state: dict | None = None) -> str:
        ...

    def resume(self, thread_id: str) -> dict | None:
        ...

    def list_checkpoints(self, thread_id: str) -> Sequence[dict]:
        ...


@runtime_checkable
class VerifyPort(Protocol):
    """验收：判断结果是否满足标准，决定重试 / 补偿 / 升级。"""

    def check(self, result: Any, criteria: Any = None,
              ctx: Any = None) -> Verdict:
        ...


@runtime_checkable
class AuditPort(Protocol):
    """审计：只追加、可查询、可校验哈希链。"""

    def emit(self, event: AuditEvent) -> Any:
        ...

    def query(self, filter: Any = None) -> Sequence[AuditEvent]:
        ...


# 兼容旧名；新代码统一用 AuditPort
AuditSink = AuditPort


# 端口名 → 协议。顺序即治理清单顺序。
PORT_PROTOCOLS = {
    "identity": IdentityPort,
    "context": ContextPort,
    "memory": MemoryPort,
    "knowledge": KnowledgePort,
    "policy": AccessPolicyPort,
    "ingest": IngestPort,
    "action": ActionPort,
    "state": StatePort,
    "verify": VerifyPort,
    "audit": AuditPort,
}

# 一级模块 → 二级端口（与 docs/端口清单-v1.md §12.1 对齐）
MODULE_PORTS = {
    "definition": ("identity",),
    "observation": ("ingest",),
    "context": ("context", "memory", "knowledge"),
    "control": ("model",),
    "action": ("action", "policy"),
    "state": ("state",),
    "verification": ("verify", "audit"),
}

CORE_PORT_NAMES = tuple(PORT_PROTOCOLS)
CORE_PORT_COUNT = len(CORE_PORT_NAMES)

__all__ = [
    "IdentityPort", "ContextPort", "MemoryPort", "KnowledgePort",
    "AccessPolicyPort", "ModelPort",
    "IngestPort", "ActionPort", "StatePort", "VerifyPort", "AuditPort",
    "AuditSink",
    "PORT_PROTOCOLS", "MODULE_PORTS", "CORE_PORT_NAMES", "CORE_PORT_COUNT",
    "ActionResult", "ApprovalTicket", "AuditEvent", "Checkpoint", "RawInput",
    "Verdict",
]
