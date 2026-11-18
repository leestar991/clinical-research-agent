import asyncio

import pytest
from databricks_agentkit.runtime.durability.store import DurableRuntimeStore
from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from databricks_agentkit.runtime.types import InvocationConflictError, InvocationStatus

from runtime.sqlite_store import SQLiteRuntimeStore, local_runtime_store


def test_only_local_dev_selects_sqlite(monkeypatch):
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV, raising=False)
    assert local_runtime_store() is None
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    assert isinstance(local_runtime_store(), DurableRuntimeStore)


async def test_persisted_result_and_cursor(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(path)
    await store.initialize()
    try:
        await store.accept("a", {"hello": "世界"}, "s")
        await store.accept("b", {}, "s")
        assert (await store.get(session_id="s")).invocation_id == "a"
        assert await store.claim("b") is None
        assert await store.queued_invocation_ids() == ["a"]
        assert (await store.claim("a")).attempt == 1
        cursor = await store.append_event("a", 1, {"type": "delta", "text": "您好"})
        assert await store.complete("a", 1, {"answer": "您好"})
        assert await store.claim("b")
        assert await store.fail("b", 1, {"error": {"message": "test"}})
        events = await store.events("a")
    finally:
        await store.close()

    reopened = SQLiteRuntimeStore(path)
    await reopened.initialize()
    try:
        state = await reopened.accept("a", {"hello": "世界"}, "s")
        assert state.status == InvocationStatus.COMPLETED
        assert state.response == {"answer": "您好"}
        assert await reopened.events("a") == events
        assert await reopened.events("a", after_sequence=cursor) == events[-1:]
        assert [e.event["type"] for e in await reopened.events(session_id="s")] == [
            "run.started",
            "delta",
            "run.completed",
            "run.started",
            "run.failed",
        ]
        with pytest.raises(InvocationConflictError):
            await reopened.accept("a", {}, "s")
        assert await reopened.get(session_id="s") is None
    finally:
        await reopened.close()


async def test_recovery_fences_old_attempt_and_concurrent_workers(tmp_path):
    first, second = (SQLiteRuntimeStore(tmp_path / "runtime.db") for _ in range(2))
    await first.initialize()
    await second.initialize()
    try:
        await asyncio.gather(first.accept("a", {}, "s"), second.accept("a", {}, "s"))
        owners = await asyncio.gather(first.claim("a"), second.claim("a"))
        assert sum(state is not None for state in owners) == 1
        old, new = (first, second) if owners[0] else (second, first)
        assert await old.heartbeat("a", 1)
        assert await new.claim_recoverable("a", stale_seconds=60) is None
        await new.accept("b", {}, "s")
        assert await new.stale_invocation_ids(0) == ["a"]
        assert (await new.claim_recoverable("a", stale_seconds=0)).attempt == 2
        assert not await old.heartbeat("a", 1)
        assert not await old.complete("a", 1, {})
        assert await old.append_event("a", 1, {}) is None
        assert await new.complete("a", 2, "ok")
        assert await old.claim("b") is None
        assert (await old.claim_recoverable("b", 0)).attempt == 2
    finally:
        await first.close()
        await second.close()
