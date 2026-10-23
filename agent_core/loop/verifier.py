"""Verifier：校验结果 + 触发重规划。

两层：
- rules（默认）：确定性规则，不调模型
- llm：规则通过后，再由模型判断是否满足用户需求
- off：不校验
"""
from __future__ import annotations

from ..llm.openai_compat import tool_result

VERDICT_TOOL = {"type": "function", "function": {
    "name": "submit_verdict",
    "description": "提交校验结论",
    "parameters": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "结果是否满足用户需求"},
            "feedback": {"type": "string", "description": "不通过时给出具体建议"},
        },
        "required": ["ok"],
    },
}}

DEFAULT_VERIFY_SYSTEM = """你是结果校验器。判断任务图的执行结果是否满足用户需求，用 `submit_verdict` 提交结论。
节点输出可能被截断，不要因为“看起来没写完”而判不通过；核心内容已回答即 ok=true。
若最终答复只是建议/步骤/请用户手动执行，或明确未完成，判 ok=false。"""

HANDOFF_MARKERS = ("无法自动完成", "无法自动执行", "不能自动完成", "无法完成",
                   "请手动执行", "请手动安装", "请手动操作", "请自行执行", "请自行安装",
                   "需要你手动", "需要您手动", "需人工介入", "我无法完成", "我做不到",
                   "NOT_INSTALLED", "未能完成")


class Verifier:
    def __init__(self, provider, system_prompt: str | None = None,
                 verdict_tool: dict | None = None):
        self.provider = provider
        self.system_prompt = system_prompt or DEFAULT_VERIFY_SYSTEM
        self.verdict_tool = verdict_tool or VERDICT_TOOL

    def _rule_check(self, plan: dict, results: dict) -> list:
        problems = []
        for k, v in results.items():
            s = str(v if v is not None else "").strip()
            if not s:
                problems.append(f"节点 {k} 输出为空")
            elif s.startswith("[") and "失败" in s[:12]:
                problems.append(f"节点 {k} 执行失败：{s[:120]}")
        final_id = plan.get("final_node")
        final_txt = str(results.get(final_id) or "")
        if final_id and not final_txt.strip():
            problems.append(f"最终节点 {final_id} 无输出")
        if final_id:
            low = final_txt.lower()
            hit = next((m for m in HANDOFF_MARKERS if m.lower() in low), None)
            if hit:
                problems.append(
                    f"最终答复是未完成/转交说明（命中『{hit}』），不算满足需求，请换可行方法继续执行")
        return problems

    async def verify(self, requirement: str, plan: dict, results: dict,
                     mode: str = "rules") -> dict:
        if mode == "off":
            return {"ok": True, "feedback": "", "raw": "off"}
        problems = self._rule_check(plan, results)
        if problems:
            return {"ok": False, "feedback": "；".join(problems), "raw": "rules"}
        if mode != "llm":
            return {"ok": True, "feedback": "", "raw": "rules"}

        body = "\n".join(f"[{k}] {str(v)[:1500]}" for k, v in results.items())
        msgs = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content":
             f"用户需求：{requirement}\n\n最终节点：{plan.get('final_node')}\n\n"
             f"各节点输出：\n{body}"},
        ]
        msg = await self.provider.chat(
            msgs, tools=[self.verdict_tool],
            tool_choice={"type": "function",
                         "function": {"name": self.verdict_tool["function"]["name"]}})
        data = tool_result(msg, self.verdict_tool["function"]["name"])
        if not data:
            return {"ok": True, "feedback": "", "raw": str(msg)[:300]}
        return {"ok": bool(data.get("ok", True)),
                "feedback": data.get("feedback", ""), "raw": str(msg)[:300]}
