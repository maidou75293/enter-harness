"""长期记忆：结构化条目 + 混合检索 + 写入策展 + 增量维护。

条目结构：
    {id, type, content, entities, source, confidence, importance,
     created, last_used, links, embedding, tier, status, superseded_by, use_count}

四环节：写入（去重/合并/冲突消解）→ 检索（向量 + 关键词）→ 注入（预算裁剪）→ 维护。
持久化只需要 Store 提供：
    all_memory / get_memory / upsert_memory / delete_memory / add_memory_event
    get_config / get_int / get_float
"""
from __future__ import annotations

import re
import time
import uuid

from ..llm import embedding as emb

TIERS = ["L0", "L1", "L2", "L3"]
_TIER_TRUST = {"L0": 0.4, "L1": 0.6, "L2": 0.85, "L3": 0.7}


class Memory:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider

    # ---------------- 向量 ----------------
    def embed(self, text: str) -> list:
        backend = self.store.get_config("memory.embed_backend", "local")
        if backend == "openai" and self.provider is not None and hasattr(self.provider, "embed"):
            try:
                return self.provider.embed([text])[0]
            except Exception:  # noqa: BLE001 —— 真模型不可用时退回本地，不阻断主链路
                pass
        return emb.local_embed(text, self.store.get_int("memory.embed_dim", 256))

    # ---------------- 写入（策展） ----------------
    def write(self, content: str, type: str = "事实", source: str = "user",
              confidence: float = 0.6, importance: float = 0.5, tier: str = "L1",
              entities: list | None = None) -> dict:
        content = (content or "").strip()
        if not content:
            return {"ok": False, "error": "内容为空"}
        vec = self.embed(content)
        now = time.time()
        item = {
            "id": "m_" + uuid.uuid4().hex[:10],
            "type": type, "content": content,
            "entities": entities or _guess_entities(content),
            "source": source, "confidence": confidence, "importance": importance,
            "created": now, "last_used": 0, "links": [], "embedding": vec,
            "tier": tier, "status": "active", "superseded_by": None, "use_count": 0,
        }
        dup_th = self.store.get_float("memory.dedup_threshold", 0.92)
        best, best_sim = None, 0.0
        for old in self.store.all_memory(status="active"):
            sim = emb.cosine(vec, old.get("embedding") or [])
            if sim > best_sim:
                best, best_sim = old, sim

        decision = "new"
        if best is not None and best_sim >= dup_th:
            merged = best
            if len(content) > len(best["content"]):
                merged["content"] = content
                merged["embedding"] = vec
            merged["use_count"] = int(best.get("use_count", 0)) + 1
            merged["importance"] = max(float(best.get("importance", 0.5)), importance)
            merged["confidence"] = max(float(best.get("confidence", 0.6)), confidence)
            merged["last_used"] = now
            self.store.upsert_memory(merged)
            self.store.add_memory_event(best["id"], "merge",
                                        f"命中重复(相似度 {best_sim:.2f})，合并")
            return {"ok": True, "action": "merge", "id": best["id"],
                    "similarity": round(best_sim, 3)}
        if best is not None and best_sim >= 0.75 and best.get("type") == type:
            new_wins = (confidence, now) >= (float(best.get("confidence", 0.6)),
                                             best.get("created", 0))
            if new_wins:
                item["links"] = [best["id"]]
                best["status"] = "superseded"
                best["superseded_by"] = item["id"]
                self.store.upsert_memory(best)
                self.store.add_memory_event(
                    best["id"], "supersede",
                    f"被 {item['id']} 取代（相似 {best_sim:.2f}，新置信度更高）")
                decision = "supersede"
            else:
                item["status"] = "superseded"
                item["superseded_by"] = best["id"]
                self.store.add_memory_event(
                    item["id"], "rejected",
                    f"与 {best['id']} 冲突且置信度不占优，未采纳")
                self.store.upsert_memory(item)
                return {"ok": True, "action": "superseded", "id": item["id"]}

        self.store.upsert_memory(item)
        self.store.add_memory_event(item["id"], "write",
                                    f"写入({item['type']}, tier={tier}, conf={confidence})")
        return {"ok": True, "action": decision, "id": item["id"], "item": item}

    # ---------------- 检索（混合） ----------------
    def search(self, query: str, top_k: int = 5, type_filter: str | None = None) -> dict:
        q_vec = self.embed(query)
        q_tokens = set(emb.tokenize(query))
        results = []
        for item in self.store.all_memory(status="active"):
            if type_filter and item.get("type") != type_filter:
                continue
            cos = emb.cosine(q_vec, item.get("embedding") or [])
            kw = _keyword_score(q_tokens, item.get("content", ""))
            score = 0.65 * cos + 0.35 * kw
            results.append({
                "id": item["id"], "type": item["type"], "content": item["content"],
                "tier": item["tier"], "importance": item["importance"],
                "vector": round(cos, 3), "keyword": round(kw, 3),
                "score": round(score, 3),
                "why": f"向量{cos:.2f}×0.65 + 关键词{kw:.2f}×0.35",
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        top = [r for r in results if r["score"] > 0.0][:top_k]
        for r in top:
            it = self.store.get_memory(r["id"])
            if it:
                it["use_count"] = int(it.get("use_count", 0)) + 1
                it["last_used"] = time.time()
                self.store.upsert_memory(it)
            self.store.add_memory_event(r["id"], "recall", r["why"])
        return {"query": query, "count": len(top), "results": top, "total": len(results)}

    def inject(self, query: str, token_budget: int = 600) -> dict:
        hits = self.search(query, top_k=self.store.get_int("memory.recall_top_k", 5))["results"]
        now = time.time()
        scored = []
        for h in hits:
            it = self.store.get_memory(h["id"]) or {}
            recency = 1.0 / (1.0 + (now - (it.get("last_used") or it.get("created", now))) / 86400)
            rank = h["score"] * (0.5 + float(it.get("importance", 0.5))) * (0.5 + recency)
            scored.append((rank, h))
        scored.sort(key=lambda x: x[0], reverse=True)
        lines, used = [], 0
        for _rank, h in scored:
            line = f"- [{h['type']}/{h['tier']}] {h['content']}"
            if used + len(line) > token_budget:
                break
            lines.append(line)
            used += len(line)
        return {"text": "\n".join(lines), "used_chars": used, "count": len(lines)}

    # ---------------- 维护（增量） ----------------
    def maintain(self) -> dict:
        now = time.time()
        promoted, archived, decayed = [], [], []
        for it in self.store.all_memory(status="active"):
            age_days = (now - float(it.get("created", now))) / 86400
            last = float(it.get("last_used") or it.get("created", now))
            idle_days = (now - last) / 86400
            uc = int(it.get("use_count", 0))
            changed = False
            if it["tier"] == "L1" and uc >= 3:
                it["tier"] = "L2"
                promoted.append(it["id"])
                self.store.add_memory_event(it["id"], "promote", f"召回 {uc} 次 → L2")
                changed = True
            if it["tier"] == "L1" and idle_days > 30 and uc == 0:
                it["status"] = "archived"
                archived.append(it["id"])
                self.store.add_memory_event(it["id"], "archive", f"闲置 {idle_days:.0f} 天，归档")
                changed = True
            if not changed:
                decay = max(0.1, float(it["importance"]) * (1 - min(0.5, age_days / 3650)))
                if abs(decay - float(it["importance"])) > 1e-6:
                    it["importance"] = round(decay, 4)
                    decayed.append(it["id"])
            self.store.upsert_memory(it)
        return {"promoted": promoted, "archived": archived, "decayed": len(decayed)}

    # ---------------- 可解释 ----------------
    def explain(self, mid: str) -> dict:
        it = self.store.get_memory(mid)
        if not it:
            return {"ok": False, "error": "未找到"}
        return {"ok": True, "item": it, "events": self.store.memory_events(mid)}

    # ---------------- 工具适配 ----------------
    def tool_write(self, content: str, type: str = "事实", importance: float = 0.5,
                   source: str = "agent"):
        r = self.write(content, type=type, source=source, importance=importance)
        return f"记忆写入：{r.get('action')} id={r.get('id')}"

    def tool_search(self, query: str, top_k: int = 5):
        r = self.search(query, top_k=top_k)
        if not r["results"]:
            return "无命中"
        return "\n".join(f"[{h['score']}] {h['content']}" for h in r["results"])


def _keyword_score(q_tokens: set, text: str) -> float:
    if not q_tokens:
        return 0.0
    t = set(emb.tokenize(text))
    if not t:
        return 0.0
    hit = len(q_tokens & t)
    if hit == 0:
        return 0.0
    return (hit / len(q_tokens)) * (1.0 / (1.0 + abs(len(t) - len(q_tokens)) / 50.0))


def _guess_entities(text: str) -> list:
    return list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text)))[:8]
