"""记忆写入契约：先校验，再落库；非法直接返回 invalid。

产品可以自定义存储，但必须走同一条校验与四态返回，
否则「看起来写进去了」的脏数据会污染下一轮上下文。
"""
from __future__ import annotations

from typing import Any

from ..context.model import MemoryRecord, WriteResult, validate_record


def check_record(item: Any) -> WriteResult | None:
    """合法返回 None；非法返回 invalid 结果。"""
    err = validate_record(item)
    if not err:
        return None
    return WriteResult(action="invalid",
                       id=str(getattr(item, "id", "") or ""),
                       reason=err, ok=False)


def write_record(store: Any, item: MemoryRecord) -> WriteResult:
    """统一写入入口：校验 → 适配存储 → 四态。

    store 需实现 upsert(item) -> WriteResult；没有实现时返回 invalid，
    不静默吞掉。
    """
    invalid = check_record(item)
    if invalid is not None:
        return invalid
    upsert = getattr(store, "upsert", None)
    if not callable(upsert):
        return WriteResult(action="invalid", id=getattr(item, "id", ""),
                           reason="store_missing_upsert", ok=False)
    result = upsert(item)
    if isinstance(result, WriteResult):
        return result
    if isinstance(result, dict):
        return WriteResult(
            action=str(result.get("action") or "append"),
            id=str(result.get("id") or ""),
            replaced_id=str(result.get("replaced_id") or ""),
            reason=str(result.get("reason") or ""),
            ok=bool(result.get("ok", True)))
    return WriteResult(action="append", id=str(result or ""), ok=True)
