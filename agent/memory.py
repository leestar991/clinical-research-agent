"""Actor-scoped long-term memory tools for local development."""

import re
import sqlite3
from contextlib import closing, contextmanager

from databricks_agentkit.langgraph import memory_tools as managed_memory_tools
from agent.storage_config import storage_config
from langchain_core.tools import BaseTool, tool

def use_local_memory() -> bool:
    return storage_config().uses_sqlite("memory")


@contextmanager
def _database():
    path = storage_config().memory
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                actor_id TEXT NOT NULL, path TEXT NOT NULL, session_id TEXT NOT NULL,
                content TEXT NOT NULL, description TEXT, update_time TEXT NOT NULL,
                PRIMARY KEY (actor_id, path, session_id)
            )
        """)
        yield db


def save_entry(actor: str, path: str, content: str, session_id="", description=None, *, append=False):
    """Append a distinct fact or replace a workbench entry in one transaction."""
    key = (actor, path, session_id)
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        if append:
            row = db.execute(
                "SELECT content FROM memories WHERE actor_id=? AND path=? AND session_id=?", key
            ).fetchone()
            if row:
                lines = row["content"].splitlines()
                if content not in lines:
                    lines.append(content)
                content = "\n".join(lines)
        row = db.execute("""
            INSERT INTO memories VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(actor_id,path,session_id) DO UPDATE SET
                content=excluded.content, description=coalesce(excluded.description,memories.description),
                update_time=excluded.update_time
            RETURNING *
        """, (*key, content, description)).fetchone()
        return dict(row)


def _query_terms(query: str) -> set[str]:
    # Chinese text has no word separators. Bigrams also match shorter phrases within a sentence.
    terms = set()
    for token in re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", query.casefold()):
        if len(token) > 1 and "\u3400" <= token[0] <= "\u9fff":
            terms.update(token[i:i + 2] for i in range(len(token) - 1))
        else:
            terms.add(token)
    return terms


def entries(actor: str, *, query=None, path_prefix=None, session_id=None, limit=100):
    with _database() as db:
        sql = "SELECT * FROM memories WHERE actor_id=?"
        params = [actor]
        if path_prefix:
            sql += " AND instr(path, ?) = 1"
            params.append(path_prefix)
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if query is None:
            rows = db.execute(sql + " ORDER BY update_time DESC, path LIMIT ?", (*params, limit))
        else:
            terms = _query_terms(query)

            def rank(path, content, description):
                text = f"{path} {content} {description or ''}".casefold()
                return sum(term in text for term in terms)

            db.create_function("rank_memory", 3, rank, deterministic=True)
            rows = db.execute(
                f"SELECT * FROM ({sql}) WHERE rank_memory(path,content,description)>0 "
                "ORDER BY rank_memory(path,content,description) DESC, update_time DESC, path LIMIT ?",
                (*params, limit),
            )
        return [dict(row) for row in rows]


def _topic_path(topic: str) -> str:
    slug = re.sub(r"[^\w-]+", "-", topic.strip().casefold()).strip("-_")[:64]
    return f"/{slug or 'general'}.md"


def memory_tools(actor: str) -> list[BaseTool]:
    if not use_local_memory():
        return managed_memory_tools(actor)

    @tool
    def remember(fact: str, topic: str) -> str:
        """Save a fact or preference for future conversations. Use a short descriptive topic."""
        fact = " ".join(fact.split())
        if not fact:
            return "Nothing to remember."
        path = _topic_path(topic)
        save_entry(actor, path, fact, append=True)
        return f"Remembered in {path}."

    @tool
    def recall(query: str, session_id: str | None = None) -> str:
        """Recall this user's long-term memories by topic or keywords. Omit session_id to search across conversations."""
        found = entries(actor, query=query, session_id=session_id, limit=5)
        return "\n".join(f"{r['path']}: {r['content']}" for r in found) or "No relevant memories."

    @tool
    def list_memories(session_id: str | None = None) -> str:
        """List the user's stored memory paths and facts, optionally for just one session."""
        found = entries(actor, session_id=session_id)
        return "\n".join(f"{r['path']}: {r['content']}" for r in found) or "No memories yet."

    @tool
    def forget(fact: str, topic: str, session_id: str | None = None) -> str:
        """Forget an exact fact in the user's long-term memory. Supply its topic or stored path."""
        path = topic if topic.startswith("/") else _topic_path(topic)
        target = " ".join(fact.split()).casefold()
        removed = 0
        with _database() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT rowid, session_id, content FROM memories WHERE actor_id=? AND path=?",
                (actor, path),
            ).fetchall()
            for row in rows:
                if session_id and row["session_id"] != session_id:
                    continue
                lines = row["content"].splitlines()
                kept = [line for line in lines if " ".join(line.split()).casefold() != target]
                if len(kept) == len(lines):
                    continue
                removed += len(lines) - len(kept)
                if kept:
                    db.execute(
                        "UPDATE memories SET content=?, update_time=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE rowid=?",
                        ("\n".join(kept), row["rowid"]),
                    )
                else:
                    db.execute("DELETE FROM memories WHERE rowid=?", (row["rowid"],))
        return f"Forgot {removed} fact(s)."

    return [remember, recall, forget, list_memories]
