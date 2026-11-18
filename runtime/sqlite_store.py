"""SQLite implementation of the AgentKit durable runtime store for agentbricks dev."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from databricks_agentkit.runtime.store import (
    failed_event,
    runtime_store_is_persistent_environment,
)
from databricks_agentkit.runtime.types import (
    Invocation,
    InvocationConflictError,
    InvocationEvent,
    InvocationStatus,
    JsonObject,
    JsonValue,
)

from agent.storage_config import storage_config

# Only the first pending request in an idle session can start. Session-less requests run independently.
_QUEUE_HEAD = """
    status='QUEUED' AND NOT EXISTS (
        SELECT 1 FROM invocations AS prior WHERE prior.session_id=i.session_id
        AND (prior.status='ACTIVE' OR (prior.status='QUEUED' AND prior.session_seq<i.session_seq))
    )
"""
_OWNED = "invocation_id=? AND attempt=? AND worker_id=? AND status='ACTIVE'"
_STALE = f"heartbeat_at < ? AND (status='ACTIVE' OR ({_QUEUE_HEAD}))"


def local_runtime_store() -> SQLiteRuntimeStore | None:
    config = storage_config()
    if config.uses_sqlite("runtime"):
        return SQLiteRuntimeStore(config.runtime)
    if config.mode == "cloud" and not runtime_store_is_persistent_environment():
        raise ValueError(
            "Cloud storage requires the Runtime Store connection supplied by agentbricks deploy. "
            "For agentbricks dev, set mode to auto or local in agent/storage.toml."
        )
    return None


def _state(row: sqlite3.Row) -> Invocation:
    return Invocation(
        invocation_id=row["invocation_id"],
        status=InvocationStatus(row["status"]),
        attempt=row["attempt"],
        session_id=row["session_id"],
        session_sequence_number=row["session_seq"],
        worker_id=row["worker_id"],
        request=json.loads(row["request"]),
        response=json.loads(row["response"]),
    )


def _scope(invocation_id, session_id):
    if (invocation_id is None) == (session_id is None) or invocation_id == "" or session_id == "":
        raise ValueError("provide exactly one non-empty invocation_id or session_id")
    return (
        ("invocation_id", invocation_id)
        if invocation_id is not None
        else ("session_id", session_id)
    )


class SQLiteRuntimeStore:
    def __init__(self, path: Path):
        self.path = path
        self.worker_id = str(uuid4())
        self._lock = asyncio.Lock()
        self._db = None

    async def initialize(self):
        import aiosqlite

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            await self._db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS invocations (
                    invocation_id TEXT PRIMARY KEY, session_id TEXT, session_seq INTEGER,
                    status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT NOT NULL, heartbeat_at REAL NOT NULL,
                    request TEXT NOT NULL, response TEXT NOT NULL DEFAULT 'null',
                    UNIQUE(session_id, session_seq)
                );
                CREATE INDEX IF NOT EXISTS runtime_queue ON invocations(status, heartbeat_at);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, invocation_id TEXT NOT NULL REFERENCES invocations,
                    attempt INTEGER NOT NULL, event TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runtime_replay ON events(invocation_id, seq);
            """)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self._db is not None:
            await self._db.close()
            self._db = None

    @asynccontextmanager
    async def _write(self):
        # The lock protects this connection; BEGIN IMMEDIATE serializes writers across processes.
        async with self._lock:
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise

    async def _read(self, sql, params):
        async with self._lock:
            return await self._db.execute_fetchall(sql, params)

    async def accept(self, invocation_id: str, request: JsonValue, session_id: str | None = None):
        if not invocation_id or session_id == "":
            raise ValueError("invocation_id and supplied session_id must be non-empty")
        async with self._write() as db:
            await db.execute(
                """
                INSERT INTO invocations(invocation_id, request, session_id, session_seq, status,
                                        worker_id, heartbeat_at)
                VALUES (?, ?, ?, CASE WHEN ? IS NOT NULL THEN
                    (SELECT coalesce(max(session_seq),0)+1 FROM invocations WHERE session_id=?) END,
                    'QUEUED', ?, ?) ON CONFLICT(invocation_id) DO NOTHING
            """,
                (
                    invocation_id,
                    json.dumps(request, allow_nan=False),
                    session_id,
                    session_id,
                    session_id,
                    self.worker_id,
                    time.time(),
                ),
            )
            rows = await db.execute_fetchall(
                "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
            )
            state = _state(rows[0])
            if state.session_id != session_id or state.request != request:
                raise InvocationConflictError(invocation_id)
            return state

    async def get(self, invocation_id=None, session_id=None):
        field, value = _scope(invocation_id, session_id)
        sql = f"SELECT * FROM invocations WHERE {field}=?"
        if session_id is not None:
            sql += " AND status IN ('ACTIVE','QUEUED') ORDER BY status, session_seq LIMIT 1"
        rows = await self._read(sql, (value,))
        return _state(rows[0]) if rows else None

    async def queued_invocation_ids(self):
        rows = await self._read(
            f"SELECT invocation_id FROM invocations AS i WHERE worker_id=? AND {_QUEUE_HEAD}",
            (self.worker_id,),
        )
        return [r[0] for r in rows]

    async def stale_invocation_ids(self, stale_seconds):
        rows = await self._read(
            f"SELECT invocation_id FROM invocations AS i WHERE {_STALE}",
            (time.time() - stale_seconds,),
        )
        return [r[0] for r in rows]

    async def claim(self, invocation_id):
        return await self._claim(invocation_id, None)

    async def claim_recoverable(self, invocation_id, stale_seconds):
        return await self._claim(invocation_id, stale_seconds)

    async def _claim(self, invocation_id, stale_seconds):
        if not invocation_id:
            raise ValueError("invocation_id must not be empty")
        if stale_seconds is None:
            predicate, parameter, first_attempt = (
                f"worker_id=? AND {_QUEUE_HEAD}",
                self.worker_id,
                1,
            )
        else:
            predicate, parameter, first_attempt = _STALE, time.time() - stale_seconds, 2
        async with self._write() as db:
            rows = await db.execute_fetchall(
                f"""
                UPDATE invocations AS i SET status='ACTIVE', worker_id=?, heartbeat_at=?,
                    attempt=max(attempt+1, ?) WHERE invocation_id=? AND {predicate} RETURNING *
            """,
                (self.worker_id, time.time(), first_attempt, invocation_id, parameter),
            )
            if not rows:
                return None
            state = _state(rows[0])
            await self._event(db, invocation_id, state.attempt, {"type": "run.started"})
            return state

    async def heartbeat(self, invocation_id, attempt):
        async with self._write() as db:
            cursor = await db.execute(
                f"UPDATE invocations SET heartbeat_at=? WHERE {_OWNED}",
                (time.time(), invocation_id, attempt, self.worker_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    async def _event(db, invocation_id, attempt, event):
        cursor = await db.execute(
            "INSERT INTO events(invocation_id,attempt,event) VALUES (?,?,?)",
            (invocation_id, attempt, json.dumps(event, allow_nan=False)),
        )
        return cursor.lastrowid

    async def append_event(self, invocation_id, attempt, event: JsonObject):
        async with self._write() as db:
            owner = await db.execute_fetchall(
                f"SELECT 1 FROM invocations WHERE {_OWNED}",
                (invocation_id, attempt, self.worker_id),
            )
            if owner:
                return await self._event(db, invocation_id, attempt, event)
            return None

    async def complete(self, invocation_id, attempt, response):
        return await self._finish(
            invocation_id, attempt, "COMPLETED", response, {"type": "run.completed"}
        )

    async def fail(self, invocation_id, attempt, response=None):
        return await self._finish(
            invocation_id, attempt, "FAILED", response, failed_event(response)
        )

    async def _finish(self, invocation_id, attempt, status, response, event):
        async with self._write() as db:
            rows = await db.execute_fetchall(
                f"UPDATE invocations SET status=?, response=? WHERE {_OWNED} RETURNING session_id",
                (
                    status,
                    json.dumps(response, allow_nan=False),
                    invocation_id,
                    attempt,
                    self.worker_id,
                ),
            )
            if not rows:
                return False
            await self._event(db, invocation_id, attempt, event)
            # A request waiting behind a long run gets a fresh lease when it becomes runnable.
            await db.execute(
                f"""
                UPDATE invocations AS i SET heartbeat_at=? WHERE session_id=? AND {_QUEUE_HEAD}
            """,
                (time.time(), rows[0]["session_id"]),
            )
            return True

    async def events(self, invocation_id=None, after_sequence=None, session_id=None):
        field, value = _scope(invocation_id, session_id)
        rows = await self._read(
            f"""
            SELECT e.* FROM events AS e JOIN invocations AS i USING(invocation_id)
            WHERE i.{field}=? AND e.seq>? ORDER BY e.seq
        """,
            (value, after_sequence or 0),
        )
        return [
            InvocationEvent(r["seq"], r["invocation_id"], r["attempt"], json.loads(r["event"]))
            for r in rows
        ]
