"""Kiro Agents MCP server — fully async, zero threads, non-blocking."""

import asyncio
import json
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from core import _jobs, launch, get_status, get_result, stop, _dbg

mcp = FastMCP("kiro-agents")
_dbg("server.py loaded — kiro-agents MCP server starting (async rewrite)")

AGENTS_DIRS = [
    Path(__file__).parent / "agents",
    Path(os.path.expanduser("~/.kiro/agents")),
]


# ── Core tools ──

@mcp.tool()
async def launch_agent(agent: str, task: str, work_dir: str, model: str = None) -> str:
    """Launch a kiro-cli agent process. Returns immediately with a job_id.

    Args:
        agent: Agent name (e.g. 'investigator-child', 'correspondence-writer', 'report-creator')
        task: The task/prompt to give the agent
        work_dir: Working directory for the agent
        model: Optional model override (e.g. 'claude-sonnet-4.6-1m')
    """
    _dbg(f"launch_agent: agent={agent} work_dir={work_dir}")
    t0 = time.monotonic()
    job_id = await launch(agent, task, work_dir, model)
    job = _jobs[job_id]
    elapsed = time.monotonic() - t0
    _dbg(f"launch_agent: done in {elapsed*1000:.1f}ms, job_id={job_id}")
    return (
        f"🚀 Agent '{agent}' launched as job {job_id}\n"
        f"📂 Logs: {job['log_path']}\n\n"
        f"Use agent_status('{job_id}') to check progress.\n"
        f"Use agent_result('{job_id}') to get output when complete."
    )


@mcp.tool()
async def agent_status(job_id: str) -> str:
    """Check the status of a launched agent.

    Args:
        job_id: Job ID returned by launch_agent
    """
    _dbg(f"agent_status: job_id={job_id}")
    t0 = time.monotonic()
    status = await get_status(job_id)
    elapsed = time.monotonic() - t0
    _dbg(f"agent_status: completed in {elapsed*1000:.1f}ms")
    if not status:
        candidates = list(_jobs.keys())
        return f"Unknown job_id '{job_id}'. Active jobs: {candidates or 'none'}"
    icon = "✅" if status["phase"] == "complete" else "⏳"
    result = f"Agent {job_id} ({status['agent']}) — {icon} {status['phase']}"
    if status["exit_code"] is not None:
        result += f" (exit {status['exit_code']})"
    result += f"\n\n📋 Recent output:\n{status['log_tail']}"
    return result


@mcp.tool()
async def agent_result(job_id: str) -> str:
    """Get the full output of a completed agent.

    Args:
        job_id: Job ID returned by launch_agent
    """
    _dbg(f"agent_result: job_id={job_id}")
    t0 = time.monotonic()
    result = await get_result(job_id)
    elapsed = time.monotonic() - t0
    _dbg(f"agent_result: completed in {elapsed*1000:.1f}ms")
    if result is None:
        return f"Unknown job_id '{job_id}'."
    job = _jobs.get(job_id, {})
    phase = job.get("phase", "unknown")

    # Write full result to file to avoid pipe backpressure deadlock.
    # Return file path + preview so the caller can read the file.
    PIPE_SAFE_LIMIT = 12000  # chars safe for pipe (well under 65KB JSON-RPC)
    full_response = f"Agent {job_id} — {phase}\n\n{result}"

    if len(full_response) <= PIPE_SAFE_LIMIT:
        return full_response

    # Large result: write to file, return pointer + preview
    out_dir = job.get("out_dir", "")
    result_file = str(Path(out_dir) / "result.md") if out_dir else f"/tmp/kiro-agent-result-{job_id}.md"
    Path(result_file).write_text(full_response)
    _dbg(f"agent_result: large output ({len(full_response)} chars) written to {result_file}")

    # Build preview: first and last lines
    lines = result.splitlines()
    preview_head = "\n".join(lines[:15])
    preview_tail = "\n".join(lines[-15:]) if len(lines) > 30 else ""
    preview = preview_head
    if preview_tail:
        preview += f"\n\n[... {len(lines) - 30} lines omitted ...]\n\n{preview_tail}"

    return (
        f"Agent {job_id} — {phase}\n"
        f"📄 Full output ({len(full_response)} chars) written to: {result_file}\n"
        f"⚡ Use the `read` tool to get the full content from that file.\n\n"
        f"--- PREVIEW ---\n{preview[:3000]}"
    )


