# kiro-mcp-agents

General-purpose agent launcher MCP server for [Kiro CLI](https://github.com/virgilio-murillo/kiro-config).

## Tools

| Tool | Description |
|------|-------------|
| `launch_agent(agent, task, work_dir, model?)` | Spawn a kiro-cli agent, returns job_id |
| `agent_status(job_id)` | Check if agent is done, get output preview |
| `agent_result(job_id)` | Get full agent output |
| `stop_agent(job_id)` | Kill a running agent |

## Pre-configured Agents

Agent definitions in `agents/` — copy to `~/.kiro/agents/`:

- **correspondence-writer** — writes professional AWS customer correspondence
- **report-creator** — generates structured reports from raw findings

## Python API

Other MCP servers can import the core module:

```python
from core import spawn_kiro, launch, get_status, get_result, stop, _jobs
```

## Setup

```bash
cd ~/.kiro/mcp-servers/kiro-agents
python -m venv .venv
.venv/bin/pip install -e .
```
