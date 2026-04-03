"""Core primitives for spawning and managing kiro-cli agents."""

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

# Ensure homebrew and local bins are in PATH
os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + os.environ.get("PATH", "")

# ── Job registry (shared in-process) ──
_jobs: dict[str, dict] = {}


def spawn_kiro(agent: str, task: str, work_dir: str, log_path: str, model: str = None) -> subprocess.Popen:
    """Spawn a kiro-cli agent process. Returns the Popen handle."""
    log_file = open(log_path, "w")
    cmd = [
        os.path.expanduser("~/.local/bin/kiro-cli"), "chat",
        "--no-interactive", "--trust-all-tools",
        "--agent", agent, "--wrap=never",
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(f"skip confirmation. {task}")
    return subprocess.Popen(
        cmd, stdout=log_file, stderr=subprocess.STDOUT,
        cwd=work_dir, preexec_fn=os.setsid,
    )


def is_done(proc) -> bool:
    if isinstance(proc, str):
        return True
    return proc.poll() is not None


def kill_proc(proc):
    if isinstance(proc, subprocess.Popen) and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def tail(path: str, n: int = 20) -> str:
    try:
        lines = Path(path).read_text().splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return "(no output yet)"


def open_ghostty_tab(command: str):
    """Open a new Ghostty tab via native AppleScript API."""
    script = (
        'tell application "Ghostty"\n'
        '  set cfg to new surface configuration\n'
        f'  set command of cfg to "{command}"\n'
        '  new tab in front window with configuration cfg\n'
        'end tell'
    )
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch(agent: str, task: str, work_dir: str, model: str = None) -> str:
    """Launch an agent and return a job_id. Registers in _jobs."""
    job_id = str(uuid.uuid4())[:8]
    out_dir = str(Path(work_dir) / ".kiro-agents" / job_id)
    os.makedirs(out_dir, exist_ok=True)
    log_path = str(Path(out_dir) / "agent.log")

    proc = spawn_kiro(agent, task, work_dir, log_path, model)

    _jobs[job_id] = {
        "job_id": job_id, "agent": agent, "task": task,
        "work_dir": work_dir, "out_dir": out_dir,
        "log_path": log_path, "proc": proc,
        "phase": "running",
    }

    def _monitor():
        while proc.poll() is None:
            time.sleep(5)
        _jobs[job_id]["phase"] = "complete"

    threading.Thread(target=_monitor, daemon=True).start()
    return job_id


def get_status(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    proc = job["proc"]
    done = is_done(proc)
    return {
        "job_id": job_id, "agent": job["agent"],
        "phase": "complete" if done else "running",
        "exit_code": proc.returncode if done and isinstance(proc, subprocess.Popen) else None,
        "log_tail": tail(job["log_path"], 15),
    }


def get_result(job_id: str) -> str | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    return tail(job["log_path"], 200)


def stop(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if not job:
        return False
    kill_proc(job["proc"])
    job["phase"] = "stopped"
    return True
