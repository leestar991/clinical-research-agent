import pytest
from databricks_agentkit.runtime.store import (
    RUNTIME_STORE_DATABASE_ENV,
    RUNTIME_STORE_LAKEBASE_BRANCH_ENV,
    RUNTIME_STORE_LOCAL_ENV,
    RUNTIME_STORE_USERNAME_ENV,
)

from agent import memory, sessions
from agent import storage_config as config_module
from runtime import ui
from runtime.sqlite_store import local_runtime_store


@pytest.fixture
def profile(tmp_path, monkeypatch):
    config_path = tmp_path / "storage.toml"
    loader = config_module.load_storage_config
    monkeypatch.setattr(config_module, "load_storage_config", lambda _: loader(config_path))
    config_module.storage_config.cache_clear()

    def write(mode):
        config_path.write_text(
            f'mode = "{mode}"\n[sqlite]\n'
            f'sessions = "{tmp_path}/state/sessions.db"\n'
            f'memory = "{tmp_path}/state/memory.db"\n'
            f'runtime = "{tmp_path}/state/runtime.db"\n'
        )
        config_module.storage_config.cache_clear()

    yield write
    config_module.storage_config.cache_clear()


async def test_local_profile_uses_all_configured_files(profile, tmp_path, monkeypatch):
    profile("local")
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV, raising=False)
    monkeypatch.setenv("AGENT_SESSION_STORE", "managed-session")
    monkeypatch.setenv("AGENT_MEMORY_STORE", "managed-memory")
    assert sessions.use_local_sqlite() and memory.use_local_memory()
    assert not ui._memory_store() and not ui._session_store()
    async with sessions.local_sqlite_sessions():
        await sessions.checkpointer().aget_tuple(sessions.thread_config("s", "alice"))
    memory.save_entry("alice", "/fact", "remember")
    store = local_runtime_store()
    await store.initialize()
    await store.close()
    assert all(
        (tmp_path / "state" / name).is_file() for name in ("sessions.db", "memory.db", "runtime.db")
    )


def test_cloud_profile_keeps_sdk_bindings(profile, monkeypatch):
    profile("cloud")
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    assert not sessions.use_local_sqlite() and not memory.use_local_memory()
    with pytest.raises(ValueError, match="Cloud storage requires"):
        local_runtime_store()
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV)
    monkeypatch.setenv("AGENT_SESSION_STORE", "managed-session")
    monkeypatch.setenv("AGENT_MEMORY_STORE", "managed-memory")
    monkeypatch.setenv(RUNTIME_STORE_LAKEBASE_BRANCH_ENV, "projects/p/branches/b")
    monkeypatch.setenv(RUNTIME_STORE_DATABASE_ENV, "runtime-db")
    monkeypatch.setenv(RUNTIME_STORE_USERNAME_ENV, "app-sp")
    assert ui._session_store() == "managed-session" and ui._memory_store() == "managed-memory"
    assert local_runtime_store() is None  # DurableAgentServer selects its SDK backend.


def test_invalid_profile_fails_early(profile):
    profile("cluod")
    with pytest.raises(ValueError, match="mode must be"):
        config_module.storage_config()
