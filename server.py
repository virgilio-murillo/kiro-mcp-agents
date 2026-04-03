"""Kiro Agents MCP server — launch and manage kiro-cli agents."""

from mcp.server.fastmcp import FastMCP
from core import _jobs, launch, get_status, get_result, stop, open_ghostty_tab

mcp = FastMCP("kiro-agents")


@mcp.tool()
def launch_agent(agent: str, task: str, work_dir: str, model: str = None) -> str:
    """Launch a kiro-cli agent process. Returns immediately with a job_id.

    Args:
        agent: Agent name (e.g. 'investigator-child', 'correspondence-writer', 'report-creator')
        task: The task/prompt to give the agent
        work_dir: Working directory for the agent
        model: Optional model override (e.g. 'claude-sonnet-4.6-1m')
    """
    job_id = launch(agent, task, work_dir, model)
    job = _jobs[job_id]
    return (
        f"🚀 Agent '{agent}' launched as job {job_id}\n"
        f"📂 Logs: {job['log_path']}\n\n"
        f"Use agent_status('{job_id}') to check progress.\n"
        f"Use agent_result('{job_id}') to get output when complete."
    )


@mcp.tool()
def agent_status(job_id: str) -> str:
    """Check the status of a launched agent.

    Args:
        job_id: Job ID returned by launch_agent
    """
    status = get_status(job_id)
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
def agent_result(job_id: str) -> str:
    """Get the full output of a completed agent.

    Args:
        job_id: Job ID returned by launch_agent
    """
    result = get_result(job_id)
    if result is None:
        return f"Unknown job_id '{job_id}'."
    status = get_status(job_id)
    phase = status["phase"] if status else "unknown"
    return f"Agent {job_id} — {phase}\n\n{result}"


@mcp.tool()
def stop_agent(job_id: str) -> str:
    """Stop a running agent.

    Args:
        job_id: Job ID returned by launch_agent
    """
    if stop(job_id):
        return f"Agent {job_id} stopped."
    return f"Unknown job_id '{job_id}'."


def main():
    mcp.run()

if __name__ == "__main__":
    main()
