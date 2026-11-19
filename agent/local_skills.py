"""Model-facing tools for local directory skills. Invocation state stays outside checkpoints."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from langchain_core.tools import ToolException, tool

from agent import skill_files
from agent.local_sandbox import LocalSandbox
from agent.skill_directory import LocalConfig, discover, local_skills_enabled, relative_path, snapshot


@dataclass
class SkillRun:
    sandbox: LocalSandbox
    actor: str
    session: str
    loaded: dict[str, str] = field(default_factory=dict)

    @property
    def directory(self):
        return skill_files.session_directory(self.sandbox.config, self.actor, self.session)

    def package(self, name):
        if name not in self.loaded:
            raise ToolException("Call load_skill to read SKILL.md before using this package.")
        return self.sandbox.staging / "skills" / name


_current: ContextVar[SkillRun] = ContextVar("local_skill_invocation")


@asynccontextmanager
async def skill_invocation(actor: str, session: str):
    if not local_skills_enabled():
        yield
        return
    sandbox = LocalSandbox(LocalConfig.load())
    token = _current.set(SkillRun(sandbox, actor, session))
    try:
        yield
    finally:
        try:
            _current.reset(token)
        finally:
            await sandbox.close()


@tool
def list_skills() -> list[dict]:
    """Discover installed local skills and the requests that each one handles."""
    return discover(_current.get().sandbox.config.directory)


@tool
async def load_skill(name: str) -> dict:
    """Load the complete SKILL.md and pin this package for the current invocation."""
    run = _current.get()
    async with run.sandbox.lock:
        if name not in run.loaded:
            target = run.sandbox.staging / "skills" / name
            run.loaded[name] = snapshot(run.sandbox.config.directory, name, target)
        package = run.package(name)
        return {"name": name, "sha256": run.loaded[name], "instructions": (package / "SKILL.md").read_text(),
                "files": sorted(str(p.relative_to(package)) for p in package.rglob("*") if p.is_file())}


@tool
def read_skill_file(name: str, path: str) -> str:
    """Read a text reference or script by its package-relative path after load_skill."""
    entry = _current.get().package(name) / relative_path(path)
    if not entry.is_file() or entry.stat().st_size > 256 * 1024:
        raise ToolException("The requested text file is unavailable or too large.")
    return entry.read_text("utf-8")


@tool
async def run_skill(name: str, entry: str, argv: list[str], save_files: list[str] | None = None) -> dict:
    """Run a bundled .py or .sh CLI with literal argv in /work. Save named /work-relative output files for later turns and return download URLs."""
    run = _current.get()
    async with run.sandbox.lock:
        run.package(name)
        paths = [relative_path(path) for path in save_files or []]
        try:
            result = await run.sandbox.run(name, entry, argv)
        except TimeoutError:
            raise ToolException("Skill command timed out; its container was removed.") from None
        result["files"] = []
        for path in paths:
            try:
                data = await run.sandbox.read_output(str(path))
                file_id = skill_files.publish(run.directory, path.name, data)
            except (ToolException, OSError) as exc:
                result.setdefault("file_errors", {})[str(path)] = str(exc)
                continue
            result["files"].append({"id": file_id, "name": path.name,
                                    "url": f"/api/skill-files/{run.directory.name}/{file_id}"})
        return result


@tool
def list_session_files() -> list[dict]:
    """List saved skill output files in this user's current session, including earlier invocations."""
    run = _current.get()
    return [{"id": key, "name": name, "url": f"/api/skill-files/{run.directory.name}/{key}"}
            for key, name, _ in skill_files.available(run.directory, run.sandbox.config.retention_days)]


@tool
async def restore_session_file(file_id: str) -> str:
    """Make a saved file from this session available as a read-only sandbox input. Use its returned path in argv."""
    run = _current.get()
    async with run.sandbox.lock:
        name, path = skill_files.lookup(run.directory, run.sandbox.config.retention_days, file_id)
        return run.sandbox.stage_input(file_id, name, path)


def local_tools():
    if not local_skills_enabled():
        return []
    tools = [list_skills, load_skill, read_skill_file, run_skill, list_session_files, restore_session_file]
    for item in tools:
        item.handle_tool_error = True
    return tools
