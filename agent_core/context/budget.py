"""上下文预算工具。

两个约束：
- 条数（window_keep）
- 字符（window_max_chars）
并且至少保留 MIN_WINDOW 条，防止一条超长消息把窗口清空。
"""
from __future__ import annotations

MIN_WINDOW = 3


def fit_window(messages: list, keep: int = 10, max_chars: int = 12000,
               min_window: int = MIN_WINDOW,
               content_of=lambda m: (m.get("content") or "")) -> list:
    """从最新往旧取，返回按时间正序的窗口。"""
    out, used = [], 0
    for m in reversed(messages):
        if len(out) >= keep:
            break
        n = len(content_of(m) or "")
        if out and used + n > max_chars and len(out) >= min_window:
            break
        out.append(m)
        used += n
    out.reverse()
    return out


def trim_messages(messages: list, max_chars: int, min_keep: int = MIN_WINDOW) -> list:
    """按总字符预算裁剪（保留首条 system + 最新消息）。"""
    if max_chars <= 0 or not messages:
        return list(messages)
    head = messages[:1] if messages and messages[0].get("role") == "system" else []
    body = messages[len(head):]
    out, used = [], 0
    for m in reversed(body):
        n = len(str(m.get("content") or ""))
        if out and used + n > max_chars and len(out) >= min_keep:
            break
        out.append(m)
        used += n
    out.reverse()
    return head + out
