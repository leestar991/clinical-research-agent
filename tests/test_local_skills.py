import os
import time
from dataclasses import replace

import pytest
from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from langchain_core.tools import ToolException

from agent import local_skills as skills
from agent import skill_files
from agent.local_sandbox import _docker
from agent.skill_directory import LocalConfig, discover, relative_path, snapshot


NAME = "life-sciences-literature-search"
ENTRY = "scripts/search_literature.py"


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = replace(LocalConfig.load(), files=tmp_path / "files")
    monkeypatch.setenv(RUNTIME_STORE_LOCAL_ENV, "true")
    monkeypatch.setattr(LocalConfig, "load", lambda: config)
    return config


async def test_skill_snapshot_and_discovery(config, tmp_path, monkeypatch):
    assert NAME in [s["name"] for s in discover(config.directory)]
    async with skills.skill_invocation("alice", "conversation"):
        result = await skills.load_skill.ainvoke({"name": NAME})
        assert result["instructions"] == (config.directory / NAME / "SKILL.md").read_text()
        assert ENTRY in result["files"]
        assert "PubMed" in await skills.read_skill_file.ainvoke({"name": NAME, "path": "references/sources.md"})
        assert not skills._current.get().sandbox.created
    with pytest.raises(ToolException):
        relative_path("../.env")
    package = tmp_path / "bad"
    package.mkdir()
    (package / "SKILL.md").write_text("test")
    (package / "link").symlink_to(config.directory)
    with pytest.raises(ToolException, match="regular files"):
        snapshot(tmp_path, "bad", tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV)
    assert skills.local_tools() == []


def test_private_files_expire_independently(config):
    directory = skill_files.session_directory(config, "alice", "same")
    old = skill_files.publish(directory, "results.json", b"old")
    filename, path = skill_files.lookup(directory, 30, old)
    assert filename == "results.json"
    os.utime(path, (time.time() - 30 * 86400,) * 2)
    new = skill_files.publish(directory, "results.json", b"new")
    for actor, session, key in (("alice", "same", old), ("bob", "same", new), ("alice", "other", new)):
        with pytest.raises(ToolException):
            skill_files.lookup(skill_files.session_directory(config, actor, session), 30, key)
    skill_files.sweep(config)
    assert not path.exists()
    assert skill_files.lookup(directory, 30, new)[1].read_bytes() == b"new"


@pytest.mark.skipif(os.getenv("TEST_SKILL_DOCKER") != "1", reason="requires local Docker and the configured image")
async def test_real_cli_in_disposable_containers(config):
    async with skills.skill_invocation("alice", "session"):
        await skills.load_skill.ainvoke({"name": NAME})
        sandbox = skills._current.get().sandbox
        version = await skills.run_skill.ainvoke({"name": NAME, "entry": ENTRY, "argv": ["--version"]})
        assert version["exit_code"] == 0 and version["stdout"].strip()
        result = await skills.run_skill.ainvoke({
            "name": NAME, "entry": ENTRY,
            "argv": ["clinical", "KRAS G12C clinical trials", "--format", "json", "--dry-run", "--no-normalize", "--out", "plan.json"],
            "save_files": ["plan.json"],
        })
        assert result["exit_code"] == 0, result
        saved, = result["files"]
    assert (await _docker("container", "inspect", sandbox.name))[0] != 0
    async with skills.skill_invocation("alice", "session"):
        obj, = await skills.list_session_files.ainvoke({})
        assert obj == saved
        restored = await skills.restore_session_file.ainvoke({"file_id": obj["id"]})
        await skills.load_skill.ainvoke({"name": NAME})
        rendered = await skills.run_skill.ainvoke({
            "name": NAME, "entry": "scripts/render_evidence.py",
            "argv": [restored, "--format", "table"],
        })
        assert rendered["exit_code"] == 0, rendered
        assert skills._current.get().sandbox.name != sandbox.name


def test_download_enforces_authenticated_owner(config, monkeypatch):
    from tests.test_demo_ui import _client

    directory = skill_files.session_directory(config, "alice", "same")
    key = skill_files.publish(directory, "result.txt", b"private")
    url = f"/api/skill-files/{directory.name}/{key}"
    client = _client(monkeypatch)
    assert client.get(url).content == b"private"
    monkeypatch.setenv("AGENT_ADMIN_USERS", "operator")
    for actor in ("bob", "operator"):
        assert client.get(url + "?actor=alice", headers={"X-Forwarded-Email": actor}).status_code == 404
    assert client.get("/api/skill-files/invalid/" + key).status_code == 404
    monkeypatch.delenv(RUNTIME_STORE_LOCAL_ENV)
    assert _client(monkeypatch).get(url).status_code == 404


@pytest.mark.skipif(os.getenv("TEST_SKILL_DOCKER") != "1", reason="requires local Docker and the configured image")
async def test_secret_isolation_and_timeout(config, tmp_path, monkeypatch):
    root = tmp_path / "skills"
    package = root / "inspection"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: inspection\ndescription: inspection fixture\n---\n")
    (package / "inspect.sh").write_text('test -z "$LOCAL_SKILL_TEST_SECRET" && test ! -e /var/run/docker.sock && id -u')
    (package / "inspect.py").write_text('import os; print(os.getenv("LOCAL_SKILL_TEST_SECRET", "absent"))')
    (package / "wait.py").write_text('import time; time.sleep(10)')
    monkeypatch.setenv("LOCAL_SKILL_TEST_SECRET", "fixture")
    monkeypatch.setattr(LocalConfig, "load", lambda: replace(config, directory=root, timeout=1))
    async with skills.skill_invocation("alice", "session"):
        sandbox = skills._current.get().sandbox
        await skills.load_skill.ainvoke({"name": "inspection"})
        for entry, expected in (("inspect.sh", "65534"), ("inspect.py", "absent")):
            result = await skills.run_skill.ainvoke({"name": "inspection", "entry": entry, "argv": []})
            assert result["exit_code"] == 0 and result["stdout"].strip() == expected
        tools = skills.local_tools()
        command = next(t for t in tools if t.name == "run_skill")
        assert "timed out" in await command.ainvoke({"name": "inspection", "entry": "wait.py", "argv": []})
        assert not sandbox.created
    assert (await _docker("container", "inspect", sandbox.name))[0] != 0
