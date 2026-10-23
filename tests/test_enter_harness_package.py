"""独立包约束：不依赖 app、只用允许的库、公开 API 可导入。"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "enter_harness"
ALLOWED_THIRD_PARTY = {
    "fastapi", "uvicorn", "langgraph", "langchain_core", "langchain",
    "mcp", "httpx", "pydantic", "multipart", "pypdf", "psycopg",
}


def _py_files():
    return sorted(CORE.rglob("*.py"))


class PackageBoundaryTests(unittest.TestCase):
    def test_no_app_imports(self):
        bad = []
        for fp in _py_files():
            text = fp.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith(("import app", "from app")) or \
                        s.startswith(("import app.", "from app.")):
                    bad.append(f"{fp.relative_to(ROOT)}:{i}: {s}")
        self.assertEqual(bad, [])

    def test_only_allowed_dependencies(self):
        bad = []
        stdlib = set(sys.stdlib_module_names)
        for fp in _py_files():
            tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        top = a.name.split(".")[0]
                        if top not in stdlib and top not in ALLOWED_THIRD_PARTY \
                                and top != "enter_harness":
                            bad.append(f"{fp.name}: import {a.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    top = (node.module or "").split(".")[0]
                    if top and top not in stdlib and top not in ALLOWED_THIRD_PARTY \
                            and top != "enter_harness":
                        bad.append(f"{fp.name}: from {node.module}")
        self.assertEqual(bad, [])

    def test_import_and_public_api(self):
        import enter_harness
        from enter_harness._version import __version__
        self.assertEqual(enter_harness.__version__, __version__)
        for name in ("Agent", "AgentService", "ToolRegistry", "RulePolicy",
                     "HookManager", "Tracer", "ContextManager", "PromptRegistry",
                     "SessionManager", "SkillLibrary", "SubagentRunner",
                     "Memory", "OpenAICompatModel", "Provider", "Settings",
                     "Store", "Policy", "ToolSpec", "ToolContext",
                     "PolicyDecision"):
            self.assertTrue(hasattr(enter_harness, name), name)

    def test_lazy_langgraph_exports(self):
        import enter_harness
        self.assertTrue(callable(enter_harness.build_react_subgraph))
        # 单一公开入口：Orchestrator / 旧异步 AgentLoop 均为内部阶段，不再对外导出
        self.assertFalse(hasattr(enter_harness, "Orchestrator"))
        self.assertFalse(hasattr(enter_harness, "AgentLoop"))
        # 内部模块仍可直接导入（SubagentRunner 与旧单测使用）
        from enter_harness.loop import AgentLoop  # noqa: F401
        # 顶层 RunResult 必须是 v1.1 的领域对象，而不是旧 loop 的同名结果
        from enter_harness.domain import RunResult as DomainRunResult
        self.assertIs(enter_harness.RunResult, DomainRunResult)

    def test_kernel_modules_do_not_import_business_names(self):
        """core 内不应出现业务模块名（租户/计费/审批单/API）。"""
        banned = ("from ..tenants", "from .tenants", "import billing",
                  "from ..billing", "from .billing")
        for fp in _py_files():
            text = fp.read_text(encoding="utf-8")
            for b in banned:
                self.assertNotIn(b, text, fp.name)


if __name__ == "__main__":
    unittest.main()
