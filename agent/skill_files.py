"""Immutable file versions scoped to an authenticated actor and internal session."""

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from agent.skill_directory import LocalConfig
from langchain_core.tools import ToolException


def owner_directory(config: LocalConfig, actor: str) -> Path:
    return config.files / hashlib.sha256(actor.encode()).hexdigest()


def session_directory(config: LocalConfig, actor: str, session: str) -> Path:
    return owner_directory(config, actor) / hashlib.sha256(session.encode()).hexdigest()


def publish(directory: Path, name: str, data: bytes) -> str:
    file_id = uuid4().hex
    directory.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            temp.replace(directory / f"{file_id}-{name}")
        finally:
            temp.unlink(missing_ok=True)
    return file_id


def available(directory: Path, retention_days: int):
    cutoff = time.time() - retention_days * 86400
    for path in sorted(directory.glob("*-*")):
        file_id, _, name = path.name.partition("-")
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            continue
        try:
            if path.stat().st_mtime > cutoff:
                yield file_id, name, path
        except FileNotFoundError:
            continue


def lookup(directory: Path, retention_days: int, file_id: str):
    for key, name, path in available(directory, retention_days):
        if key == file_id:
            return name, path
    raise ToolException("File is unavailable in this session.")


def sweep(config: LocalConfig):
    cutoff = time.time() - config.retention_days * 86400
    for path in config.files.glob("*/*/*"):
        try:
            if path.is_file() and os.stat(path).st_mtime <= cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue


async def cleanup_loop(config: LocalConfig):
    while True:
        try:
            await asyncio.to_thread(sweep, config)
        except OSError:
            logging.getLogger(__name__).exception("Local skill file cleanup failed")
        await asyncio.sleep(3600)
