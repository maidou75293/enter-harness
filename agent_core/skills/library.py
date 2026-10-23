"""技能库。

目录约定：
    skills/<skill-name>/SKILL.md      正文（可带 --- frontmatter: name/description）
    skills/<skill-name>/scripts/*     可选脚本

注入策略：system prompt 只放清单（name + description），正文用 load_skill 按需加载。
扫描顺序：私有目录（private_dir）优先 → 全局目录（skills_dir），同名以私有为准。
core 不读业务配置：两个目录都由调用方传入。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
from pathlib import Path
from typing import Callable


def default_sources() -> dict:
    """可「一键导入」的外部技能来源（label -> Path）。可用环境变量覆盖。"""
    out = {}
    env = os.environ.get("AGENT_SKILL_SOURCES", "")
    if env:
        for i, p in enumerate(env.split(":")):
            pp = Path(p)
            if pp.exists():
                out[pp.name or f"源{i+1}"] = pp
        return out
    for p in sorted(glob.glob(str(Path.home() /
                               ".nvm/versions/node/*/lib/node_modules/openclaw/skills")), reverse=True):
        out["官方技能"] = Path(p)
        break
    if not out:
        g = (glob.glob("/usr/*/lib/node_modules/openclaw/skills")
             + glob.glob("/usr/local/*/lib/node_modules/openclaw/skills"))
        if g:
            out["官方技能"] = Path(g[0])
    ws = Path.home() / ".openclaw/agents/main/agent/workshop-skills"
    if ws.exists():
        out["我的技能"] = ws
    return out


class SkillLibrary:
    def __init__(self, skills_dir: str | Path | None = None,
                 private_dir: Callable[[], Path] | None = None):
        self.dir = Path(skills_dir) if skills_dir else None
        self._private_dir = private_dir

    # ---------------- 目录 ----------------
    def tenant_dir(self) -> Path:
        """私有技能目录（调用方注入；未注入时退回全局目录）。"""
        if self._private_dir is not None:
            try:
                d = Path(self._private_dir())
                d.mkdir(parents=True, exist_ok=True)
                return d
            except Exception:  # noqa: BLE001
                pass
        if self.dir is not None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            return self.dir
        return Path.cwd() / "skills"

    def _dirs(self) -> list:
        out = []
        td = self.tenant_dir()
        if self.dir is None or td != self.dir:
            out.append(td)
        if self.dir is not None and self.dir.exists():
            out.append(self.dir)
        return out

    # ---------------- 扫描 ----------------
    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        meta = {}
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            text = text[m.end():]
        return meta, text

    def list(self) -> list:
        out, seen = [], set()
        for base in self._dirs():
            if not base.exists():
                continue
            for d in sorted(base.iterdir()):
                if not d.is_dir() or d.name in seen:
                    continue
                f = d / "SKILL.md"
                if not f.exists():
                    continue
                try:
                    meta, body = self._parse_frontmatter(f.read_text(encoding="utf-8"))
                except OSError:
                    continue
                seen.add(d.name)
                out.append({
                    "name": meta.get("name", d.name),
                    "dir": d.name,
                    "description": meta.get("description") or
                    (body.strip().splitlines()[0] if body.strip() else ""),
                    "has_scripts": (d / "scripts").exists(),
                    "origin": self._origin(d),
                    "scope": "global" if self.dir is not None and base == self.dir else "private",
                })
        return out

    @staticmethod
    def _origin(d) -> str:
        m = d / ".origin"
        try:
            return m.read_text(encoding="utf-8").strip() if m.exists() else ""
        except OSError:
            return ""

    def manifest(self) -> str:
        items = self.list()
        if not items:
            return "(暂无技能)"
        return "\n".join(f"- {i['name']}：{i['description']}" for i in items)

    def load(self, name: str) -> str:
        for base in self._dirs():
            if not base.exists():
                continue
            for d in base.iterdir():
                f = d / "SKILL.md"
                if d.is_dir() and f.exists():
                    try:
                        meta, body = self._parse_frontmatter(f.read_text(encoding="utf-8"))
                    except OSError:
                        continue
                    if meta.get("name", d.name) == name or d.name == name:
                        return body.strip()
        return f"未找到技能：{name}"

    # ---------------- 工具适配 ----------------
    def tool_load(self, name: str):
        return self.load(name)

    # ---------------- 对话中自建技能 ----------------
    def create(self, name: str, description: str = "", body: str = "") -> dict:
        name = (name or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return {"ok": False, "error": "非法技能名"}
        d = self.tenant_dir() / name
        f = d / "SKILL.md"
        if f.exists():
            return {"ok": False, "error": f"技能已存在：{name}"}
        d.mkdir(parents=True, exist_ok=True)
        fm = f"---\nname: {name}\ndescription: {description or name}\n---\n\n"
        f.write_text(fm + (body or "").strip() + "\n", encoding="utf-8")
        (d / ".origin").write_text("self", encoding="utf-8")
        return {"ok": True, "created": name, "path": str(f)}

    def tool_create(self, name: str, description: str = "", body: str = ""):
        r = self.create(name, description, body)
        if r.get("ok"):
            return f"已创建技能「{name}」（下次对话即可被 load_skill 加载）"
        return f"创建失败：{r.get('error')}"

    # ---------------- 一键导入外部技能 ----------------
    def sources(self) -> list:
        return [{"label": k, "dir": str(v)} for k, v in default_sources().items()]

    def available(self) -> list:
        imported = {i["dir"] for i in self.list()}
        out = []
        for label, sdir in default_sources().items():
            if not sdir.exists():
                continue
            for d in sorted(sdir.iterdir()):
                f = d / "SKILL.md"
                if not (d.is_dir() and f.exists()):
                    continue
                try:
                    meta, body = self._parse_frontmatter(f.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                desc = meta.get("description") or (
                    body.strip().splitlines()[0] if body.strip() else "")
                out.append({"name": meta.get("name", d.name), "dir": d.name,
                            "source": label, "description": desc[:200],
                            "imported": d.name in imported})
        return out

    def import_one(self, dirname: str, source: str = "") -> dict:
        if not dirname or "/" in dirname or "\\" in dirname or ".." in dirname:
            return {"ok": False, "error": "非法技能名"}
        targets = list(default_sources().items())
        if source:
            targets = [(k, v) for k, v in targets if k == source]
        for label, sdir in targets:
            src = sdir / dirname
            if src.is_dir() and (src / "SKILL.md").exists():
                dst = self.tenant_dir() / dirname
                if dst.exists():
                    return {"ok": True, "skipped": True, "message": f"{dirname} 已存在，跳过"}
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
                (dst / ".origin").write_text("external", encoding="utf-8")
                return {"ok": True, "imported": dirname, "source": label}
        return {"ok": False, "error": "未找到该技能"}