@mcp.tool()
async def stop_agent(job_id: str) -> str:
    """Stop a running agent.

    Args:
        job_id: Job ID returned by launch_agent
    """
    _dbg(f"stop_agent: job_id={job_id}")
    t0 = time.monotonic()
    ok = await stop(job_id)
    elapsed = time.monotonic() - t0
    _dbg(f"stop_agent: completed in {elapsed*1000:.1f}ms ok={ok}")
    if ok:
        return f"Agent {job_id} stopped."
    return f"Unknown job_id '{job_id}'."


# ── Dynamic agent-as-tool registration ──

def _register_agent_tools():
    """Scan agent definitions and register those with expose_as_tool as MCP tools."""
    seen = set()
    for agents_dir in AGENTS_DIRS:
        if not agents_dir.exists():
            continue
        for f in sorted(agents_dir.glob("*.json")):
            try:
                cfg = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            spec = cfg.get("expose_as_tool")
            if not spec:
                continue
            tool_name = spec["name"]
            if tool_name in seen:
                continue
            seen.add(tool_name)
            _make_agent_tool(cfg, spec)


def _make_agent_tool(cfg: dict, spec: dict):
    """Create and register a single agent-as-tool."""
    tool_name = spec["name"]
    agent_name = cfg["name"]
    description = spec.get("description", f"Launch {agent_name} agent")
    params = spec.get("parameters", {})
    task_template = spec.get("task_template", "{task}")

    param_names = list(params.keys()) + ["work_dir"]
    annotations = {"work_dir": str, "return": str}
    defaults = {}
    for pname, pinfo in params.items():
        if pinfo.get("type") == "boolean":
            annotations[pname] = bool
            if not pinfo.get("required"):
                defaults[pname] = True
        else:
            annotations[pname] = str
            if not pinfo.get("required"):
                defaults[pname] = ""

    doc_lines = [description, "", "    Args:"]
    for pname, pinfo in params.items():
        req = " (required)" if pinfo.get("required") else ""
        doc_lines.append(f"        {pname}: {pinfo.get('description', '')}{req}")
    doc_lines.append("        work_dir: Working directory for the agent (required)")

    sig_parts = []
    for pname in params:
        if pname in defaults:
            sig_parts.append(f"{pname}={repr(defaults[pname])}")
        else:
            sig_parts.append(pname)
    sig_parts.append("work_dir: str = ''")
    sig = ", ".join(sig_parts)

    func_code = f"""
async def {tool_name}({sig}) -> str:
    '''{chr(10).join(doc_lines)}'''
    import os
    work_dir_resolved = work_dir or os.getcwd()
    os.makedirs(work_dir_resolved, exist_ok=True)
    tpl_vars = {{"work_dir": work_dir_resolved}}
    for pname in param_names_ref:
        tpl_vars[pname] = locals().get(pname, "")
    if "first_correspondence" in tpl_vars:
        tpl_vars["correspondence_type"] = (
            "This is a FIRST CORRESPONDENCE (introduce yourself)."
            if tpl_vars["first_correspondence"] else "This is a FOLLOW-UP CORRESPONDENCE."
        )
    if "case_id" in tpl_vars and not tpl_vars["case_id"]:
        tpl_vars["case_id"] = "draft"
    if "report_type" in tpl_vars and not tpl_vars["report_type"]:
        tpl_vars["report_type"] = "investigation"
    task = task_tpl_ref.format(**tpl_vars)
    job_id = await launch_ref(agent_name_ref, task, work_dir_resolved)
    job = jobs_ref[job_id]
    return (
        f"🚀 {{agent_name_ref}} launched as job {{job_id}}\\n"
        f"📂 Logs: {{job['log_path']}}\\n\\n"
        f"Use agent_status('{{job_id}}') to check progress.\\n"
        f"Use agent_result('{{job_id}}') to get output when complete."
    )
"""
    local_ns = {
        "param_names_ref": list(params.keys()),
        "task_tpl_ref": task_template,
        "launch_ref": launch,
        "jobs_ref": _jobs,
        "agent_name_ref": agent_name,
    }
    exec(func_code, local_ns)
    handler = local_ns[tool_name]
    handler.__annotations__ = annotations
    mcp.tool(name=tool_name, description=description)(handler)


_register_agent_tools()


def main():
    mcp.run()

if __name__ == "__main__":
    main()
