"""默认验收端口：确定性规则，不调模型。

criteria 示例：
    {
      "required": ["answer"],          # 结果字典里必须出现的键
      "min_chars": 20,                 # 注入文本最短长度
      "forbid_handoff": true,          # 命中“请手动执行”等转交话术即失败
      "forbidden": ["TODO"],           # 禁止出现的文本
      "schema": {"answer": "str"}      # 简单类型校验
    }
"""
from __future__ import annotations

from typing import Any

from .types import Verdict

HANDOFF_MARKERS = (
    "无法自动完成", "无法自动执行", "不能自动完成", "无法完成",
    "请手动执行", "请手动安装", "请手动操作", "请自行执行", "请自行安装",
    "需要你手动", "需要您手动", "需人工介入", "我无法完成", "我做不到",
    "NOT_INSTALLED", "未能完成",
)

_TYPE_MAP = {
    "str": str, "string": str, "int": int, "integer": int,
    "float": (int, float), "number": (int, float),
    "bool": bool, "boolean": bool, "list": list, "array": list,
    "dict": dict, "object": dict,
}


class RuleVerifyPort:
    def __init__(self, *, handoff_markers=HANDOFF_MARKERS):
        self.handoff_markers = tuple(handoff_markers)

    def check(self, result: Any, criteria: Any = None,
              ctx: Any = None) -> Verdict:
        criteria = dict(criteria or {})
        reasons: list[str] = []

        if isinstance(result, dict):
            for key in criteria.get("required") or ():
                if key not in result or result.get(key) in (None, ""):
                    reasons.append(f"缺少必需字段：{key}")
            self._check_schema(result, criteria.get("schema") or {}, reasons)
            if result.get("ok") is False or result.get("status") in (
                    "error", "denied", "needs_approval"):
                reasons.append(str(result.get("error") or "结果状态不是成功"))

        text = self._text(result)
        min_chars = int(criteria.get("min_chars") or 0)
        if not text.strip():
            reasons.append("结果为空")
        elif min_chars and len(text.strip()) < min_chars:
            reasons.append(f"结果长度 {len(text.strip())} 小于下限 {min_chars}")

        if criteria.get("forbid_handoff", True):
            low = text.lower()
            hit = next((m for m in self.handoff_markers if m.lower() in low), "")
            if hit:
                reasons.append(f"结果包含未完成/转交话术：{hit}")

        for word in criteria.get("forbidden") or ():
            if str(word) and str(word) in text:
                reasons.append(f"结果包含禁止内容：{word}")

        passed = not reasons
        return Verdict(
            passed=passed,
            score=1.0 if passed else 0.0,
            reasons=reasons,
            retryable=bool(criteria.get("retryable", True)) and not passed,
            raw={"criteria": criteria, "status": "rules"},
        )

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _check_schema(result: dict, schema: dict, reasons: list[str]) -> None:
        for key, typ in (schema or {}).items():
            expected = _TYPE_MAP.get(str(typ).lower())
            if expected is None:
                continue
            if key not in result:
                reasons.append(f"schema 缺少字段：{key}")
            elif not isinstance(result[key], expected):
                reasons.append(f"字段 {key} 类型应为 {typ}")

    @staticmethod
    def _text(result: Any) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("answer", "output", "text", "content", "result"):
                if key in result and result[key] is not None:
                    return str(result[key])
            return ""
        return "" if result is None else str(result)
