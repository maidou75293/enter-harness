"""ContextManager：组装模型窗口 + 压缩(compaction) + 可选记忆注入。

会话隔离：一个会话一个窗口，不跨会话共享。
两层：
  窗口  → 最近 window_keep 条 / ≤ window_max_chars（原样保留）
  压缩  → 超过 compact_trigger 条时，把更早消息压成一条 summary（幂等）
"""
from __future__ import annotations

from ..prompts.registry import PromptRegistry
from ..settings import as_settings
from .budget import MIN_WINDOW, fit_window

DEFAULT_SYSTEM_PROMPT = """你是一个通用智能体，通过工具帮用户完成任务。
- 简洁直接，不写开场白；要改变系统状态就真正调用工具，而不是只给步骤。
- 遇到错误先分析原因再换方法；同一方法反复失败就停下来如实说明。
- 最终答复给出结论、关键结果与来源。"""

SUMMARY_PROMPT = "把以下对话历史压成要点，保留事实与决定。"


class ContextManager:
    def __init__(self, sessions, settings=None, prompts: PromptRegistry | None = None,
                 memory=None):
        self.sessions = sessions
        self.settings = as_settings(settings)
        self.prompts = prompts or PromptRegistry().add("core.system", DEFAULT_SYSTEM_PROMPT)
        self.memory = memory

    # ------------------------------------------------------------ 提示词
    def system_prompt(self, extra: str = "") -> str:
        return self.prompts.render(extra=extra)

    def memory_block(self, query: str) -> dict:
        """按配置注入召回记忆（默认关闭，保持会话隔离）。"""
        if self.memory is None or not self.settings.get_bool("context.memory_inject", False):
            return {"text": "", "used_chars": 0, "count": 0, "disabled": True}
        budget = self.settings.get_int("context.memory_budget", 600)
        try:
            r = self.memory.inject(query, token_budget=budget)
        except Exception as e:  # noqa: BLE001 —— 记忆失败不阻断主链路
            return {"text": "", "used_chars": 0, "count": 0, "error": str(e)}
        return r

    # ------------------------------------------------------------ 窗口
    def window(self, sid: str) -> list:
        keep = self.settings.get_int("context.window_keep", 10)
        max_chars = self.settings.get_int("context.window_max_chars", 12000)
        msgs = [m for m in self.sessions.context_history(sid)
                if m["role"] in ("user", "assistant", "summary")]
        return fit_window(msgs, keep=keep, max_chars=max_chars, min_window=MIN_WINDOW)

    async def compact_if_needed(self, sid: str, provider) -> dict:
        keep = self.settings.get_int("context.window_keep", 10)
        trigger = self.settings.get_int("context.compact_trigger", 20)
        msgs = self.sessions.context_history(sid)
        if len(msgs) <= trigger:
            return {"compacted": False, "kept": len(msgs)}
        head = msgs[:-keep] if keep > 0 else msgs
        older = [m for m in head if m["role"] in ("user", "assistant")]
        if not older:
            return {"compacted": False, "kept": len(msgs)}
        text = "\n".join(f"{m['role']}: {(m['content'] or '')[:200]}" for m in older)
        summary = await provider.complete(
            [{"role": "system", "content": SUMMARY_PROMPT},
             {"role": "user", "content": text}], kind="compact")
        self.sessions.add(sid, "summary", summary,
                          meta={"summary": True, "covers": len(older)})
        self.sessions.store.mark_compacted([m["id"] for m in older])
        return {"compacted": True, "from": len(msgs), "summarized": len(older),
                "kept": keep, "summary": (summary or "")[:200]}

    # ------------------------------------------------------------ 组装
    async def build(self, sid: str, user_text: str, provider) -> dict:
        comp = await self.compact_if_needed(sid, provider)
        history = self.window(sid)
        mem = self.memory_block(user_text)
        system = self.system_prompt()
        if mem.get("text"):
            system += "\n\n## 相关记忆\n" + mem["text"]
        messages = [{"role": "system", "content": system}]
        for m in history:
            role = "assistant" if m["role"] == "summary" else m["role"]
            messages.append({"role": role, "content": m["content"]})
        # 去重：调用方通常已把本轮用户消息写入会话，历史末条可能就是它
        last = history[-1] if history else None
        if last and last.get("role") == "user":
            prev = str(last.get("content") or "").strip()
            cur = str(user_text or "").strip()
            if prev and cur.startswith(prev):
                messages[-1]["content"] = user_text
            elif prev != cur:
                messages.append({"role": "user", "content": user_text})
        else:
            messages.append({"role": "user", "content": user_text})
        return {
            "messages": messages,
            "injected_memory": mem,
            "compaction": comp,
            "history_len": len(history),
            "preview": {
                "system": (messages[0]["content"] or "")[:20000],
                "window": [{"role": m["role"], "content": (m["content"] or "")[:4000]}
                           for m in messages[1:]],
            },
        }
