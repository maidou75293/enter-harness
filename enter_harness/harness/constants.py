"""内核统一枚举：状态值只在 port 边界使用，业务自定义状态放产品侧。

约定：
- 全部小写、下划线分隔；
- 结果状态用 `ok / partial / error / denied / needs_approval`；
- 运行状态用 `ok / partial / failed / cancelled / blocked`；
- 写入动作用 `append / supersede / noop / invalid`。
"""
from __future__ import annotations

ACTION_STATUSES = ("ok", "partial", "error", "denied", "needs_approval")
RUN_STATUSES = ("ok", "partial", "failed", "cancelled", "blocked")
WRITE_ACTIONS = ("append", "supersede", "noop", "invalid")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")
REVIEW_STATUSES = ("pending", "approved", "rejected", "archived")
AUDIT_DECISIONS = ("start", "ok", "failed", "pending", "approved",
                   "rejected", "error")

__all__ = [
    "ACTION_STATUSES", "RUN_STATUSES", "WRITE_ACTIONS",
    "APPROVAL_STATUSES", "REVIEW_STATUSES", "AUDIT_DECISIONS",
]
