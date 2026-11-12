"""Run one throwaway container, with the dials set deliberately.

The contract is small on purpose: a host directory becomes `/work` inside the container, a
fixed argv runs, and whatever the process left in that directory is the result. There is no
shell, no string interpolation into a command line, and nothing is ever passed as code on the
command line - code travels as a file in the mounted directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import Boundary
from ..settings import Settings, settings


class SandboxUnavailable(Boundary):
    """Docker or the sandbox image is not there; the message names the fix (make image, start Docker).
    """
    kind = "sandbox_unavailable"


@dataclass
class ContainerResult:
    """How one container run ended: exit code, time, timeout, killed for memory, its output.

    Returned by: run_container; read by the render and code clients.
    """
    ok: bool
    exit_code: int
    seconds: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    killed_for_memory: bool = False
    argv: list[str] = field(default_factory=list)

    def detail(self) -> dict:
        """The container's outcome for logs and manifests: exit code, time, timeout, memory kill,
        the
        last 2000 characters of its output.
        """
        return {"exit_code": self.exit_code, "seconds": self.seconds, "timed_out": self.timed_out,
                "killed_for_memory": self.killed_for_memory,
                "stderr": self.stderr[-2000:], "stdout": self.stdout[-2000:]}


def available(s: Settings | None = None) -> tuple[bool, str]:
    """Is the sandbox usable right now? -> (ok, reason). Used by `doctor` and by fallbacks.

    Called by: cli.cmd_doctor, run_container.
    """
    s = s or settings()
    if not shutil.which(s.docker_bin):
        return False, f"{s.docker_bin} is not on PATH"
    try:
        p = subprocess.run([s.docker_bin, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "the docker daemon did not answer within 20s"
    if p.returncode != 0:
        return False, f"the docker daemon is not reachable: {p.stderr.strip()[:200]}"
    img = subprocess.run([s.docker_bin, "image", "inspect", s.sandbox_image],
                         capture_output=True, text=True, timeout=30)
    if img.returncode != 0:
        return False, f"image {s.sandbox_image} is not built. Run: make image"
    return True, f"docker {p.stdout.strip()}, image {s.sandbox_image}"


def run_container(argv: list[str], workdir: str | Path, *, timeout_s: int, memory: str,
                  cpus: str | None = None, pids: int | None = None, network: str = "none",
                  env: dict[str, str] | None = None, read_only: bool = True,
                  tmpfs_mb: int = 512, s: Settings | None = None) -> ContainerResult:
    """Run `argv` in the sandbox image with `workdir` mounted at /work.

    Defaults are the strict ones; callers loosen them explicitly and say why.

    * `--network none`        nothing in here can reach the network, model endpoints included
    * `--read-only`           the image's filesystem cannot be modified; /work and /tmp are the
                              only writable places, and both are thrown away afterwards
    * `--cap-drop ALL`,
      `--security-opt
       no-new-privileges`     no capabilities, no setuid escalation
    * `--memory`/`--pids-limit`/`--cpus`  a runaway loop or a fork bomb hits a wall, not the host
    * `--user <host uid:gid>` files written to /work come back owned by us, not by root

    Called by: cli.cmd_selftest, sandbox.code.run_builder, sandbox.render._render_docker.
    """
    s = s or settings()
    ok, why = available(s)
    if not ok:
        raise SandboxUnavailable(why)
    workdir = Path(workdir).resolve()
    home = workdir / ".home"            # LibreOffice insists on a writable HOME
    home.mkdir(parents=True, exist_ok=True)
    name = f"deckops-{uuid.uuid4().hex[:12]}"
    # Deliberately not --rm: the container has to outlive its own exit for long enough to be
    # asked *why* it stopped. Exit code 137 is "killed by SIGKILL", which is what both the
    # memory cgroup and an outside kill look like, and telling a model "you used too much
    # memory" when it actually ran too long sends it off fixing the wrong thing.
    cmd = [
        s.docker_bin, "run", "--name", name,
        "--network", network,
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", memory, "--memory-swap", memory,
        "--tmpfs", f"/tmp:rw,size={tmpfs_mb}m",
        "-v", f"{workdir}:/work:rw",
        "-w", "/work",
        "-e", "HOME=/work/.home",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "MALLOC_ARENA_MAX=2",
    ]
    if read_only:
        cmd.append("--read-only")
    if cpus:
        cmd += ["--cpus", str(cpus)]
    if pids:
        cmd += ["--pids-limit", str(pids)]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [s.sandbox_image, *argv]

    t0 = time.time()
    timed_out = False
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        # Killing the client does not kill the container, so say so explicitly.
        subprocess.run([s.docker_bin, "kill", name], capture_output=True, timeout=30)
        timed_out, rc = True, 124
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    seconds = round(time.time() - t0, 2)
    oom = _was_oom_killed(name, s)
    subprocess.run([s.docker_bin, "rm", "-f", name], capture_output=True, timeout=60)
    return ContainerResult(ok=(rc == 0), exit_code=rc, seconds=seconds, stdout=out, stderr=err,
                           timed_out=timed_out, killed_for_memory=oom, argv=argv)


def _was_oom_killed(name: str, s: Settings) -> bool:
    """Ask Docker, rather than reading it off the exit code.

    Called by: run_container.
    """
    try:
        p = subprocess.run([s.docker_bin, "inspect", "--format", "{{.State.OOMKilled}}", name],
                           capture_output=True, text=True, timeout=30)
        return p.returncode == 0 and p.stdout.strip().lower() == "true"
    except Exception:
        return False


def read_result(workdir: str | Path, name: str = "out/result.json") -> dict:
    """Read the JSON a sandbox job leaves behind. A missing file is a failure, not an empty dict.


    Called by: cli.cmd_selftest.
    """
    path = Path(workdir) / name
    if not path.exists():
        raise Boundary(f"the sandbox produced no {name}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Boundary(f"the sandbox wrote unreadable {name}: {e}") from None
