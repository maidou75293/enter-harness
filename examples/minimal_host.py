"""最小宿主示例：只用 FakeModel + InMemoryStore，不需要真实模型。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enter_harness import (
    Agent, AgentConfig, FakeModel, InMemoryStore, ModelResponse, RunRequest,
    Scope, ToolCall, ToolRegistry,
)


def main():
    tools = ToolRegistry()
    tools.register("echo", lambda text="": text, "回显文本")
    agent = Agent(AgentConfig(
        scope=Scope("demo", "u1", ["assistant"]),
        model=FakeModel([
            ModelResponse(content="", tool_calls=(
                ToolCall("c1", "echo", {"text": "hello"}),)),
            ModelResponse(content="工具返回 hello"),
            ModelResponse(content="会话收到 hello"),
        ]),
        store=InMemoryStore(),
        tools=tools,
    ))
    result = agent.run(RunRequest(objective="调用 echo 回显 hello"))
    print("run:", result.status, result.answer)
    session = agent.session("s1")
    r2 = session.send("继续")
    print("session:", r2.status, r2.answer)
    agent.close()
    return result


if __name__ == "__main__":
    main()
