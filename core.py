"""Core primitives for spawning and managing kiro-cli agents — fully async, zero threads."""

import asyncio
import fcntl
import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure homebrew and local bins are in PATH
os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + os.environ.get("PATH", "")

# ── Safety limits ──
MAX_DEPTH = 3
MAX_CHILDREN = 7
MAX_SYSTEM_AGENTS = 30
_DEPTH_ENV = "KIRO_AGENT_DEPTH"
_LAUNCH_LOCK = str(Path.home() / ".kiro/.agent-launch.lock")

# ── Debug logging ──
_LOG_PATH = os.path.expanduser("~/.kiro/kiro-agents-debug.log")


def _dbg(msg: str):
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')} pid={os.getpid()}] {msg}\n")
            f.flush()
    except OSError:
        pass


# ── Job registry (single event loop — plain dict is safe) ──
_jobs: dict[str, dict] = {}
_result_cache: dict[str, str] = {}


async def _async_tail(path: str, n: int = 20) -> str:
    """Non-blocking tail via subprocess. Returns last n lines."""
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "tail", "-n", str(n), path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        elapsed = time.monotonic() - t0
        if elapsed > 1.0:
            _dbg(f"_async_tail: SLOW {path} took {elapsed:.2f}s")
        return stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError) as e:
        _dbg(f"_async_tail: error {path}: {e}")
        return "(no output yet)"


async def _async_flock(fd, max_attempts: int = 5, interval: float = 0.2):
    """Non-blocking flock with async retry."""
    for attempt in range(max_attempts):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _dbg(f"_async_flock: acquired on attempt {attempt}")
            return
        except BlockingIOError:
            if attempt == max_attempts - 1:
                raise RuntimeError(
                    f"Cannot acquire agent launch lock after {max_attempts} attempts. "
                    f"Try: rm {_LAUNCH_LOCK}"
                )
            await asyncio.sleep(interval)


