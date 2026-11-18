"""Attach authenticated ownership to the SDK's durable invocation envelope."""

from databricks_agentkit import DurableAgentServer
from databricks_agentkit.runtime.runtime import Runtime
from fastapi import HTTPException

from runtime.access import current_access, session_key
from runtime.ui import _session_store, ensure_owned_session


class _OwnedRuntime(Runtime):
    async def submit(self, invocation_id, request, session_id=None):
        access = current_access.get()
        actor = access.viewer.actor
        session_key(session_id)
        data = request["input"]
        if isinstance(data, list):
            data = {"messages": data}
        if not isinstance(data, dict):
            raise HTTPException(422, "Expected an input object or message list.")
        if data.get("actor", actor) != actor:
            raise HTTPException(403, "Invocations must use the caller's identity.")
        if _session_store():
            await ensure_owned_session(access.checkpoint_actor, session_id)
        # This envelope field is outside caller input. accept() atomically compares
        # the whole request on retries, including simultaneous submissions on different workers.
        stamped = {**request, "input": {**data, "actor": actor}, "owner": actor}
        return await super().submit(invocation_id, stamped, session_id=session_id)

    async def get_invocation(self, invocation_id=None, session_id=None):
        access = current_access.get()
        key = access.read_key or invocation_id
        if not key or session_id is not None:
            raise HTTPException(400, "Expected an invocation ID.")
        state = await self.runtime_store.get(key)
        if state is None:
            return None
        owner = state.request.get("owner") if isinstance(state.request, dict) else None
        if not access.viewer.admin and owner != access.viewer.actor:
            return None
        self.executor.ensure_scheduled(state)
        return state

    async def get_events(self, invocation_id=None, session_id=None, after_sequence=None):
        if not await self.get_invocation(invocation_id, session_id):
            raise HTTPException(404, "Invocation not found.")
        key = current_access.get().read_key or invocation_id
        return await super().get_events(key, after_sequence=after_sequence)


def install_runtime_access(app: DurableAgentServer) -> None:
    # Retain the SDK's store and executor, including recovery, fencing and heartbeat.
    runtime = app._runtime
    app._runtime = _OwnedRuntime(
        runtime_store=runtime.runtime_store,
        executor=runtime.executor,
        poll_seconds=runtime.poll_seconds,
    )
