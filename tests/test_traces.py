from uuid import uuid4

import mlflow
from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV

from agent.sessions import actor_thread_id
from runtime.traces import configure_local_traces
from tests.test_demo_ui import _client


def test_only_admin_sees_session_traces(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", "local-trace-test")
    # Register environment changes with the fixture before startup sets them.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "")
    configure_local_traces()
    mlflow.tracing.enable()
    session = str(uuid4())
    for sid, name in [
        (actor_thread_id(session, "operator"), "visible-tool"),
        (actor_thread_id(session, "alice"), "private-alice-tool"),
        (str(uuid4()), "other-session-tool"),
    ]:
        with mlflow.start_span(name=name) as span:
            mlflow.update_current_trace(metadata={"mlflow.trace.session": sid})
            span.set_outputs({"text": "<script>probe</script>"})
    mlflow.flush_trace_async_logging()
    client = _client(monkeypatch, session_id=session)
    path = "/api/demo/traces"
    assert client.get("/api/ui/config").json()["tracing"] is None
    assert client.get(path).status_code == 403
    client.headers.pop("x-forwarded-email")
    assert client.get(path).status_code == 401
    client.headers["X-Forwarded-Email"] = "operator"
    assert client.get("/api/ui/config").json()["tracing"]["url"] == path
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    assert "visible-tool" in response.text and "other-session-tool" not in response.text
    assert "private-alice-tool" not in response.text
    assert "private-alice-tool" in client.get(path, params={"actor": "alice"}).text
    assert "<script>probe</script>" not in response.text
    assert "暂无" in client.get(path, params={"session_id": "another"}).text
