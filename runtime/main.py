"""Run the LangGraph agent with the optional Agent Bricks chat app."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from agent.agent import configure
from agent.sessions import local_sqlite_sessions, use_local_sqlite
from dotenv import load_dotenv
from runtime.access import deployed
from runtime.adapter import invoke, recover
from runtime.server import install_runtime_access
from runtime.sqlite_store import local_runtime_store
from runtime.traces import configure_local_traces
from runtime.ui import install_ui

from databricks_agentkit import DurableAgentServer

# override=False so injected DATABRICKS_* (from `agentbricks dev -p` or the deploy platform) win over a
# checked-in .env.
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
configure_local_traces()
configure()

runtime_store = local_runtime_store()
app = DurableAgentServer(runtime_store=runtime_store)
install_runtime_access(app)
if use_local_sqlite():
    runtime_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def local_lifespan(server):
        async with local_sqlite_sessions(), runtime_lifespan(server) as state:
            yield state

    app.router.lifespan_context = local_lifespan

app.invoke(invoke)
app.recover(recover)
install_ui(app, local_runtime=runtime_store is not None)


def main() -> None:
    uvicorn.run(
        "runtime.main:app",
        host="0.0.0.0" if deployed() else "127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=False,
    )
