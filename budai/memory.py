import asyncio
import json
import math
import re

import httpx

from .errors import redact


CATEGORIES = ("working", "episodic", "semantic", "procedural", "observation", "error", "correction")


def words(text):
    return list(dict.fromkeys(re.findall(r"[^\W_]{3,}", text.casefold())))[:24]


def cosine(left, right):
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    scale = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / scale if scale else 0.0


class Memory:
    def __init__(self, store, ollama, embedding_model, errors):
        self.store = store
        self.ollama = ollama
        self.model = embedding_model
        self.errors = errors

    def record(self, account, content, category="semantic", source="user", confidence=1.0,
               conversation=None, corrects=None):
        with self.store.connect() as db:
            if corrects:
                row = db.execute("UPDATE memories SET superseded=1 WHERE id=? AND user_id=? AND superseded=0",
                                 (corrects, account))
                if row.rowcount != 1:
                    raise ValueError("The memory to correct is missing or already superseded")
            return db.execute(
                "INSERT INTO memories(user_id,content,category,source,confidence,conversation,corrects,created) "
                "VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (account, redact(content)[:30000], category, source, confidence, conversation, corrects),
            ).lastrowid

    def event(self, account, content, category, source, conversation=None):
        return self.record(account, content, category, source, 0.8 if category == "episodic" else 1, conversation)

    def browse(self, account, *, query="", category=None, offset=0, limit=75, active=False):
        terms = words(query)
        cond = "m.user_id=?"
        args = [account]
        ranking = "m.id DESC"
        if terms:
            cond += " AND m.id IN (SELECT rowid FROM memory_index WHERE memory_index MATCH ?)"
            args.append(" OR ".join(f'"{term}"' for term in terms))
        if category:
            cond += " AND m.category=?"
            args.append(category)
        if active:
            cond += " AND m.superseded=0"
        return self.store.all(
            f"SELECT m.id,m.content,m.category,m.confidence,m.source,m.created,m.conversation,"
            f"m.corrects,m.superseded FROM memories m WHERE {cond} ORDER BY {ranking} LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )

    async def index(self, account, limit=20):
        if not self.model:
            raise ValueError("Set BUDAI_EMBEDDING_MODEL to a locally installed Ollama embedding model")
        pending = self.store.all(
            "SELECT id,content FROM memories WHERE user_id=? AND superseded=0 "
            "AND (embedding_model IS NULL OR embedding_model<>?) ORDER BY id DESC LIMIT ?", (account, self.model, limit),
        )
        for row in pending:
            vector = await self.ollama.embed(self.model, row["content"])
            if not vector or not all(math.isfinite(value) for value in vector):
                raise ValueError("Ollama returned an invalid embedding")
            self.store.run("UPDATE memories SET vector=?,embedding_model=? WHERE id=? AND user_id=?",
                           (json.dumps(vector), self.model, row["id"], account))
        return {"indexed": len(pending), "model": self.model}

    async def relevant(self, account, query, limit, current=None, *, lock=None):
        if not limit:
            return []
        terms = words(query)
        if not terms:
            return []
        phrase = " OR ".join(f'"{term}"' for term in terms)
        candidates = self.store.all(
            "SELECT m.id,m.content,m.category,m.confidence,m.source,m.created,m.conversation "
            "FROM memory_index i JOIN memories m ON m.id=i.rowid WHERE memory_index MATCH ? "
            "AND m.user_id=? AND m.superseded=0 AND (m.conversation IS NULL OR m.conversation<>?) "
            "ORDER BY i.rank LIMIT ?",
            (phrase, account, current or -1, limit),
        )
        if self.model:
            vectors = self.store.all(
                "SELECT id,content,category,confidence,source,created,conversation,vector FROM memories "
                "WHERE user_id=? AND superseded=0 AND embedding_model=? AND (conversation IS NULL OR conversation<>?) "
                "ORDER BY id DESC LIMIT 1500", (account, self.model, current or -1),
            )
            if vectors:
                try:
                    if lock:
                        async with lock:
                            vector = await self.ollama.embed(self.model, query)
                    else:
                        vector = await self.ollama.embed(self.model, query)
                    rank = await asyncio.to_thread(
                        sorted, vectors, key=lambda item: cosine(vector, json.loads(item["vector"])), reverse=True
                    )
                    keywords = {row["id"] for row in candidates}
                    semantic = [row for row in rank[:limit] if row["id"] not in keywords]
                    for row in semantic:
                        del row["vector"]
                    candidates = candidates[: max(1, limit // 2)] + semantic + candidates[max(1, limit // 2):]
                except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as error:
                    self.errors.capture(account, "memory embedding", error)
        return [{**row, "content": row["content"][:1200]} for row in candidates[:limit]]
