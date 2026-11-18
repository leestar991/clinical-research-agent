from agent import sessions
from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt


def test_sqlite_is_only_the_local_dev_default(monkeypatch):
    monkeypatch.delenv("AGENT_SESSION_STORE", raising=False)
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV, raising=False)
    assert not sessions.use_local_sqlite()
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    assert sessions.use_local_sqlite()
    monkeypatch.setenv("AGENT_SESSION_STORE", "managed-sessions")
    assert not sessions.use_local_sqlite()


async def test_sqlite_restores_conversation_and_pending_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    builder = StateGraph(MessagesState)

    def review(state):
        decision = interrupt("Review this message")
        return {"messages": [("assistant", decision)]}

    builder.add_node("review", review)
    builder.add_edge(START, "review")
    builder.add_edge("review", END)

    async with sessions.local_sqlite_sessions():
        graph = builder.compile(checkpointer=sessions.checkpointer())
        await graph.ainvoke({"messages": [("user", "remember this")]}, sessions.thread_config("s", "alice"))

    async with sessions.local_sqlite_sessions():
        history = await sessions.session_history("s", "alice")
        assert history["session_items"][0]["data"]["content"] == "remember this"
        assert history["interrupts"][0]["value"] == "Review this message"
        assert (await sessions.session_history("s", "bob"))["session_items"] == []
        graph = builder.compile(checkpointer=sessions.checkpointer())
        result = await graph.ainvoke(Command(resume="approved"), sessions.thread_config("s", "alice"))
        assert [msg.content for msg in result["messages"]] == ["remember this", "approved"]
