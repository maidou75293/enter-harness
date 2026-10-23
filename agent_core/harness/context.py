"""把记忆记录适配成上下文块（ContextPort 实现）。

职责边界：只做「MemoryRecord → BlockMeta / Block」的翻译。

- **不做**预算、排序、鉴权——这些归 ``assemble()`` 与 AccessPolicyPort；
- ``list_blocks`` 只返回元数据，不读正文（每轮版本比对要便宜）；
- ``get_block`` 才取正文，正文口径是 ``summary or content``；
- 只暴露本租户、``status == "active"`` 的记录（存储没挡住的，这里再挡一层）。
"""
from __future__ import annotations

from typing import Sequence

from ..context.model import Block, BlockMeta
from ..context.ports import MemoryPort


class MemoryContextPort:
    """MemoryPort → ContextPort 的默认适配器（无需继承，结构化匹配协议）。"""

    def __init__(self, memory: MemoryPort, tenant_id: str = "",
                 default_scope: str = "", default_scope_id: str = ""):
        self.memory = memory
        self.tenant_id = str(tenant_id or "")
        self.default_scope = str(default_scope or "")
        self.default_scope_id = str(default_scope_id or "")

    # ------------------------------------------------------------ 内部
    def _visible(self, rec) -> bool:
        """只认本租户的 active 记录；防止自定义 MemoryPort 漏过滤。"""
        status = str(getattr(rec, "status", "") or "")
        tenant = str(getattr(rec, "tenant_id", "") or "")
        return status == "active" and tenant == self.tenant_id

    @staticmethod
    def _to_meta(rec) -> BlockMeta:
        """记录 → 块元数据；priority 用 (1 - importance) * 1000（越重要越靠前）。"""
        importance = float(getattr(rec, "importance", 0.5) or 0.0)
        return BlockMeta(
            id=str(getattr(rec, "id", "") or ""),
            kind=str(getattr(rec, "kind", "") or "semantic"),
            scope=str(getattr(rec, "scope", "") or "project"),
            scope_id=str(getattr(rec, "scope_id", "") or ""),
            tenant_id=str(getattr(rec, "tenant_id", "") or ""),
            priority=int((1.0 - importance) * 1000),
            version=int(getattr(rec, "version", 1) or 1),
            content_hash=str(getattr(rec, "content_hash", "") or ""),
            source=str(getattr(rec, "source", "") or ""),
            owner="memory",
            cacheable=True,
        )

    # ------------------------------------------------------------ ContextPort
    def list_blocks(self, scope: str = "", scope_id: str = "") -> Sequence[BlockMeta]:
        """按 scope / scope_id 查询记录并转成块元数据。

        传入为空时回退到构造参数里的默认 scope；同一 id 只保留一次。
        """
        req = {
            "tenant_id": self.tenant_id,
            "scope": str(scope or self.default_scope or ""),
            "scope_id": str(scope_id or self.default_scope_id or ""),
        }
        metas: list[BlockMeta] = []
        seen: set[str] = set()
        for rec in self.memory.query(req) or ():
            if rec is None or not self._visible(rec):
                continue
            rid = str(getattr(rec, "id", "") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            metas.append(self._to_meta(rec))
        return metas

    def get_block(self, block_id: str, version: int = 0) -> Block | None:
        """按 id 取正文；优先用 StoreMemoryPort 暴露的 store。

        记录不存在、租户不匹配或已非 active 时返回 None。
        ``version`` 仅为协议兼容：存储里只有当前版本，不做多版本回取。
        """
        getter = getattr(self.memory, "get", None)
        if not callable(getter):
            store = getattr(self.memory, "store", None)
            getter = getattr(store, "get", None)
        if not callable(getter):
            return None
        rec = getter(block_id)
        if rec is None or not self._visible(rec):
            return None
        text = str(getattr(rec, "summary", "") or getattr(rec, "content", "") or "")
        return Block(self._to_meta(rec), text)
