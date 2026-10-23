"""统一模型的 5 个端口（产品实现，agent-core 只依赖协议）。

命名说明：agent-core 已有 `agent_core.ports.ToolPolicyPort`（工具执行策略），
本模块的数据访问策略命名为 `AccessPolicyPort`，两者领域不同，不合并。

ContextPort 拆成两个方法，是为了让「每轮比对版本」不必读正文：
    list_blocks() 只返回元数据（便宜）
    get_block()   只在缓存未命中时读正文
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from .model import Block, BlockMeta, WriteResult


@runtime_checkable
class IdentityPort(Protocol):
    """身份是平台事实，只读；不得从对话推断。"""

    def resolve(self, req: Any = None) -> dict:
        """至少含 user_id / roles；建议带 capabilities 或 tags。"""


@runtime_checkable
class ContextPort(Protocol):
    def list_blocks(self, scope: str = "", scope_id: str = "") -> Sequence[BlockMeta]:
        ...

    def get_block(self, block_id: str, version: int = 0) -> Block | None:
        ...


@runtime_checkable
class MemoryPort(Protocol):
    def query(self, req: Any = None) -> Sequence[Any]:
        ...

    def upsert(self, item: Any) -> WriteResult:
        ...

    def archive(self, ids: Sequence[str]) -> int:
        ...


@runtime_checkable
class KnowledgePort(Protocol):
    def search(self, query: str, top_k: int = 4,
               scope: str = "") -> Sequence[dict]:
        """每条命中必须带 source；无 source 的记录由调用方丢弃。"""

    def get_chunk(self, chunk_id: str) -> dict | None:
        """按 id 回取正文；引用复核与按需取长文用。"""


@runtime_checkable
class AccessPolicyPort(Protocol):
    def can(self, identity: dict, action: str, target: Any) -> bool:
        ...
