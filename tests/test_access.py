"""Permission checks through the same HTTP routes used by the workbench and API clients."""

from uuid import uuid4

from databricks_agentkit import DurableAgentServer
from databricks_agentkit.runtime.auth import InvocationAuthPolicy
from fastapi.testclient import TestClient

from runtime import access, server, ui
from runtime.sqlite_store import SQLiteRuntimeStore
from tests.test_demo_ui import _FakeStateClient, _client


def _app(path):
    app = DurableAgentServer(runtime_store=SQLiteRuntimeStore(path), auth_policy=InvocationAuthPolicy())
    server.install_runtime_access(app)

    async def echo(payload, context):
        await context.emit({"type": "message", "text": payload["actor"]})
        return {"actor": payload["actor"]}

    app.invoke(echo)
    app.recover(echo)
    ui.install_ui(app)
    return app


def identity(actor):
    return {"X-Forwarded-Email": actor}


def test_durable_result_retry_and_replay_are_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "deployed", lambda: True)
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")
    monkeypatch.setattr(server, "_session_store", lambda: "")
    path = tmp_path / "runtime.db"
    body = {"id": str(uuid4()), "session_id": "conversation", "input": []}
    url = "/api/invocations/" + body["id"]
    with TestClient(_app(path)) as client:
        assert client.post("/api/invocations", json=body).status_code == 401
        answer = client.post("/api/invocations", json=body, headers=identity("alice"))
        assert answer.json()["output"] == {"actor": "alice"}
        for transport in ({}, {"background": True}, {"stream": True}):
            response = client.post("/api/invocations", json={**body, **transport}, headers=identity("bob"))
            assert response.status_code == 409
        for actor in ("bob", "operator"):
            foreign = {**body, "id": str(uuid4()), "input": {"actor": "alice"}}
            assert client.post("/api/invocations", json=foreign, headers=identity(actor)).status_code == 403
        next_body = {**body, "id": str(uuid4()), "background": True, "stream": True}
        assert client.post("/api/invocations", json=next_body, headers=identity("alice")).status_code == 202
        assert "run.completed" in client.get("/api/invocations/" + next_body["id"] + "/events", headers=identity("alice")).text

    with TestClient(_app(path)) as client:
        for suffix in ("", "/events?after=0"):
            assert client.get(url + suffix).status_code == 401
            assert client.get(url + suffix, headers=identity("bob")).status_code == 404
            for actor in ("alice", "operator"):
                response = client.get(url + suffix, headers=identity(actor))
                assert response.status_code == 200 and "alice" in response.text


