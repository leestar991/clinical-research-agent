"""Read and pin complete developer-installed skill packages."""

import hashlib
import os
import re
import shutil
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml
from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from databricks_agentkit.runtime.tool_manifest import project_root
from langchain_core.tools import ToolException


def local_skills_enabled() -> bool:
    return os.getenv(RUNTIME_STORE_LOCAL_ENV, "").lower() == "true"


@dataclass(frozen=True)
class LocalConfig:
    directory: Path
    image: str
    timeout: int
    files: Path
    retention_days: int

    @classmethod
    def load(cls):
        root = project_root()
        with (root / "agent" / "skills.toml").open("rb") as stream:
            config = tomllib.load(stream)
        local, files = config["local"], config["session_files"]
        if min(local["command_timeout_seconds"], files["retention_days"]) <= 0:
            raise ValueError("Skill timeouts and retention must be positive")
        return cls(root / local["directory"], local["image"], local["command_timeout_seconds"],
                   root / files["directory"], files["retention_days"])


def relative_path(value: str) -> Path:
    path = Path(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ToolException("Expected a relative path inside the skill or work directory.")
    return path


def package_path(root: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise ToolException("Invalid skill name.")
    directory = root / name
    if directory.is_symlink() or not directory.is_dir():
        raise ToolException("Unknown local skill.")
    return directory


def discover(root: Path) -> list[dict[str, str]]:
    skills = []
    for directory in sorted(root.iterdir()):
        if directory.name.startswith(".") or not directory.is_dir() or directory.is_symlink():
            continue
        manifest = package_path(root, directory.name) / "SKILL.md"
        if not manifest.is_file() or manifest.is_symlink():
            continue
        lines = manifest.read_text("utf-8").splitlines()
        if not lines or lines[0] != "---" or "---" not in lines[1:]:
            raise ToolException(f"Missing SKILL.md front matter in {directory.name}.")
        meta = yaml.safe_load("\n".join(lines[1:lines.index("---", 1)]))
        if not isinstance(meta, dict) or meta.get("name") != directory.name or not isinstance(meta.get("description"), str):
            raise ToolException(f"Invalid skill metadata in {directory.name}.")
        skills.append({"name": directory.name, "description": meta["description"]})
    return skills


def snapshot(root: Path, name: str, target: Path) -> str:
    source = package_path(root, name)
    digest = hashlib.sha256()
    target.mkdir(mode=0o755)
    try:
        for entry in sorted(source.rglob("*")):
            path = entry.relative_to(source)
            if "__pycache__" in path.parts or entry.name == ".DS_Store" or entry.suffix == ".pyc":
                continue
            mode = entry.lstat().st_mode
            dest = target / path
            if stat.S_ISDIR(mode):
                dest.mkdir(mode=0o755)
            elif stat.S_ISREG(mode):
                data = entry.read_bytes()
                dest.write_bytes(data)
                dest.chmod(0o555 if mode & 0o111 else 0o444)
                digest.update(str(path).encode() + b"\0" + hashlib.sha256(data).digest())
            else:
                raise ToolException("A skill package may contain only regular files and directories.")
        if not (target / "SKILL.md").is_file():
            raise ToolException("Skill package is missing SKILL.md.")
        return digest.hexdigest()
    except BaseException:
        shutil.rmtree(target)
        raise
