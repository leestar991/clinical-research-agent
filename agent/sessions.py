"""Session checkpoints for the local workbench and the managed AgentKit runtime."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from databricks_agentkit.langgraph import checkpointer as agentkit_checkpointer
from databricks_agentkit.langgraph import read_history
from databricks_agentkit.langgraph import thread_config as agentkit_thread_config
from databricks_agentkit.langgraph.messages import message_items
from agent.storage_config import storage_config
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, uuid6
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import add_messages

if TYPE_CHECKING:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_sqlite_saver: "AsyncSqliteSaver | None" = None


def use_local_sqlite() -> bool:
    return storage_config().uses_sqlite("sessions")


@asynccontextmanager
async def local_sqlite_sessions() -> AsyncIterator[None]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    global _sqlite_saver
    path = storage_config().sessions
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        await saver.conn.execute("""
            CREATE TABLE IF NOT EXISTS workbench_sessions (
                thread_id TEXT PRIMARY KEY, public_id TEXT NOT NULL, created_id TEXT NOT NULL
            )
        """)
        await saver.conn.commit()
        _sqlite_saver = saver
        try:
            yield
        finally:
            _sqlite_saver = None


def checkpointer() -> BaseCheckpointSaver:
    return _sqlite_saver if _sqlite_saver is not None else agentkit_checkpointer()


def thread_config(session_id: str, actor: str) -> RunnableConfig:
    config = agentkit_thread_config(session_id, actor)
    if _sqlite_saver is not None or isinstance(checkpointer(), InMemorySaver):
        # SQLite and in-memory savers index by thread_id alone. Preserve the actor scope of the session.
        config["configurable"]["thread_id"] = json.dumps([actor, session_id], separators=(",", ":"))
    return config


async def register_session(session_id: str, private_id: str, actor: str) -> None:
    thread = thread_config(private_id, actor)["configurable"]["thread_id"]
    await _sqlite_saver.conn.execute(
        "INSERT OR IGNORE INTO workbench_sessions VALUES (?, ?, ?)",
        (thread, session_id, str(uuid6())),
    )
    await _sqlite_saver.conn.commit()


async def has_session(private_id: str, actor: str) -> bool:
    thread = thread_config(private_id, actor)["configurable"]["thread_id"]
    rows = await _sqlite_saver.conn.execute_fetchall(
        """SELECT 1 FROM workbench_sessions WHERE thread_id = ?
           UNION ALL SELECT 1 FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' LIMIT 1""",
        (thread, thread),
    )
    return bool(rows)


async def saved_sessions(actor: str, *, namespaced: bool) -> list[str]:
    # Local thread IDs are compact JSON arrays. Restrict both indexed scans to
    # this actor, including conversations created before the workbench had a directory.
    prefix = json.dumps([actor], separators=(",", ":"))[:-1] + ","
    bounds = (prefix, prefix + "~")
    rows = await _sqlite_saver.conn.execute_fetchall(
        """SELECT thread_id, max(public_id), max(activity_id) AS latest FROM (
               SELECT thread_id, public_id, created_id AS activity_id FROM workbench_sessions
               WHERE thread_id BETWEEN ? AND ?
               UNION ALL
               SELECT thread_id, NULL, max(checkpoint_id) FROM checkpoints
               WHERE thread_id BETWEEN ? AND ? AND checkpoint_ns = '' GROUP BY thread_id
           ) GROUP BY thread_id ORDER BY latest DESC""",
        (*bounds, *bounds),
    )
    # Request-user thread IDs are one-way hashes. Only registered public IDs are navigable.
    return [public_id or json.loads(thread)[1] for thread, public_id, _ in rows
            if public_id or not namespaced]


async def session_history(session_id: str, actor: str) -> dict:
    if _sqlite_saver is None:
        thread_id = thread_config(session_id, actor)["configurable"]["thread_id"]
        history = await read_history(thread_id, actor)
        return {**history, "session_id": session_id}

    saved = await _sqlite_saver.aget_tuple(thread_config(session_id, actor))
    items = []
    interrupts = []
    if saved:
        messages = saved.checkpoint["channel_values"].get("messages", [])
        for _, channel, value in saved.pending_writes or []:
            if channel == "messages":
                messages = add_messages(messages, value)
            elif channel == "__interrupt__":
                interrupts.extend({"id": event.id, "value": event.value} for event in value)
        for index, message in enumerate(messages):
            item_id = str(message.id or index)
            *reasoning, answer = message_items(message)
            items.extend({"item_id": f"{item_id}:reasoning", "data": part} for part in reasoning)
            items.append({"item_id": item_id, "data": answer})
    return {"session_id": session_id, "session_items": items, "interrupts": interrupts}
