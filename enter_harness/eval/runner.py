"""任务级评测（eval）：对 Agent 跑一组用例并统计通过率。

一次评测（eval）由若干用例（case）组成。每个用例给出目标（objective）、
期望状态（expect_status）以及回答中必须出现的内容（expect_contains）；
工厂函数（factory）为每个用例新建一个 Agent，执行后汇总为报告（report）。

离线优先：评测核心不访问网络。命令行入口使用 FakeModel（脚本化假模型）
与 InMemoryStore（内存存储）自测，因此没有模型凭证也能运行。

超时说明：当工厂返回的 Agent 支持 ``start()``（返回 RunHandle 运行句柄）时，
使用 ``RunHandle.wait(timeout)`` 控制超时；超时记为失败并尝试取消运行。
若返回的对象只有同步 ``run()``，则直接同步执行、不强制超时（由被测对象
自行控制），此时 ``timeout`` 参数不生效。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..domain import BudgetProfile, RunRequest

__all__ = ["EvalCase", "EvalResult", "EvalReport", "load_cases", "run_cases", "main"]


@dataclass(frozen=True)
class EvalCase:
    """单条评测用例。

    字段：
    - ``name``：用例名（报告中的唯一标识）；
    - ``objective``：交给 Agent 的任务目标；
    - ``expect_contains``：回答中必须出现的全部片段；
    - ``expect_status``：期望的运行状态，默认 ``"ok"``；
    - ``max_steps``：本轮运行允许的最大步数，映射到 BudgetProfile。
    """

    name: str
    objective: str
    expect_contains: tuple[str, ...] = ()
    expect_status: str = "ok"
    max_steps: int = 4


@dataclass
class EvalResult:
    """单条用例的执行结果。"""

    name: str
    ok: bool
    status: str = ""
    answer: str = ""
    error: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        """转成可直接 JSON 序列化的字典。"""
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class EvalReport:
    """一次评测的汇总报告。"""

    results: list[EvalResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        """通过用例数。"""
        return sum(1 for item in self.results if item.ok)

    @property
    def total(self) -> int:
        """用例总数。"""
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        """通过率（0.0～1.0）；没有用例时为 0.0。"""
        if not self.total:
            return 0.0
        return self.passed / self.total

    def to_dict(self) -> dict:
        """转成可直接 JSON 序列化的字典。"""
        return {
            "results": [item.to_dict() for item in self.results],
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
        }


def _case_from_dict(data: dict) -> EvalCase:
    """把一条 JSON 对象解析为 EvalCase；字段不合法时抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("用例必须是 JSON 对象")
    name = str(data.get("name") or "").strip()
    objective = str(data.get("objective") or "").strip()
    if not name:
        raise ValueError("缺少 name")
    if not objective:
        raise ValueError(f"用例 {name!r} 缺少 objective")
    raw_contains = data.get("expect_contains", ())
    if isinstance(raw_contains, str):
        raw_contains = (raw_contains,)
    if not isinstance(raw_contains, (list, tuple)) or not all(
            isinstance(x, str) for x in raw_contains):
        raise ValueError(f"用例 {name!r} 的 expect_contains 必须是字符串列表")
    status = str(data.get("expect_status") or "ok")
    try:
        max_steps = int(data.get("max_steps", 4))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"用例 {name!r} 的 max_steps 必须是整数") from exc
    if max_steps <= 0:
        raise ValueError(f"用例 {name!r} 的 max_steps 必须大于 0")
    return EvalCase(
        name=name,
        objective=objective,
        expect_contains=tuple(raw_contains),
        expect_status=status,
        max_steps=max_steps,
    )


