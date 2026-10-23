# modules: enter_harness.eval.runner enter_harness.api enter_harness.adapters.memory
"""任务级评测（eval）模块单元测试。

全部使用 FakeModel（脚本化假模型）与 InMemoryStore（内存存储），
不访问网络，也不读取真实模型凭证。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from enter_harness.adapters.memory import FakeModel, InMemoryStore
from enter_harness.api import Agent, AgentConfig
from enter_harness.domain import ModelResponse, Scope
from enter_harness.eval import EvalCase, EvalReport, EvalResult, load_cases, run_cases
from enter_harness.tooling import ToolRegistry


def _agent_factory(handler):
    """构造工厂：每个用例新建一个 Agent（FakeModel + InMemoryStore）。"""

    def factory(case):
        return Agent(AgentConfig(
            scope=Scope(tenant_id="t-eval", agent_id="a-eval"),
            model=FakeModel(handler=handler),
            store=InMemoryStore(),
            tools=ToolRegistry(),
        ))

    return factory


def _keyword_handler(messages, tools, budget):
    """按目标关键词返回固定回答；messages 中任意位置命中即可。"""
    blob = "\n".join(str(getattr(m, "content", "")) for m in messages)
    if "问好" in blob or "你好" in blob:
        return ModelResponse(content="你好，很高兴见到你")
    if "1+1" in blob or "计算" in blob:
        return ModelResponse(content="计算结果是 2")
    return ModelResponse(content="这是无关回答")


class RunCasesTests(unittest.TestCase):
    def test_all_cases_pass(self):
        cases = [
            EvalCase("greeting", "请向用户问好", ("你好",)),
            EvalCase("math", "计算 1+1", ("2", "结果是")),
        ]
        report = run_cases(cases, _agent_factory(_keyword_handler), timeout=5)

        self.assertIsInstance(report, EvalReport)
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 2)
        self.assertEqual(report.pass_rate, 1.0)
        self.assertTrue(all(item.ok for item in report.results))
        self.assertTrue(all(item.status == "ok" for item in report.results))
        self.assertEqual(report.results[1].answer, "计算结果是 2")
        self.assertTrue(all(item.duration_ms >= 0 for item in report.results))

        data = report.to_dict()
        self.assertEqual(set(data), {"results", "passed", "total", "pass_rate"})
        self.assertEqual(data["passed"], 2)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["pass_rate"], 1.0)
        self.assertEqual(len(data["results"]), 2)
        self.assertIn("duration_ms", data["results"][0])

    def test_expect_contains_mismatch_marks_failure(self):
        cases = [EvalCase("mismatch", "任意任务", ("绝不该出现",))]
        report = run_cases(cases, _agent_factory(_keyword_handler), timeout=5)

        self.assertEqual(report.total, 1)
        self.assertEqual(report.passed, 0)
        self.assertEqual(report.pass_rate, 0.0)
        bad = report.results[0]
        self.assertIsInstance(bad, EvalResult)
        self.assertFalse(bad.ok)
        # 运行状态本身正常，失败原因是回答内容不匹配
        self.assertEqual(bad.status, "ok")
        self.assertIn("绝不该出现", bad.error)
        self.assertIn("缺少期望内容", bad.error)

    def test_exception_marks_failure(self):
        def broken_factory(case):
            raise RuntimeError("工厂故障")

        report = run_cases([EvalCase("boom", "任意任务")], broken_factory,
                           timeout=5)
        bad = report.results[0]
        self.assertFalse(bad.ok)
        self.assertEqual(bad.status, "exception")
        self.assertIn("RuntimeError", bad.error)
        self.assertIn("工厂故障", bad.error)

        # 运行期异常（同步 run 降级路径）同样记为失败并保留信息
        class BrokenAgent:
            def run(self, req):
                raise ValueError("运行故障")

        report2 = run_cases([EvalCase("boom2", "任意任务")],
                            lambda case: BrokenAgent(), timeout=5)
        bad2 = report2.results[0]
        self.assertFalse(bad2.ok)
        self.assertEqual(bad2.status, "exception")
        self.assertIn("ValueError", bad2.error)
        self.assertIn("运行故障", bad2.error)

    def test_timeout_marks_failure(self):
        def slow_handler(messages, tools, budget):
            time.sleep(0.5)
            return ModelResponse(content="太晚了")

        started = time.perf_counter()
        report = run_cases([EvalCase("slow", "慢任务", ("太晚",))],
                           _agent_factory(slow_handler), timeout=0.05)
        elapsed = time.perf_counter() - started

        bad = report.results[0]
        self.assertFalse(bad.ok)
        self.assertEqual(bad.status, "timeout")
        self.assertIn("超时", bad.error)
        # 不应该等满 0.5 秒
        self.assertLess(elapsed, 0.45)


class LoadCasesTests(unittest.TestCase):
    def test_load_cases_parses_json(self):
        payload = [
            {"name": "one", "objective": "任务一", "expect_contains": ["甲"],
             "expect_status": "ok", "max_steps": 3},
            {"name": "two", "objective": "任务二"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(payload, ensure_ascii=False),
                            encoding="utf-8")
            cases = load_cases(path)

        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0], EvalCase("one", "任务一", ("甲",), "ok", 3))
        self.assertIsInstance(cases[0].expect_contains, tuple)
        self.assertEqual(cases[1].expect_contains, ())
        self.assertEqual(cases[1].expect_status, "ok")
        self.assertEqual(cases[1].max_steps, 4)

    def test_builtin_cases_file(self):
        path = Path(__file__).resolve().parent.parent / \
            "enter_harness" / "eval" / "cases.json"
        cases = load_cases(path)
        self.assertEqual(len(cases), 3)
        self.assertTrue(all(case.objective for case in cases))
        self.assertTrue(all(case.expect_contains for case in cases))

    def test_invalid_cases_file_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"name": "不是数组"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