def test_legacy_results_require_admin_even_if_input_names_a_user(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(access, "deployed", lambda: True)
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")
    path = tmp_path / "runtime.db"
    invocation_id = str(uuid4())

    async def seed():
        store = SQLiteRuntimeStore(path)
        await store.initialize()
        try:
            await store.accept(invocation_id, {"input": {"actor": "alice"}}, "old")
            await store.claim(invocation_id)
            await store.complete(invocation_id, attempt=1, response={"old": True})
        finally:
            await store.close()

    asyncio.run(seed())
    with TestClient(_app(path)) as client:
        url = "/api/invocations/" + invocation_id
        assert client.get(url, headers=identity("alice")).status_code == 404
        assert client.get(url, headers=identity("operator")).json()["output"] == {"old": True}


def test_session_memory_and_pipeline_permissions(monkeypatch):
    client = _client(monkeypatch, configured=True, session_id="shared")
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")

    class State(_FakeStateClient):
        def ensure_session(self, actor, session_id):
            return self.get_session(session_id)  # Existing record after an atomic create conflict.

    monkeypatch.setattr(ui, "_state_client", State)
    monkeypatch.setattr(server, "_session_store", lambda: "sessions")
    for actor, read, write, admin in (("alice", 200, 200, 403), ("bob", 403, 403, 403), ("operator", 200, 403, 200)):
        client.headers.update(identity(actor))
        assert client.get("/api/demo/session").status_code == read
        assert client.post("/api/demo/sessions/shared/open").status_code == read
        assert client.get("/api/demo/session/items").status_code == read
        assert client.post("/api/demo/sessions").status_code == write
        assert client.post("/api/demo/session/items", json={"items": [{"content": "test"}]}).status_code == write
        assert client.get("/api/demo/sessions", params={"actor": "alice"}).status_code == read
        assert client.get("/api/demo/memory/entries", params={"actor": "alice"}).status_code == read
        assert client.post("/api/demo/memory/search", json={"actor": "alice", "query": "test"}).status_code == read
        assert client.post("/api/demo/memory/pipeline/run").status_code == admin


def test_local_login_ignores_forwarded_identity_and_verifies_token(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(access, "deployed", lambda: False)
    monkeypatch.setattr(access, "_workspace_host", lambda: "https://workspace.example")
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")

    async def whoami(self, url, *, headers):
        assert url == "https://workspace.example/api/2.0/preview/scim/v2/Me"
        ok = headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200 if ok else 401, json={"userName": "bob"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", whoami)
    with TestClient(_app(tmp_path / "runtime.db")) as client:
        url = "/api/ui/config?session_id=test"
        assert client.get(url, headers=identity("operator")).status_code == 401
        assert client.get(url, auth=("operator", "wrong")).status_code == 401
        config = client.get(url, auth=("operator", "test-token"), headers=identity("operator")).json()
        assert (config["viewer"], config["role"]) == ("bob", "user")


def test_direct_invocation_checks_managed_session_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "deployed", lambda: True)
    monkeypatch.setattr(server, "_session_store", lambda: "sessions")

    class ExistingSession(_FakeStateClient):
        def ensure_session(self, actor, session_id):
            return {"actor_id": "alice", "session_id": session_id}

    monkeypatch.setattr(ui, "_state_client", ExistingSession)
    with TestClient(_app(tmp_path / "runtime.db")) as client:
        body = {"id": str(uuid4()), "session_id": "existing", "input": []}
        assert client.post("/api/invocations", json=body, headers=identity("bob")).status_code == 403
        assert client.post("/api/invocations", json=body, headers=identity("alice")).status_code == 200


def test_simultaneous_submission_never_shares_results(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(access, "deployed", lambda: True)
    monkeypatch.setattr(server, "_session_store", lambda: "")
    body = {"id": str(uuid4()), "session_id": "concurrent", "input": []}
    with TestClient(_app(tmp_path / "runtime.db")) as client, ThreadPoolExecutor(2) as pool:
        def submit(actor):
            return actor, client.post("/api/invocations", json=body, headers=identity(actor))
        responses = dict(pool.map(submit, ("alice", "bob")))
        assert sorted(r.status_code for r in responses.values()) == [200, 409]
        for actor, result in responses.items():
            read = client.get("/api/invocations/" + body["id"], headers=identity(actor))
            if result.status_code == 200:
                assert read.json()["output"]["actor"] == actor
            else:
                assert read.status_code == 404


def test_admin_can_read_request_user_replay_without_impersonating_for_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
    monkeypatch.setenv("DATABRICKS_APP_URL", "https://test.databricksapps.com")
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")
    monkeypatch.setattr(server, "_session_store", lambda: "")
    app = _app(tmp_path / "obo.db")
    app.auth_policy = InvocationAuthPolicy(user_required=True)
    def gateway(user):
        return {**identity(user), "X-Forwarded-User": f"id-{user}", "X-Forwarded-Access-Token": "test-token"}

    body = {"id": str(uuid4()), "session_id": "public-id", "input": []}
    url = "/api/invocations/" + body["id"]
    with TestClient(app) as client:
        assert client.post("/api/invocations", json=body, headers=gateway("alice")).status_code == 200
        assert client.get(url, headers=gateway("bob")).status_code == 404
        for suffix in ("", "/events"):
            target = url + suffix + "?principal_id=id-alice"
            assert client.get(target, headers=gateway("bob")).status_code == 403
            assert "alice" in client.get(target, headers=gateway("operator")).text


def test_public_session_id_is_scoped_to_the_viewer(monkeypatch, tmp_path):
    import databricks_agentkit.langgraph.session_store as sdk_sessions
    from agent import sessions
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph

    monkeypatch.setattr(access, "deployed", lambda: True)
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")
    monkeypatch.delenv("AGENT_SESSION_STORE", raising=False)
    monkeypatch.setattr(sdk_sessions, "_saver", InMemorySaver())
    builder = StateGraph(MessagesState)
    builder.add_node("answer", lambda _: {"messages": []})
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile(checkpointer=sessions.checkpointer())

    async def conversation(payload, context):
        await graph.ainvoke({"messages": payload["messages"]}, sessions.thread_config(context.session_id, payload["actor"]))
        return {}

    app = DurableAgentServer(runtime_store=SQLiteRuntimeStore(tmp_path / "invocations.db"))
    server.install_runtime_access(app)
    app.invoke(conversation)
    app.recover(conversation)
    ui.install_ui(app)
    with TestClient(app) as client:
        for name in ("alice", "bob"):
            body = {"id": str(uuid4()), "session_id": "shared", "input": {"messages": [{"role": "user", "content": name}]}}
            assert client.post("/api/invocations", json=body, headers=identity(name)).status_code == 200
        url = "/api/demo/session/items?session_id=shared"
        for name in ("alice", "bob"):
            items = client.get(url, headers=identity(name)).json()["session_items"]
            assert [item["data"]["content"] for item in items] == [name]
        assert client.get(url + "&actor=alice", headers=identity("bob")).status_code == 403
        items = client.get(url + "&actor=alice", headers=identity("operator")).json()["session_items"]
        assert [item["data"]["content"] for item in items] == ["alice"]