def load_cases(path: str | Path) -> list[EvalCase]:
    """从 JSON 文件读取用例列表。

    文件内容必须是一个 JSON 数组（列表），每个元素包含 ``name``、
    ``objective``，可选 ``expect_contains`` / ``expect_status`` / ``max_steps``。
    解析失败时抛出 ValueError 并带上序号，便于定位坏用例。
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ValueError(f"用例文件 {p} 的顶层必须是 JSON 数组")
    cases: list[EvalCase] = []
    for index, item in enumerate(raw, start=1):
        try:
            cases.append(_case_from_dict(item))
        except ValueError as exc:
            raise ValueError(f"第 {index} 条用例无效：{exc}") from exc
    return cases


def _answer_text(answer: Any) -> str:
    """把 Agent 的回答统一转成字符串（None 视为空串）。"""
    if answer is None:
        return ""
    return answer if isinstance(answer, str) else str(answer)


def _error_text(error: Any) -> str:
    """把 RunResult.error（ErrorInfo）转成可读文本。"""
    if error is None:
        return ""
    code = str(getattr(error, "code", "") or "")
    message = str(getattr(error, "message", "") or error)
    return f"{code}: {message}" if code else message


def _execute(agent: Any, req: RunRequest, timeout: float) -> Any:
    """执行一次运行并返回 RunResult。

    优先使用 ``start()`` / ``RunHandle.wait(timeout)`` 取得超时控制；
    只有同步 ``run()`` 的对象则直接执行（不支持超时）。
    """
    start = getattr(agent, "start", None)
    if callable(start):
        handle = start(req)
        wait = getattr(handle, "wait", None)
        if not callable(wait):
            raise TypeError("Agent.start() 返回的对象缺少 wait(timeout) 方法")
        try:
            return wait(timeout)
        except TimeoutError:
            # 超时后尽力取消后台运行；取消失败的异常不影响评测报告。
            cancel = getattr(handle, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except Exception:  # noqa: BLE001 —— 取消失败不覆盖超时结论
                    pass
            raise
    run = getattr(agent, "run", None)
    if not callable(run):
        raise TypeError("agent_factory 返回的对象既没有 start() 也没有 run()")
    # 同步降级：没有 RunHandle，因此不在这里强制超时。
    return run(req)


def run_cases(cases: Sequence[EvalCase],
              agent_factory: Callable[[EvalCase], Any],
              *, timeout: float = 30.0) -> EvalReport:
    """执行一组评测用例并返回汇总报告。

    参数：
    - ``cases``：用例序列；
    - ``agent_factory``：工厂函数，签名为 ``factory(case) -> Agent``；
      每个用例都会调用一次，保证用例之间互不共享 Agent 状态；
    - ``timeout``：单条用例的超时秒数；仅对支持 ``start()`` 的 Agent 生效，
      超时来自 ``RunHandle.wait(timeout)`` 抛出的 TimeoutError。

    判定规则：运行状态等于 ``expect_status``，且回答包含全部
    ``expect_contains`` 片段，才算通过。工厂或运行抛出异常时记为失败，
    并保留异常类型与信息；此类失败在结果中标记 ``status="exception"``，
    超时则标记 ``status="timeout"``（不是 Agent 的真实运行状态）。
    """
    results: list[EvalResult] = []
    effective_timeout = max(0.0, float(timeout))
    for case in cases:
        started = time.perf_counter()
        status = ""
        answer = ""
        error = ""
        ok = False
        try:
            agent = agent_factory(case)
            if agent is None:
                raise TypeError("agent_factory 返回了 None")
            req = RunRequest(
                objective=case.objective,
                budget=BudgetProfile(max_steps=int(case.max_steps)),
            )
            try:
                result = _execute(agent, req, effective_timeout)
            except TimeoutError:
                status = "timeout"
                error = f"运行超时（超过 {effective_timeout:g} 秒）"
            else:
                status = str(getattr(result, "status", "") or "")
                answer = _answer_text(getattr(result, "answer", None))
                run_error = _error_text(getattr(result, "error", None))
                if status != case.expect_status:
                    error = (f"状态不匹配：期望 {case.expect_status!r}，"
                             f"实际 {status!r}")
                    if run_error:
                        error += f"；运行错误：{run_error}"
                else:
                    missing = [needle for needle in case.expect_contains
                               if needle not in answer]
                    if missing:
                        preview = answer[:200]
                        error = (f"回答缺少期望内容：{missing}；"
                                 f"实际回答：{preview!r}")
                    else:
                        ok = True
        except Exception as exc:  # noqa: BLE001 —— 评测必须继续跑完其余用例
            status = status or "exception"
            error = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started) * 1000.0
        results.append(EvalResult(
            name=case.name,
            ok=ok,
            status=status,
            answer=answer,
            error=error,
            duration_ms=round(duration_ms, 3),
        ))
    return EvalReport(results=results)


# ---------------------------------------------------------------- 离线 CLI
# 以下内容只用于命令行自测：用 FakeModel + InMemoryStore 复现一个最小宿主，
# 不读取任何网络配置，也不依赖真实模型。

_OFFLINE_ANSWERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("1+1", "计算"), "计算结果是 2。"),
    (("问好", "你好", "greeting"), "你好！这是离线评测的回答。"),
    (("状态", "status"), "当前状态：就绪。"),
)


def _offline_answer(text: str, fallback: str = "") -> str:
    """按提示中的关键词生成固定的离线回答。"""
    blob = text or ""
    for keywords, answer in _OFFLINE_ANSWERS:
        if any(keyword in blob for keyword in keywords):
            return answer
    return f"已完成：{fallback or blob}"


def _offline_agent_factory(case: EvalCase) -> Any:
    """为离线 CLI 新建 Agent（FakeModel + InMemoryStore，不连真实模型）。"""
    # 延迟导入，避免评测核心在只需要 run_cases 时加载示例适配器。
    from ..adapters.memory import FakeModel, InMemoryStore
    from ..api import Agent, AgentConfig
    from ..domain import ModelResponse, Scope
    from ..tooling import ToolRegistry

    def handler(messages, tools, budget):
        # 离线夹具：按用例目标返回预置回答，保证不连真实模型也可复现。
        return ModelResponse(content=_offline_answer(case.objective))

    return Agent(AgentConfig(
        scope=Scope(tenant_id="eval", agent_id="eval-agent"),
        model=FakeModel(handler=handler),
        store=InMemoryStore(),
        tools=ToolRegistry(),
    ))


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：加载用例、离线执行、打印 JSON 报告。

    退出码：0 表示全部通过；1 表示存在失败用例（或没有用例）；
    2 表示用例文件读取/解析失败。
    """
    default_cases = Path(__file__).with_name("cases.json")
    parser = argparse.ArgumentParser(
        description="任务级评测离线自测（FakeModel + InMemoryStore，不连网络）")
    parser.add_argument("--cases", default=str(default_cases),
                        help="用例 JSON 文件路径（默认：包内 cases.json）")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="单条用例超时秒数（默认：30）")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    report = run_cases(cases, _offline_agent_factory, timeout=args.timeout)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.total > 0 and report.passed == report.total else 1


if __name__ == "__main__":  # pragma: no cover —— 命令行入口
    raise SystemExit(main())
