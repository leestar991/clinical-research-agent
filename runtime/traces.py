"""Local MLflow recording and an authenticated, read-only view of one session."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from agent.sessions import actor_thread_id
from runtime.access import read_namespace, session_key

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
PAGE_SIZE = 10


def local_trace_db() -> Path:
    return Path(".agentbricks/tracing/mlflow.db").resolve()


def local_tracing() -> bool:
    return os.getenv(RUNTIME_STORE_LOCAL_ENV, "").lower() == "true"


def configure_local_traces() -> None:
    if not local_tracing():
        return
    db = local_trace_db()
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uri = f"sqlite:///{db}"
    os.environ.pop("MLFLOW_TRACING_DESTINATION", None)
    os.environ["MLFLOW_TRACKING_URI"] = uri
    name = os.environ.setdefault("MLFLOW_EXPERIMENT_NAME", Path.cwd().name)
    client = MlflowClient(tracking_uri=uri)
    experiment = client.get_experiment_by_name(name)
    if experiment is None:
        try:
            client.create_experiment(name, artifact_location=str(db.parent / "artifacts"))
        except MlflowException as exc:
            if exc.error_code != "RESOURCE_ALREADY_EXISTS":
                raise
        experiment = client.get_experiment_by_name(name)
    os.environ["MLFLOW_EXPERIMENT_ID"] = experiment.experiment_id


@router.get("/api/demo/traces", include_in_schema=False)
def session_traces(
    request: Request, session_id: str, actor: str | None = None, offset: int = Query(0, ge=0)
):
    request.state.viewer.require_admin()
    if not local_tracing():
        raise HTTPException(404, "Local tracing is unavailable.")
    session_id = session_key(session_id)
    actor = actor or request.state.viewer.actor
    thread, owner = session_id, actor
    if request.app.auth_policy.requires_user:
        auth = read_namespace(request)
        try:
            thread = auth.namespace("session", session_id)
            owner = auth.namespace("actor", actor)
        finally:
            auth.close()
    db = local_trace_db()
    # LangChain autologging sets the MLflow session to LangGraph's thread_id;
    # root-only traces retain the original session ID.
    # Use MLflow's indexed session column with bound values, including for session IDs
    # containing quotation marks. The MLflow client resolves both DB and artifact-backed spans.
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            """SELECT request_id FROM trace_info WHERE experiment_id = ? AND session_id IN (?, ?)
               ORDER BY timestamp_ms DESC, request_id DESC LIMIT ? OFFSET ?""",
            (os.environ["MLFLOW_EXPERIMENT_ID"], thread, actor_thread_id(thread, owner), PAGE_SIZE + 1, offset),
        ).fetchall()
    client = MlflowClient(tracking_uri=f"sqlite:///{db}")
    traces = []
    for (trace_id,) in rows[:PAGE_SIZE]:
        trace = client.get_trace(trace_id, display=False)
        traces.append({
            "id": trace_id,
            "time": datetime.fromtimestamp(trace.info.request_time / 1000, timezone.utc).isoformat(timespec="seconds"),
            "status": trace.info.state.value,
            "duration": trace.info.execution_duration,
            "spans": sorted(trace.data.spans, key=lambda s: s.start_time_ns),
        })
    return templates.TemplateResponse(
        request=request, name="traces.html",
        context={"session_id": session_id, "actor": actor, "traces": traces, "offset": offset,
                 "page_size": PAGE_SIZE, "has_next": len(rows) > PAGE_SIZE},
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"},
    )
