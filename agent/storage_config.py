"""Resolve the project's storage profile for agent code, server startup and the workbench."""

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from databricks_agentkit.runtime.tool_manifest import (
    resolve_memory_store,
    resolve_session_store,
)

Store = Literal["sessions", "memory", "runtime"]


@dataclass(frozen=True)
class StorageConfig:
    mode: str
    sessions: Path
    memory: Path
    runtime: Path

    def uses_sqlite(self, store: Store) -> bool:
        if self.mode != "auto":
            return self.mode == "local"
        if os.getenv(RUNTIME_STORE_LOCAL_ENV, "").lower() != "true":
            return False
        # In auto mode a developer can still opt into one managed store independently.
        if store == "sessions":
            return not resolve_session_store()
        if store == "memory":
            return not resolve_memory_store()
        return True


def load_storage_config(path: Path) -> StorageConfig:
    with path.open("rb") as file:
        config = tomllib.load(file)
    mode = config.get("mode")
    if mode not in ("auto", "local", "cloud"):
        raise ValueError(f"{path}: mode must be auto, local or cloud")
    sqlite = config.get("sqlite", {})
    paths = {}
    for name in ("sessions", "memory", "runtime"):
        value = sqlite.get(name) if isinstance(sqlite, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: sqlite.{name} must be a database file path")
        paths[name] = Path(value).expanduser()
    return StorageConfig(mode=mode, **paths)


@lru_cache(maxsize=1)
def storage_config() -> StorageConfig:
    return load_storage_config(Path(__file__).with_name("storage.toml"))
