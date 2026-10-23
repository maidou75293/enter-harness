#!/usr/bin/env python
"""端到端验收脚本：打真实 HTTP 接口（服务需先起）。

覆盖：health / sessions / tools / skills / mcp / memory(写·混合检索·注入) / chat(端到端任务图)。
"""
import json
import os
import sys
import urllib.parse

import httpx

BASE = os.environ.get("AARCH_BASE", "http://127.0.0.1:8099")


def show(title, obj):
    print("\n" + "=" * 70)
    print(f"### {title}")
    print("=" * 70)
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:3000])


def main():
    c = httpx.Client(base_url=BASE, timeout=180)
    # 鉴权：优先用 AARCH_TOKEN；否则用 AARCH_USER/AARCH_PASS 登录
    tok = os.environ.get("AARCH_TOKEN", "")
    if not tok and os.environ.get("AARCH_PASS"):
        r = c.post("/api/login", json={
            "username": os.environ.get("AARCH_USER", "admin"),
            "password": os.environ["AARCH_PASS"]})
        if r.status_code == 200:
            tok = r.json().get("token", "")
    if tok:
        c.headers["X-Auth"] = tok

    show("1. 健康检查 /api/health", c.get("/api/health").json())

    # 会话
    s = c.post("/api/sessions", json={"title": "验收会话"}).json()
    sid = s["id"]
    show("2. 新建会话", s)
    show("   会话列表", c.get("/api/sessions").json())

    # 工具
    tools = c.get("/api/tools").json()
    show("3. 工具总线 /api/tools", [f"{t['name']}({'danger' if t['danger'] else 'safe'},{t['source']})" for t in tools["tools"]])

    # 技能
    sk = c.get("/api/skills").json()
    show("4. 技能清单 /api/skills", {"skills": sk["skills"], "manifest": sk["manifest"]})
    one = c.get("/api/skills/" + urllib.parse.quote("会议纪要")).json()
    show("   按需加载技能正文（前 200 字）", one["content"][:200])

    # MCP
    show("5. MCP server 列表", c.get("/api/mcp/servers").json())
    sync = c.post("/api/mcp/sync").json()
    show("   MCP 工具同步结果", sync)
    t2 = c.get("/api/tools").json()
    show("   同步后 MCP 工具", [t["name"] for t in t2["tools"] if t["source"] == "mcp"])
    test = c.post("/api/mcp/servers/demo/test").json()
    show("   MCP server 连通性测试", {"ok": test.get("ok"), "tool_count": test.get("tool_count"),
                                  "tools": [x["name"] for x in test.get("tools", [])]})

    # 记忆：写入
    seeds = [
        {"content": "用户偏好：回答先用一句话给结论，再展开细节。", "type": "偏好", "importance": 0.9},
        {"content": "项目 StreamVision 的后端跑在 WSL Ubuntu，端口 8000/8002/8003。", "type": "项目", "importance": 0.7},
        {"content": "用户偏好：所有输出要用中文。", "type": "偏好", "importance": 0.8},
        {"content": "决策：智能体编排用 LangGraph 做图引擎，Planner 自研。", "type": "决策", "importance": 0.6},
    ]
    wrote = [c.post("/api/memory", json=x).json() for x in seeds]
    show("6. 记忆写入（策展）", wrote)

    # 记忆：混合检索
    q = "用户的回答偏好是什么"
    show(f"7. 记忆混合检索（query={q}）", c.post("/api/memory/search", json={"query": q, "top_k": 3}).json())

    # 记忆：注入
    show("8. 记忆注入上下文（带预算）", c.post("/api/memory/inject", json={"query": q, "token_budget": 600}).json())

    # 端到端：一句需求 → 任务图 → 执行 → 结果
    rec = c.post("/api/chat", json={"session_id": sid,
                                    "text": "帮我在工作目录里列个清单，整理一份简短纪要，并记住我偏好中文回答。"}).json()
    show("9. 端到端（假模型）：Planner 任务图", rec["plan"])
    show("   运行消息流（stream）", rec["stream"])
    show("   各节点结果 results", rec["results"])
    show("   校验 verification", rec["verification"])
    show("   最终答复 answer", rec["answer"])

    # 记忆：维护
    show("10. 记忆增量维护", c.post("/api/memory/maintain").json())
    items = c.get("/api/memory").json()
    show("    记忆条目数", len(items["items"]))
    if items["items"]:
        show("    可解释（首条 explain）", c.get(f"/api/memory/{items['items'][0]['id']}/explain").json())

    # 策略 / 审批：危险工具应被拦下
    show("11. Policy：危险工具 shell 应进审批", c.post("/api/tools/shell", json={"args": {"command": "echo hi"}}).json())
    apprv = c.get("/api/approvals").json()
    show("    待审批单", apprv)
    if apprv["approvals"]:
        aid = apprv["approvals"][0]["id"]
        show("    批准", c.post(f"/api/approvals/{aid}", json={"decision": "approved"}).json())

    # 断点/恢复：列出 run 快照 + 从 checkpoint 读状态
    runs = c.get("/api/runs").json()
    show("12. 运行快照（checkpoint 落盘）", runs["runs"][:3])
    if runs["runs"]:
        tid = runs["runs"][0]["thread_id"]
        st = c.get(f"/api/runs/{tid}/state").json()
        show("    从 checkpoint 恢复状态（断点恢复）",
             {"thread_id": st["thread_id"], "next": st["checkpoint"]["next"],
              "results_keys": list(st["checkpoint"]["values"].get("results", {}).keys())})

    print("\n\n全部接口调用完成 ✅")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FAILED:", type(e).__name__, e)
        sys.exit(1)