async def _count_agents() -> int:
    """Count running kiro-cli agent processes via pgrep."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-f", "kiro-cli.*--no-interactive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip().count("\n") + 1 if stdout.strip() else 0
    except Exception:
        return 0


async def _monitor(job_id: str):
    """Background task to detect agent completion."""
    job = _jobs[job_id]
    proc = job["proc"]
    log_path = job["log_path"]
    COMPLETION_MARKER = b"\x1b]9;Response complete\x07"
    last_size = -1
    stale_since = None

    _dbg(f"_monitor[{job_id}]: started pid={proc.pid}")

    while proc.returncode is None:
        await asyncio.sleep(5)
        try:
            p = Path(log_path)
            if not p.exists():
                continue
            size = p.stat().st_size
            if size != last_size:
                last_size = size
                stale_since = None
                job["last_progress"] = time.monotonic()
            else:
                if stale_since is None:
                    stale_since = time.monotonic()
                    _dbg(f"_monitor[{job_id}]: output stale at size={size}")
                stale_secs = time.monotonic() - stale_since
                # Check completion marker in last 1KB
                if stale_secs >= 30 and size > 0:
                    with open(log_path, "rb") as fh:
                        fh.seek(max(0, size - 1024))
                        data = fh.read()
                    if COMPLETION_MARKER in data:
                        _dbg(f"_monitor[{job_id}]: completion marker found + stale {stale_secs:.0f}s")
                        break
                if stale_secs >= 300:
                    _dbg(f"_monitor[{job_id}]: 5-min staleness timeout")
                    break
        except OSError:
            continue

    _dbg(f"_monitor[{job_id}]: done, returncode={proc.returncode}")
    job["phase"] = "complete"


async def launch(agent: str, task: str, work_dir: str, model: str = None) -> str:
    """Launch an agent. Fully async, non-blocking."""
    t0 = time.monotonic()
    _dbg(f"launch: agent={agent} work_dir={work_dir}")

    # Depth limit
    current_depth = int(os.environ.get(_DEPTH_ENV, "0"))
    if current_depth >= MAX_DEPTH:
        raise RuntimeError(f"Agent depth limit reached ({current_depth}/{MAX_DEPTH}).")

    # Children limit
    running = sum(1 for j in _jobs.values() if j["phase"] == "running")
    if running >= MAX_CHILDREN:
        raise RuntimeError(f"Children limit reached ({running}/{MAX_CHILDREN}).")

    # Cross-process lock + system agent count
    lockf = open(_LAUNCH_LOCK, "a")
    try:
        await _async_flock(lockf.fileno())
        count = await _count_agents()
        if count >= MAX_SYSTEM_AGENTS:
            raise RuntimeError(f"System agent limit reached ({count}/{MAX_SYSTEM_AGENTS}).")

        job_id = str(uuid.uuid4())[:8]
        out_dir = str(Path(work_dir) / ".kiro-agents" / job_id)
        os.makedirs(out_dir, exist_ok=True)
        log_path = str(Path(out_dir) / "agent.log")

        # Build command
        cmd = [
            os.path.expanduser("~/.local/bin/kiro-cli"), "chat",
            "--no-interactive", "--trust-all-tools",
            "--agent", agent, "--wrap=never",
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append(f"skip confirmation. {task}")

        env = {**os.environ, _DEPTH_ENV: str(current_depth + 1)}
        log_file = open(log_path, "w")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
            start_new_session=True,
            env=env,
        )
        log_file.close()
        _dbg(f"launch: spawned pid={proc.pid} job_id={job_id}")
    finally:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        lockf.close()

    # Persist metadata
    Path(out_dir, "meta.json").write_text(json.dumps({
        "job_id": job_id, "agent": agent, "task": task[:500],
        "work_dir": work_dir, "started_at": datetime.now(timezone.utc).isoformat(),
    }))

    _jobs[job_id] = {
        "job_id": job_id, "agent": agent, "task": task,
        "work_dir": work_dir, "out_dir": out_dir,
        "log_path": log_path, "proc": proc,
        "phase": "running", "last_progress": time.monotonic(),
        "monitor_task": None,
    }

    # Start monitor as async task
    _jobs[job_id]["monitor_task"] = asyncio.create_task(_monitor(job_id))

    elapsed = time.monotonic() - t0
    _dbg(f"launch: complete in {elapsed*1000:.1f}ms")
    return job_id


async def get_status(job_id: str) -> dict | None:
    """Get agent status. Non-blocking."""
    t0 = time.monotonic()
    job = _jobs.get(job_id)
    if not job:
        return None
    proc = job["proc"]
    phase = job["phase"] if job["phase"] in ("stopped", "complete") else (
        "complete" if proc.returncode is not None else "running"
    )
    log_tail = await _async_tail(job["log_path"], 15)
    elapsed = time.monotonic() - t0
    _dbg(f"get_status[{job_id}]: {phase} in {elapsed*1000:.1f}ms")
    return {
        "job_id": job_id, "agent": job["agent"],
        "phase": phase,
        "exit_code": proc.returncode,
        "log_tail": log_tail,
    }


async def get_result(job_id: str) -> str | None:
    """Get agent output. Caches completed results."""
    t0 = time.monotonic()
    if job_id in _result_cache:
        _dbg(f"get_result[{job_id}]: cache hit")
        return _result_cache[job_id]
    job = _jobs.get(job_id)
    if not job:
        return None
    result = await _async_tail(job["log_path"], 200)
    # Cache if complete
    if job["phase"] in ("complete", "stopped") or job["proc"].returncode is not None:
        _result_cache[job_id] = result
        _dbg(f"get_result[{job_id}]: cached ({len(result)} chars)")
    elapsed = time.monotonic() - t0
    _dbg(f"get_result[{job_id}]: {elapsed*1000:.1f}ms")
    return result


async def stop(job_id: str) -> bool:
    """Stop an agent. Kills process group."""
    job = _jobs.get(job_id)
    if not job:
        return False
    proc = job["proc"]
    if proc.returncode is not None:
        job["phase"] = "stopped"
        return True

    _dbg(f"stop[{job_id}]: killing pid={proc.pid}")
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        job["phase"] = "stopped"
        return True

    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        _dbg(f"stop[{job_id}]: exited after SIGTERM")
    except asyncio.TimeoutError:
        _dbg(f"stop[{job_id}]: SIGKILL")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()

    # Cancel monitor task
    mt = job.get("monitor_task")
    if mt and not mt.done():
        mt.cancel()
        try:
            await mt
        except asyncio.CancelledError:
            pass

    job["phase"] = "stopped"
    return True
