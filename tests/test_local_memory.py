from concurrent.futures import ThreadPoolExecutor

from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV

from agent import memory
from tests.test_demo_ui import _client


def test_memory_backend_selection(monkeypatch):
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV, raising=False)
    monkeypatch.delenv("AGENT_MEMORY_STORE", raising=False)
    assert not memory.use_local_memory()
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    assert memory.use_local_memory()
    monkeypatch.setenv("AGENT_MEMORY_STORE", "managed-id")
    assert not memory.use_local_memory()
    monkeypatch.setattr(memory, "managed_memory_tools", lambda actor: [actor])
    assert memory.memory_tools("alice") == ["alice"]


def test_workbench_and_tools_share_actor_memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    client = _client(monkeypatch)
    client.headers["x-forwarded-email"] = "alice"
    assert client.get("/api/ui/config").json()["memory"]["backend"] == "sqlite"
    tools = {tool.name: tool for tool in memory.memory_tools("alice")}
    create = client.post("/api/demo/memory/entries", json={"path": "/试验", "content": "随访代号为蓝鸟"})
    assert create.status_code == 200
    assert "蓝鸟" in tools["recall"].invoke({"query": "请问随访代号是什么"})
    # A fresh graph for a separate session builds a fresh set of actor-scoped tools.
    bob = {tool.name: tool for tool in memory.memory_tools("bob")}
    assert "蓝鸟" not in bob["list_memories"].invoke({})
    assert client.get("/api/demo/memory/entries", params={"actor": "bob"}).status_code == 403
    client.params = {"session_id": "new-session"}
    tools["remember"].invoke({"topic": "preferences", "fact": "Prefers blue."})
    rows = client.post("/api/demo/memory/search", json={"query": "blue"}).json()
    assert rows["managed_memory_entries"][0]["content"] == "Prefers blue."
    assert client.get("/api/demo/memory/pipeline").status_code == 503
    tools["forget"].invoke({"topic": "/试验", "fact": "随访代号为蓝鸟"})
    assert tools["recall"].invoke({"query": "蓝鸟"}) == "No relevant memories."


def test_concurrent_remember_preserves_distinct_facts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    facts = ["first", "second", "first", "third"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda fact: memory.save_entry("alice", "/topic.md", fact, append=True), facts))
    assert sorted(memory.entries("alice")[0]["content"].splitlines()) == ["first", "second", "third"]
