"""统一上下文拼装流水线：

    collect → authorize → rank → budget → compose → trace

产品只提供 ContextPort（块）与 AccessPolicyPort（权限），本模块负责其余步骤。
缓存键是 (block_id, content_hash)：改了内容忘了升 version 也能兜住。
"""
from __future__ import annotations

from typing import Any, Iterable

from ..errors import ContextOverflow
from .model import Block, BlockMeta, content_hash, derive_authority


def estimate_tokens(text: str) -> int:
    """粗估：中英混排按 2 字符 ≈ 1 token，只用于记账。"""
    n = len(str(text or ""))
    return (n + 1) // 2 if n else 0


class BlockCache:
    """进程内缓存；真源仍是存储，多副本部署不能把这里当权威。"""

    def __init__(self):
        self._items: dict[tuple, Block] = {}

    @staticmethod
    def key(meta: BlockMeta) -> tuple:
        return (meta.id, meta.content_hash or f"v{meta.version}")

    def get(self, meta: BlockMeta) -> Block | None:
        return self._items.get(self.key(meta))

    def put(self, block: Block) -> None:
        self._items[self.key(block.meta)] = block

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def _as_meta(item: Any) -> BlockMeta:
    if isinstance(item, BlockMeta):
        return item
    if isinstance(item, Block):
        return item.meta
    if isinstance(item, dict):
        return BlockMeta(**item)
    raise TypeError(f"不支持的块类型：{type(item)!r}")


def assemble(*, context=None, identity: dict | None = None,
             policy: Any = None, scope: str = "", scope_id: str = "",
             extra_blocks: Iterable[Any] = (), max_total_chars: int = 32000,
             cache: BlockCache | None = None,
             default_max_chars: int = 0) -> dict:
    """返回 {text, blocks, trace, used_chars, dropped, truncated}。

    - extra_blocks：调用方直接给正文的块（Block / BlockMeta），与 ContextPort 合并；
    - policy 为空 = 不做鉴权（单租户低配产品）；
    - 空集正常返回空文本，不报错。
    """
    ident = identity or {}
    cache = cache if cache is not None else BlockCache()
    trace: list[dict] = []
    dropped: list[str] = []
    truncated: list[str] = []

    # 1) collect
    provided: dict[str, Block] = {}
    metas: list[BlockMeta] = []
    if context is not None:
        try:
            metas.extend(_as_meta(m) for m in (context.list_blocks(scope, scope_id) or ()))
        except Exception as e:  # noqa: BLE001 —— 块来源坏了不能拖垮主链路
            trace.append({"id": "context.list_blocks", "error": type(e).__name__})
    for item in extra_blocks or ():
        meta = _as_meta(item)
        metas.append(meta)
        if isinstance(item, Block) and item.text:
            provided[meta.id] = item

    # 2) authorize
    allowed: list[BlockMeta] = []
    for m in metas:
        ok = True
        if policy is not None:
            try:
                ok = bool(policy.can(ident, "read", m))
            except Exception:  # noqa: BLE001 —— 鉴权异常按拒绝处理
                ok = False
        if ok:
            allowed.append(m)
        else:
            trace.append({"id": m.id, "kind": m.kind,
                          "authority": derive_authority(m.kind),
                          "dropped": True, "reason": "denied"})
            dropped.append(m.id)

    # 3) rank
    allowed.sort(key=lambda m: m.sort_key)

    # 4) fetch（缓存键 = id + content_hash）
    blocks: list[tuple[Block, str]] = []
    for m in allowed:
        inline = provided.get(m.id)
        if inline is not None:
            block, state = inline, "inline"
        else:
            hit = cache.get(m)
            if hit is not None:
                block, state = hit, "hit"
            else:
                block, state = None, "fetched"
                if context is not None and hasattr(context, "get_block"):
                    try:
                        block = context.get_block(m.id, m.version)
                    except Exception as e:  # noqa: BLE001
                        trace.append({"id": m.id, "error": type(e).__name__})
                        continue
                if block is None:
                    # 只给元数据、没有正文：保留块位但不猜内容
                    block = Block(m, "")
                else:
                    block.meta = _merge_meta(m, block.meta)
                if block.text and not block.meta.content_hash:
                    block.meta.content_hash = content_hash(block.text)
                if m.content_hash and block.text and block.meta.content_hash != m.content_hash:
                    trace.append({"id": m.id, "hash_mismatch": True,
                                  "meta": m.content_hash,
                                  "body": block.meta.content_hash})
                cache.put(block)
        blocks.append((block, state))

    # 5) budget（先去重，再按单块上限与总预算裁剪）
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    kept: list[Block] = []
    parts: list[str] = []
    used = 0
    for b, state in blocks:
        meta = b.meta
        if meta.id in seen_ids:
            trace.append({"id": meta.id, "dropped": True, "reason": "duplicate_id"})
            dropped.append(meta.id)
            continue
        seen_ids.add(meta.id)
        text = str(b.text or "").strip()
        if not text:
            trace.append({"id": meta.id, "kind": meta.kind,
                          "authority": derive_authority(meta.kind),
                          "cache": state, "chars": 0,
                          "dropped": True, "reason": "empty"})
            continue
        if text in seen_text:
            trace.append({"id": meta.id, "dropped": True, "reason": "duplicate_text"})
            dropped.append(meta.id)
            continue
        seen_text.add(text)
        limit = int(meta.max_chars or default_max_chars or 0)
        if limit and len(text) > limit:
            # 规范/规则段永不截断：超长就是配置错误，必须可见失败
            if meta.kind == "procedural":
                raise ContextOverflow(
                    f"规范/规则块 {meta.id} 长度 {len(text)} 超过上限 {limit}，"
                    "请拆分或调整上限（禁止截断）")
            trace.append({"id": meta.id, "kind": meta.kind, "dropped": True,
                          "reason": "max_chars"})
            dropped.append(meta.id)
            continue
        if max_total_chars and used + len(text) > max_total_chars:
            if meta.kind == "procedural":
                raise ContextOverflow(
                    f"规范/规则块 {meta.id} 放不进总预算 {max_total_chars}"
                    f"（已用 {used}），禁止截断")
            # 超预算整块丢弃，不输出半段
            trace.append({"id": meta.id, "kind": meta.kind, "dropped": True,
                          "reason": "budget"})
            dropped.append(meta.id)
            continue
        kept.append(b)
        parts.append(text)
        used += len(text)
        trace.append({
            "id": meta.id, "kind": meta.kind, "authority": derive_authority(meta.kind),
            "scope": meta.scope, "priority": meta.priority,
            "version": meta.version, "content_hash": meta.content_hash,
            "cache": state,
            "chars": len(text), "est_tokens": estimate_tokens(text),
            "truncated": False,
        })

    # 6) compose
    return {
        "text": "\n\n".join(parts),
        "blocks": [{
            "id": b.meta.id, "kind": b.meta.kind,
            "authority": derive_authority(b.meta.kind),
            "scope": b.meta.scope, "version": b.meta.version,
            "content_hash": b.meta.content_hash,
        } for b in kept],
        "trace": trace,
        "used_chars": used,
        "dropped": dropped,
        "truncated": truncated,
    }


def _merge_meta(meta: BlockMeta, fetched: BlockMeta | None) -> BlockMeta:
    """以 list_blocks 的元数据为准，补上 get_block 带回的正文指纹。"""
    if fetched is None:
        return meta
    if not meta.content_hash and fetched.content_hash:
        meta.content_hash = fetched.content_hash
    if not meta.max_chars and fetched.max_chars:
        meta.max_chars = fetched.max_chars
    return meta
