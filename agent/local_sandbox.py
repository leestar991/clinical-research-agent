"""One disposable local Docker container per invocation, created on first execution."""

import asyncio
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from agent.skill_directory import LocalConfig, relative_path
from langchain_core.tools import ToolException

MAX_FILE = 8 * 1024 * 1024
MAX_OUTPUT = 32 * 1024

# The container has an expendable tmpfs; only validated, explicitly selected files leave it.
_READ_OUTPUT = """
from pathlib import Path
import sys
root = Path('/work')
p = (root / sys.argv[1]).resolve()
if not p.is_relative_to(root) or not p.is_file() or p.stat().st_size > int(sys.argv[2]):
    sys.exit('Expected a regular output file under /work within the size limit.')
sys.stdout.buffer.write(p.read_bytes())
"""


async def _docker(*args: str, timeout: int = 30, limit: int = MAX_OUTPUT):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ToolException("Install Docker and start the local Docker engine to run skills.") from None

    async def read(stream):
        data = bytearray()
        total = 0
        while chunk := await stream.read(65536):
            total += len(chunk)
            data.extend(chunk[:max(0, limit - len(data))])
        return bytes(data), total > limit

    try:
        async with asyncio.timeout(timeout):
            stdout, stderr, code = await asyncio.gather(read(proc.stdout), read(proc.stderr), proc.wait())
        return code, stdout, stderr
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


def _text(output):
    data, truncated = output
    return data.decode(errors="replace") + ("\n[output truncated]" if truncated else "")


class LocalSandbox:
    def __init__(self, config: LocalConfig):
        self.config = config
        self._temp = None
        self.name = "agent-skill-" + uuid4().hex
        self.created = False
        self._launcher = None
        self.lock = asyncio.Lock()

    @property
    def staging(self) -> Path:
        if self._temp is None:
            self._temp = TemporaryDirectory(prefix="agent-skill-")
            root = Path(self._temp.name)
            for subdir in ("skills", "inputs"):
                (root / subdir).mkdir(mode=0o755)
        return Path(self._temp.name)

    async def ensure_running(self):
        if self.created:
            return
        # Bind mounts must refer to this machine, even if the developer also has remote contexts.
        code, endpoint, err = await _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
        host = os.getenv("DOCKER_HOST") or _text(endpoint).strip()
        if code or not host.startswith("unix://") or not _text(endpoint).strip().startswith("unix://"):
            raise ToolException("Select a local Unix-socket Docker context. " + _text(err))
        root = self.staging.resolve()
        args = [
            "run", "--interactive", "--rm", "--pull=never", "--name", self.name,
            "--user", "65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--init", "--pids-limit=64", "--memory=384m", "--cpus=1",
            "--log-driver=none", "--tmpfs", "/work:rw,nosuid,nodev,size=64m,mode=1777",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777", "--workdir", "/work",
            "--mount", f"type=bind,src={root / 'skills'},dst=/skills,readonly",
            "--mount", f"type=bind,src={root / 'inputs'},dst=/inputs,readonly",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
        ]
        # Docker clients can implicitly copy proxy credentials from their own configuration.
        for key in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy"):
            args.extend(["--env", key + "=", "--env", key.upper() + "="])
        # Keep stdin open for exactly the lifetime of this worker. On worker loss the pipe
        # closes, cat exits, and Docker removes the container, including running exec tasks.
        self._launcher = await asyncio.create_subprocess_exec(
            "docker", *args, self.config.image, "sh", "-c", "echo ready; exec cat >/dev/null",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(30):
            ready = await self._launcher.stdout.readline()
            if ready != b"ready\n":
                error = await self._launcher.stderr.read(4096)
                raise ToolException("Docker could not start the skill container. " + error.decode(errors="replace"))
        self.created = True

    async def run(self, package: str, entry: str, argv: list[str]):
        path = relative_path(entry)
        interpreter = {".py": "python3", ".sh": "bash"}.get(path.suffix)
        if not interpreter or not (self.staging / "skills" / package / path).is_file():
            raise ToolException("Choose a bundled .py or .sh entrypoint from the loaded skill.")
        try:
            await self.ensure_running()
            code, out, err = await _docker("exec", self.name, interpreter, f"/skills/{package}/{path}",
                                           *argv, timeout=self.config.timeout)
        except (TimeoutError, asyncio.CancelledError):
            await self.stop()
            raise
        except ToolException:
            await self.stop()
            raise
        return {"exit_code": code, "stdout": _text(out), "stderr": _text(err)}

    async def read_output(self, filename: str) -> bytes:
        path = relative_path(filename)
        code, out, err = await _docker("exec", self.name, "python3", "-I", "-c", _READ_OUTPUT,
                                       str(path), str(MAX_FILE), limit=MAX_FILE)
        if code or out[1]:
            raise ToolException("Cannot publish file: " + _text(err)[:500])
        return out[0]

    def stage_input(self, file_id: str, name: str, source: Path) -> str:
        dest = self.staging / "inputs" / file_id
        dest.mkdir(exist_ok=True)
        if not (dest / name).exists():
            shutil.copyfile(source, dest / name)
            (dest / name).chmod(0o444)
        return f"/inputs/{file_id}/{name}"

    async def stop(self):
        launcher = self._launcher
        if launcher is None:
            return
        launcher.stdin.close()
        try:
            await asyncio.wait_for(launcher.wait(), timeout=5)
        except TimeoutError:
            code, _, err = await _docker("rm", "--force", self.name)
            if code and "No such container" not in _text(err):
                raise ToolException("Could not remove invocation container. " + _text(err))
            await asyncio.wait_for(launcher.wait(), timeout=5)
        self.created = False
        self._launcher = None

    async def close(self):
        try:
            await self.stop()
        finally:
            if self._temp:
                self._temp.cleanup()
