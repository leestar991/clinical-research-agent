"""Session checkpoints for the local workbench and the managed AgentKit runtime."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from databricks_agentkit.langgraph import checkpointer as agentkit_checkpointer
from databricks_agentkit.langgraph import read_history
from databricks_agentkit.langgraph import thread_config as agentkit_thread_config
from databricks_agentkit.langgraph.messages import message_items
from agent.storage_config import storage_config
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.message import add_messages

_sqlite_saver: BaseCheckpointSaver | None = None


def use_local_sqlite() -> bool:
    return storage_config().uses_sqlite("sessions")


@asynccontextmanager
async def local_sqlite_sessions() -> AsyncIterator[None]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    global _sqlite_saver
    path = storage_config().sessions
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        _sqlite_saver = saver
        try:
            yield
        finally:
            _sqlite_saver = None


def checkpointer() -> BaseCheckpointSaver:
    return _sqlite_saver if _sqlite_saver is not None else agentkit_checkpointer()


def thread_config(session_id: str, actor: str) -> RunnableConfig:
    config = agentkit_thread_config(session_id, actor)
    if _sqlite_saver is not None:
        # SQLite indexes by thread_id alone. Preserve the actor scope of the session.
        config["configurable"]["thread_id"] = json.dumps([actor, session_id], separators=(",", ":"))
    return config


async def session_history(session_id: str, actor: str) -> dict:
    if _sqlite_saver is None:
        return await read_history(session_id, actor)

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
